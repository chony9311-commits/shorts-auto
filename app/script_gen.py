"""바이럴 쇼츠 대본 + AI 성우(TTS) 무음 제거 싱크.

1) generate_script(product)  : 규칙 기반 대본(반말·구어체 후킹, 한 줄 8~13자) -> JSON(dict)
2) synthesize_script(script) : 문장마다 edge-tts -> FFmpeg silenceremove 로 앞뒤 숨 제거
                               -> 실제 길이(초) 측정 -> 이어붙인 오디오 + 문장별 타임라인

타임라인(start/end)이 그대로 컷 전환·자막 타이밍의 기준이 된다(1:1 동기화).
대본은 유료 API 없이 동작하도록 템플릿 기반이다. 문장 품질을 올리고 싶으면
generate_script() 결과(JSON) 형식만 지키면 어떤 생성기로 바꿔 끼워도 된다.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

from . import settings, store
from .render import RenderError, ffmpeg_exe, probe_duration, _run
from .scraper import ProductInfo
from .source_fetcher import core_terms

# ------------------------------------------------------------------ 상수
VOICES: Dict[str, str] = {
    "female": "ko-KR-SunHiNeural",     # 여성
    "male": "ko-KR-InJoonNeural",      # 남성
}
DEFAULT_VOICE = "female"
DEFAULT_RATE = "+18%"                  # 쇼츠 템포: +15% ~ +20%
TARGET_SECONDS = (20.0, 25.0)
LINE_MIN, LINE_MAX = 8, 13             # 한 줄 글자 수(공백·부호 제외)
GAP_SECONDS = 0.06                     # 문장 사이 아주 짧은 이음(무음 제거 후 쫀쫀하게)


def visible_len(text: str) -> int:
    """글자 수(공백·문장부호 제외)."""
    return len(re.sub(r"[\s.,!?~…·'\"“”‘’()\[\]]", "", text or ""))


def resolve_voice(voice: Optional[str]) -> str:
    v = (voice or DEFAULT_VOICE).strip()
    return VOICES.get(v.lower(), v if v.startswith("ko-KR-") else VOICES[DEFAULT_VOICE])


def normalize_rate(rate: Optional[str]) -> str:
    """'18' / '+18' / '18%' / '+18%' -> '+18%' (범위 -30 ~ +50)."""
    m = re.search(r"-?\d+", str(rate if rate is not None else DEFAULT_RATE))
    n = int(m.group(0)) if m else 18
    if str(rate or "").strip().startswith("-") and n > 0:
        n = -n
    n = max(-30, min(50, n))
    return f"{n:+d}%"


# ------------------------------------------------------------------ 대본 템플릿
HOOKS = [
    "이거 진짜 미쳤는데", "친구 집에서 보고 바로 샀음", "나만 몰랐던 꿀템 발견",
    "이거 모르면 진짜 손해", "써보고 진심 놀랐음", "요즘 다들 이거 쓰더라",
    "이거 보고 바로 주문함", "살까 말까 고민하지 마",
]
HOOK2 = ["아직도 모르는 사람 많음", "진짜 끝까지 꼭 봐봐", "일단 이거 먼저 봐봐", "이유는 바로 알려줄게"]
PROBLEMS = [
    "매번 이게 제일 불편했잖아", "아직도 그냥 쓰고 있어?", "이거 때문에 스트레스였지?",
    "나도 처음엔 안 믿었음",
]
SOLUTIONS = ["근데 이거 하나면 해결됨", "이거 하나로 끝났음", "이걸로 갈아타니까 편함"]
INTRO = ["이게 바로 {core}", "바로 {core} 얘기야", "{core} 이거임"]
PRICE = ["{price}인데 이 퀄리티?", "{price} 이 가격 실화?", "{price}이면 사야지"]
PRICE_FALLBACK = ["가격 보고 진짜 놀람", "가격도 진짜 부담 없음"]
FEATURE_TAILS = ["이게 진짜 포인트", "이게 제일 좋음", "이건 진짜 편함", "이게 은근 큼"]
FILLERS = ["써본 사람은 다 알아", "한 번 쓰면 못 돌아가", "선물해도 좋을 것 같음", "집에 하나쯤 있어야 함",
           "솔직히 안 사면 후회함", "이건 진짜 꿀템 인정", "나도 이제 이것만 씀", "쓸수록 마음에 들더라",
           "이 정도면 가성비 갑임", "괜히 인기 있는 게 아님"]
CTAS = ["링크는 고정댓글에 있음", "품절되기 전에 챙겨", "저장해두고 나중에 사", "지금 바로 링크 눌러봐"]


def _first_fit(cands: List[str]) -> Optional[str]:
    for c in cands:
        if LINE_MIN <= visible_len(c) <= LINE_MAX:
            return c
    return None


def split_to_lines(text: str, max_lines: int = 2) -> List[str]:
    """긴 특징 문장을 어절 단위로 8~13자 줄로 쪼갠다(들어맞지 않으면 빈 리스트)."""
    text = re.sub(r"\s+", " ", re.sub(r"[()\[\]]", " ", text or "")).strip(" .,-")
    if not text:
        return []
    words = text.split(" ")
    lines: List[str] = []
    cur = ""
    for w in words:
        if visible_len(w) > LINE_MAX:             # 한 어절이 너무 길면 포기
            return []
        trial = (cur + " " + w).strip()
        if visible_len(trial) <= LINE_MAX:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    # 너무 짧은 꼬리는 앞줄에 붙이거나(가능하면) 버린다
    if len(lines) > 1 and visible_len(lines[-1]) < LINE_MIN:
        merged = lines[-2] + " " + lines[-1]
        if visible_len(merged) <= LINE_MAX:
            lines = lines[:-2] + [merged]
        else:
            lines = lines[:-1]
    return [ln for ln in lines if LINE_MIN <= visible_len(ln) <= LINE_MAX][:max_lines]


def feature_lines(feature: str, rng: random.Random) -> List[str]:
    """특징 한 줄 -> 8~13자 대사 1~2줄."""
    parts = split_to_lines(feature, 2)
    if parts:
        return parts
    f = re.sub(r"\s+", " ", feature).strip(" .,-")
    if 2 <= visible_len(f) < LINE_MIN:          # 짧은 키워드("방수") -> 꼬리말로 늘린다
        tails = FEATURE_TAILS[:]
        rng.shuffle(tails)
        hit = _first_fit([f"{f} {t}" for t in tails])
        return [hit] if hit else []
    return []


def _core_name(product: ProductInfo) -> str:
    terms = core_terms(product.name, product.brand)
    if len(terms) >= 2 and visible_len(f"{terms[0]} {terms[1]}") <= 9:      # "미니" 보다 "미니 믹서기"
        return f"{terms[0]} {terms[1]}"
    for t in terms:
        if 2 <= visible_len(t) <= 9:
            return t
    base = (terms[0] if terms else product.name)[:9]
    return base


def estimate_seconds(texts: List[str], rate: str = DEFAULT_RATE) -> float:
    """TTS 길이 대략치(무음 제거 후). 실제 값은 합성 후 측정한다."""
    speed = 6.0 * (1 + int(normalize_rate(rate)[:-1]) / 100.0)      # 초당 글자 수(대략치)
    return sum(visible_len(t) / speed + 0.18 for t in texts)


def generate_script(product: ProductInfo, seed: Optional[int] = None,
                    target: Tuple[float, float] = TARGET_SECONDS,
                    rate: str = DEFAULT_RATE) -> Dict[str, Any]:
    """상품 정보 -> 쇼츠 대본(JSON).

    {"title_lines": [...], "lines": [{"id","role","text"}...], "reserve": [...같은 형식...],
     "estimated_seconds": 22.1, "product": {...}}
    lines 는 hook -> (problem) -> intro -> feature... -> price -> cta 순서.
    reserve 는 길이가 모자랄 때 끼워 넣는 여분 대사(synthesize 가 사용).
    """
    key = seed if seed is not None else int(hashlib.sha1(product.name.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(key)
    core = _core_name(product)

    def pick(pool: List[str]) -> str:
        return rng.choice(pool)

    body: List[Tuple[str, str]] = []
    hook = pick(HOOKS)
    body.append(("hook", hook))
    body.append(("hook", pick([h for h in HOOK2])))
    body.append(("problem", pick(PROBLEMS)))
    body.append(("solution", pick(SOLUTIONS)))
    intro = _first_fit([t.format(core=core) for t in rng.sample(INTRO, len(INTRO))])
    if intro:
        body.append(("intro", intro))

    feats: List[Tuple[str, str]] = []
    for f in product.features[:5]:
        for ln in feature_lines(f, rng):
            feats.append(("feature", ln))
    body.extend(feats[:7])
    reserve_feats = feats[7:]

    price_line = None
    if product.price_text:
        price_line = _first_fit([t.format(price=product.price_text) for t in rng.sample(PRICE, len(PRICE))])
    body.append(("price", price_line or pick(PRICE_FALLBACK)))
    ctas = CTAS[:]
    rng.shuffle(ctas)
    cta_lines = [("cta", ctas[0])]

    fillers = FILLERS[:]
    rng.shuffle(fillers)
    reserve = reserve_feats + [("filler", t) for t in fillers] + [("cta", ctas[1])]

    # 길이 맞추기(대략): 모자라면 여분을 끼우고, 넘치면 특징/필러부터 덜어낸다
    def texts(items: List[Tuple[str, str]]) -> List[str]:
        return [t for _, t in items]

    lo, hi = target
    all_lines = body + cta_lines
    while estimate_seconds(texts(all_lines), rate) < lo and reserve:
        role, t = reserve.pop(0)
        insert_at = len(all_lines) - 1 if role != "cta" else len(all_lines)
        all_lines.insert(insert_at, (role, t))
    while estimate_seconds(texts(all_lines), rate) > hi:
        idx = next((i for i in range(len(all_lines) - 2, 1, -1)
                    if all_lines[i][0] in ("filler", "feature")), None)
        if idx is None:
            break
        reserve.insert(0, all_lines.pop(idx))

    def pack(items: List[Tuple[str, str]], start: int = 1) -> List[Dict[str, Any]]:
        return [{"id": start + i, "role": r, "text": t} for i, (r, t) in enumerate(items)]

    title1 = f"**{core}**"
    title2 = product.price_text or "이거 꿀템 발견"
    return {
        "title_lines": [title1, title2],
        "lines": pack(all_lines),
        "reserve": [{"role": r, "text": t} for r, t in reserve],
        "estimated_seconds": round(estimate_seconds(texts(all_lines), rate), 1),
        "product": {"name": product.name, "price": product.price, "price_text": product.price_text,
                    "url": product.url},
    }


def validate_script(script: Dict[str, Any]) -> List[str]:
    """규칙 위반 목록(빈 리스트면 통과)."""
    problems: List[str] = []
    for ln in script.get("lines") or []:
        n = visible_len(ln.get("text", ""))
        if not (LINE_MIN <= n <= LINE_MAX):
            problems.append(f"{ln.get('id')}번 줄 글자 수 {n} (8~13 필요): {ln.get('text')}")
    if not script.get("lines"):
        problems.append("대본이 비어 있습니다.")
    return problems


# ------------------------------------------------------------------ TTS
class TtsBackend(Protocol):
    async def synth(self, text: str, voice: str, rate: str, out_path: Path) -> None: ...


class EdgeTtsBackend:
    """edge-tts(무료). 설치 안 돼 있으면 안내 메시지."""

    async def synth(self, text: str, voice: str, rate: str, out_path: Path) -> None:
        try:
            import edge_tts  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("edge-tts 가 설치되어 있지 않습니다. 터미널: pip install edge-tts") from exc
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(out_path))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"edge-tts 생성 실패(인터넷 연결·보이스 이름 확인): {exc}") from exc
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("edge-tts 가 빈 파일을 만들었습니다.")


# silenceremove: 앞 무음 제거 -> 뒤집어서 같은 처리 -> 다시 뒤집기(= 뒤 무음 제거).
# 문장 중간의 쉼표 구간은 건드리지 않는다(stop_periods 를 쓰면 중간에서 잘린다).
SILENCE_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.02,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,"
    "afade=t=in:d=0.02,"
    "areverse,afade=t=in:d=0.006,"
    "aformat=sample_rates=48000:channel_layouts=mono"
)


def trim_silence(src: Path, dst: Path) -> float:
    """앞뒤 무음을 잘라 WAV 로 저장하고 실제 길이(초)를 돌려준다."""
    cp = _run([ffmpeg_exe(), "-hide_banner", "-y", "-loglevel", "error", "-i", str(src),
               "-af", SILENCE_FILTER, "-c:a", "pcm_s16le", str(dst)], timeout=120)
    if cp.returncode != 0 or not dst.exists():
        raise RenderError("무음 제거 실패: " + (cp.stderr or "").strip()[-300:])
    dur = probe_duration(dst)
    if dur < 0.15:
        raise RenderError("음성이 거의 비어 있습니다(무음 제거 후 길이 0).")
    return dur


def concat_audio(wavs: List[Path], dst: Path, gap: float = GAP_SECONDS) -> float:
    """WAV 들을 gap 초 간격으로 이어붙인다."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-loglevel", "error"]
    for w in wavs:
        cmd += ["-i", str(w)]
    labels = []
    for i in range(len(wavs)):
        labels.append(f"[{i}:a]")
    if gap > 0 and len(wavs) > 1:
        # 각 문장 뒤에 gap 초 무음을 붙인다(마지막 제외)
        parts = []
        for i in range(len(wavs)):
            tail = f",apad=pad_dur={gap}" if i < len(wavs) - 1 else ""
            parts.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=mono{tail}[a{i}]")
        fc = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(len(wavs))) + \
            f"concat=n={len(wavs)}:v=0:a=1[out]"
    else:
        fc = "".join(labels) + f"concat=n={len(wavs)}:v=0:a=1[out]"
    cmd += ["-filter_complex", fc, "-map", "[out]", "-c:a", "pcm_s16le", str(dst)]
    cp = _run(cmd, timeout=300)
    if cp.returncode != 0 or not dst.exists():
        raise RenderError("음성 이어붙이기 실패: " + (cp.stderr or "").strip()[-300:])
    return probe_duration(dst)


@dataclass
class VoiceTrack:
    audio_id: str
    path: str
    duration: float
    voice: str
    rate: str
    timeline: List[Dict[str, Any]] = field(default_factory=list)   # [{id,role,text,start,end,dur}]
    lines: List[Dict[str, Any]] = field(default_factory=list)      # 최종 사용 대사
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"audio_id": self.audio_id, "path": self.path, "duration": self.duration,
                "voice": self.voice, "rate": self.rate, "timeline": self.timeline,
                "lines": self.lines, "warnings": self.warnings}


def build_timeline(lines: List[Dict[str, Any]], durs: List[float], gap: float = GAP_SECONDS) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    t = 0.0
    for ln, d in zip(lines, durs):
        out.append({"id": ln.get("id"), "role": ln.get("role"), "text": ln["text"],
                    "start": round(t, 3), "end": round(t + d, 3), "dur": round(d, 3)})
        t += d + gap
    return out


async def _synth_lines(texts: List[str], voice: str, rate: str, backend: TtsBackend,
                       workdir: Path, cache: Dict[str, Tuple[Path, float]]) -> None:
    for text in texts:
        if text in cache:
            continue
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        raw = workdir / f"{key}.mp3"
        wav = workdir / f"{key}.wav"
        await backend.synth(text, voice, rate, raw)
        cache[text] = (wav, trim_silence(raw, wav))


def synthesize_script(script: Dict[str, Any], *, voice: Optional[str] = None,
                      rate: Optional[str] = None, backend: Optional[TtsBackend] = None,
                      gap: float = GAP_SECONDS,
                      target: Tuple[float, float] = TARGET_SECONDS) -> VoiceTrack:
    """대본 -> 문장별 TTS -> 무음 제거 -> 이어붙임. 20~25초를 벗어나면 대사를 더하거나 덜어 다시 맞춘다."""
    backend = backend or EdgeTtsBackend()
    voice_id = resolve_voice(voice)
    rate_s = normalize_rate(rate)
    lines = [dict(x) for x in script.get("lines") or []]
    reserve = [dict(x) for x in script.get("reserve") or []]
    if not lines:
        raise RenderError("대본이 비어 있습니다.")
    workdir = settings.TMP_DIR / f"tts_{store.new_id()}"
    workdir.mkdir(parents=True, exist_ok=True)
    cache: Dict[str, Tuple[Path, float]] = {}
    warnings: List[str] = []
    lo, hi = target

    async def run() -> None:
        nonlocal lines, reserve
        for _ in range(30):
            await _synth_lines([ln["text"] for ln in lines], voice_id, rate_s, backend, workdir, cache)
            durs = [cache[ln["text"]][1] for ln in lines]
            total = sum(durs) + gap * (len(durs) - 1)
            if total > hi:
                idx = next((i for i in range(len(lines) - 2, 1, -1)
                            if lines[i].get("role") in ("filler", "feature")), None)
                if idx is None:
                    warnings.append(f"대본이 {total:.1f}초로 길지만 더 덜어낼 대사가 없습니다.")
                    return
                reserve.insert(0, lines.pop(idx))
            elif total < lo and reserve:
                # 모자란 만큼 한 번에 여러 줄 추가(평균 문장 길이로 어림)
                avg = total / max(1, len(lines))
                n_add = max(1, min(len(reserve), int((lo + 0.5 - total) / max(0.5, avg + gap))))
                for _i in range(n_add):
                    nxt = reserve.pop(0)
                    pos = len(lines) - 1 if nxt.get("role") != "cta" else len(lines)
                    lines.insert(pos, nxt)
            else:
                if total < lo:
                    warnings.append(f"음성이 {total:.1f}초로 짧습니다(여분 대사 없음).")
                return
        warnings.append("20~25초에 맞추지 못하고 반복 횟수를 넘었습니다.")
        await _synth_lines([ln["text"] for ln in lines], voice_id, rate_s, backend, workdir, cache)

    try:
        asyncio.run(run())
        durs = [cache[ln["text"]][1] for ln in lines]
        for i, ln in enumerate(lines, start=1):          # 번호 재부여
            ln["id"] = i
        wavs = [cache[ln["text"]][0] for ln in lines]
        aid = store.new_id()
        dest = settings.AUDIO_DIR / f"{aid}.wav"
        measured = concat_audio(wavs, dest, gap)
        timeline = build_timeline(lines, durs, gap)
        expected = timeline[-1]["end"]
        if abs(measured - expected) > 0.06:             # 싱크 기준은 실제 파일 길이
            warnings.append(f"타임라인({expected:.2f}s)과 오디오({measured:.2f}s) 길이 차이가 큽니다.")
        meta = {"id": aid, "name": (lines[0]["text"] if lines else "tts")[:40], "path": str(dest),
                "duration": measured, "source": "tts-auto", "voice": voice_id, "rate": rate_s,
                "created_at": time.time()}
        store.save_audio_meta(meta)
        return VoiceTrack(audio_id=aid, path=str(dest), duration=measured, voice=voice_id, rate=rate_s,
                          timeline=timeline, lines=lines, warnings=warnings)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
