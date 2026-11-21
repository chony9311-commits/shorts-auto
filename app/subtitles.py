"""자막(.ass) 생성.

faster-whisper로 뽑은 구간/단어 타임스탬프를 9:16 화면용 ASS로 굽는다.
ASS는 libass가 폰트를 찾아 그리므로, macOS면 'Apple SD Gothic Neo'가 잡힌다.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import settings

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: None

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,{outline},0,2,{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _clean(text: str) -> str:
    return (text or "").replace("\n", "\\N").replace("{", "(").replace("}", ")").strip()


def build_ass(segments: Iterable[Dict[str, Any]], out_path: Path,
              font: str = "Apple SD Gothic Neo", size: int = 62,
              outline: int = 4, margin_v: int = 220, uppercase: bool = False) -> Path:
    """segments: [{"start": 0.0, "end": 1.2, "text": "..."}]"""
    header = ASS_HEADER.format(font=font, size=int(size), bold=-1, outline=int(outline),
                              margin_l=70, margin_r=70, margin_v=int(margin_v))
    lines: List[str] = [header]
    for seg in segments:
        text = _clean(str(seg.get("text") or ""))
        if not text:
            continue
        if uppercase:
            text = text.upper()
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or start + 1.0)
        if end <= start:
            end = start + 0.4
        lines.append(
            f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{{\\fad(60,60)}}{text}"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def parse_srt(raw: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for block in re.split(r"\r?\n\r?\n", (raw or "").strip()):
        block = block.strip()
        if not block:
            continue
        chunks = block.splitlines()
        m = None
        idx = 0
        for i, ln in enumerate(chunks[:3]):
            m = _SRT_TIME.search(ln)
            if m:
                idx = i
                break
        if not m:
            continue
        g = m.groups()

        def _sec(h: str, mi: str, s: str, ms: str) -> float:
            return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0

        start = _sec(g[0], g[1], g[2], g[3])
        end = _sec(g[4], g[5], g[6], g[7])
        text = " ".join(chunks[idx + 1:]).strip()
        if text:
            out.append({"start": start, "end": end, "text": text})
    return out


def srt_to_ass(srt_path: Path, out_path: Path, **kw: Any) -> Path:
    raw = srt_path.read_text(encoding="utf-8", errors="ignore")
    return build_ass(parse_srt(raw), out_path, **kw)


def transcribe_to_ass(audio_path: Path, out_path: Path,
                      model_size: str = "small", language: str = "ko",
                      word_level: bool = False, **kw: Any) -> Dict[str, Any]:
    """faster-whisper로 타임스탬프 추출 -> ASS 저장.

    word_level=True면 단어 단위로 1~3단어씩 끊어 숏폼 리듬을 만든다.
    """
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "자동 자막에는 faster-whisper가 필요합니다.\n"
            "터미널에서: pip install faster-whisper\n"
            "(설치 없이도 .srt 파일을 직접 올려 자막을 넣을 수 있습니다.)"
        ) from exc

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    seg_iter, info = model.transcribe(
        str(audio_path), language=language, beam_size=5,
        word_timestamps=word_level, vad_filter=True,
    )

    segments: List[Dict[str, Any]] = []
    words: List[Dict[str, Any]] = []
    for seg in seg_iter:
        segments.append({"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip()})
        for w in (getattr(seg, "words", None) or []):
            words.append({"start": float(w.start), "end": float(w.end), "text": w.word.strip()})

    if word_level and words:
        grouped: List[Dict[str, Any]] = []
        buf: List[Dict[str, Any]] = []
        for w in words:
            buf.append(w)
            text_len = sum(len(x["text"]) for x in buf)
            if text_len >= 9 or (buf and w is not words[-1] and buf[0]["start"] is not None
                                 and w["end"] - buf[0]["start"] > 1.6):
                grouped.append({"start": buf[0]["start"], "end": buf[-1]["end"],
                                "text": " ".join(x["text"] for x in buf)})
                buf = []
        if buf:
            grouped.append({"start": buf[0]["start"], "end": buf[-1]["end"],
                            "text": " ".join(x["text"] for x in buf)})
        if grouped:
            segments = grouped

    build_ass(segments, out_path, **kw)
    return {"segments": len(segments), "language": getattr(info, "language", language),
            "duration": float(getattr(info, "duration", 0.0) or 0.0)}


# ====================================================================== 릴스/쇼츠 중앙 자막
# reels_center: 화면 세로 55% 지점, 굵은 고딕 흰 글자 + 두꺼운 검은 외곽선 + 옅은 그림자.
# 한 번에 1줄(어절 1~2개)만 짧게 띄우고 지나간다.
SUBTITLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "default": {"label": "기본 (하단)", "desc": "화면 아래쪽 흰 글씨 + 검은 외곽선"},
    "reels_center": {
        "label": "릴스 중앙",
        "desc": "정중앙 아래(55%) · 굵은 고딕 72px · 외곽선 5 · 그림자 2 · 1줄씩 짧게",
        "size": 72, "outline": 5, "shadow": 2, "y_ratio": 0.55, "chunk_chars": 9,
    },
    "reels_clean": {
        "label": "깔끔 (노랑 강조)",
        "desc": "정중앙 · 굵은 고딕 흰 글씨 + 얇은 외곽선 + 부드러운 그림자 · 핵심어는 노랑·크게",
        "size": 72, "outline": 2, "shadow": 3, "y_ratio": 0.55, "chunk_chars": 9, "emph_scale": 1.25,
    },
}
DEFAULT_AUTO_SUBTITLE_PRESET = "reels_clean"

REELS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: None

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Reels,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# 굵은 고딕 후보(앞이 우선). Pretendard Bold -> Noto Sans KR Bold -> 시스템 고딕
_FONT_NAMES = ["Pretendard", "Noto Sans KR", "Noto Sans CJK KR", "Apple SD Gothic Neo",
               "NanumGothic", "Malgun Gothic"]
_FONT_FILE_HINTS = {
    "Pretendard": ["Pretendard"], "Noto Sans KR": ["NotoSansKR"], "Noto Sans CJK KR": ["NotoSansCJK"],
    "Apple SD Gothic Neo": ["AppleSDGothicNeo"], "NanumGothic": ["NanumGothic"],
    "Malgun Gothic": ["malgun"],
}
_font_pick_cache: Optional[Tuple[str, Optional[str]]] = None


def fonts_dir_if_any() -> Optional[Path]:
    """fonts/ 에 폰트 파일이 있으면 libass 가 거기서도 찾게 한다(Pretendard 를 넣어두는 용도)."""
    d = settings.FONT_DIR
    if d.exists() and any(p.suffix.lower() in (".ttf", ".otf", ".ttc") for p in d.iterdir()):
        return d
    return None


def pick_sub_font() -> Tuple[str, Optional[Path]]:
    """(ASS 폰트 이름, fontsdir). SHORTS_SUB_FONT 환경변수로 강제 지정 가능."""
    global _font_pick_cache
    env = os.getenv("SHORTS_SUB_FONT")
    if env:
        return env, fonts_dir_if_any()
    fdir = fonts_dir_if_any()
    if fdir:
        names = " ".join(p.name for p in fdir.iterdir())
        for fam in _FONT_NAMES:
            if any(h.lower() in names.lower() for h in _FONT_FILE_HINTS.get(fam, [fam])):
                return fam, fdir
    if _font_pick_cache is None:
        installed = ""
        if shutil.which("fc-list"):
            try:
                installed = subprocess.run(["fc-list", ":lang=ko", "family"], capture_output=True,
                                           text=True, timeout=10).stdout
            except Exception:
                installed = ""
        chosen = None
        for fam in _FONT_NAMES:
            if fam.lower() in installed.lower():
                chosen = fam
                break
        if not chosen:
            chosen = "Apple SD Gothic Neo" if sys.platform == "darwin" else (
                "Malgun Gothic" if sys.platform.startswith("win") else "Noto Sans CJK KR")
        _font_pick_cache = (chosen, None)
    return _font_pick_cache[0], fdir


def chunk_eojeol(text: str, max_chars: int = 9) -> List[str]:
    """문장을 어절 단위로 묶어 한 줄에 max_chars(공백 제외) 이하가 되게 쪼갠다.

    1~2어절씩 짧게 치고 지나가는 느낌. 한 어절이 max_chars 보다 길면 그 어절만 한 줄로 둔다.
    """
    words = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    chunks: List[str] = []
    cur: List[str] = []
    for w in words:
        trial = cur + [w]
        if cur and (sum(len(x) for x in trial) > max_chars or len(trial) > 2):
            chunks.append(" ".join(cur))
            cur = [w]
        else:
            cur = trial
    if cur:
        chunks.append(" ".join(cur))
    # 끝에 한 글자짜리가 외톨이로 남으면 앞과 합친다
    if len(chunks) > 1 and len(chunks[-1].replace(" ", "")) <= 2:
        tail = chunks.pop()
        chunks[-1] = chunks[-1] + " " + tail
    return chunks


def reels_events(segments: Iterable[Dict[str, Any]], chunk_chars: int = 9) -> List[Dict[str, Any]]:
    """문장 구간 -> 어절 묶음 이벤트. 문장 안에서 글자 수에 비례해 시간을 나눈다(빈틈 없이)."""
    out: List[Dict[str, Any]] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or start + 1.0)
        if end <= start:
            end = start + 0.4
        chunks = chunk_eojeol(text, chunk_chars)
        weights = [max(1, len(c.replace(" ", ""))) for c in chunks]
        total_w = float(sum(weights))
        t = start
        for i, (c, w) in enumerate(zip(chunks, weights)):
            t2 = end if i == len(chunks) - 1 else t + (end - start) * w / total_w
            out.append({"start": t, "end": t2, "text": c})
            t = t2
    return out


def build_reels_center_ass(segments: Iterable[Dict[str, Any]], out_path: Path,
                           font: Optional[str] = None, size: int = 72, outline: int = 5,
                           shadow: int = 2, y_ratio: float = 0.55, chunk_chars: int = 9,
                           pop: bool = True) -> Path:
    """reels_center 스타일 ASS. segments: [{"start","end","text"}] (문장 단위 타임라인)."""
    size = int(max(68, min(74, size)))                  # 요청 범위 68~74px
    font = font or pick_sub_font()[0]
    header = REELS_HEADER.format(font=font, size=size, outline=int(outline), shadow=int(shadow))
    y = int(round(1920 * max(0.3, min(0.8, y_ratio))))
    lines: List[str] = [header]
    for ev in reels_events(segments, chunk_chars):
        text = _clean(ev["text"])
        anim = "\\fscx110\\fscy110\\t(0,90,\\fscx100\\fscy100)\\fad(30,20)" if pop else ""
        lines.append(
            f"Dialogue: 0,{_ts(ev['start'])},{_ts(ev['end'])},Reels,,0,0,0,,"
            f"{{\\an5\\pos(540,{y}){anim}}}{text}"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


# 노랑 강조 대상(자동): 숫자·가격, 감탄/핵심 키워드
_EMPH_WORDS = ("진짜", "미쳤", "대박", "무조건", "꿀템", "손해", "실화", "공짜", "최고", "끝", "충격", "할인", "세일")
_EMPH_YELLOW = "&H0000E1FF&"          # #FFE100 (ASS 는 BGR 순서)


def is_emphasis(chunk: str) -> bool:
    return bool(re.search(r"\d", chunk)) or any(w in chunk for w in _EMPH_WORDS)


def build_reels_clean_ass(segments: Iterable[Dict[str, Any]], out_path: Path, font: Optional[str] = None,
                          size: int = 72, outline: int = 2, shadow: int = 3, y_ratio: float = 0.55,
                          chunk_chars: int = 9, emph_scale: float = 1.25) -> Path:
    """깔끔 버전: 얇은 외곽선 + 부드러운 그림자, 핵심어(숫자·키워드·**표시**)는 노랑 + 크게."""
    size = int(max(68, min(76, size)))
    font = font or pick_sub_font()[0]
    header = REELS_HEADER.format(font=font, size=size, outline=int(outline), shadow=int(shadow))
    header = header.replace("&H78000000", "&H96000000")        # 그림자 색(반투명 검정)
    y = int(round(1920 * max(0.3, min(0.8, y_ratio))))
    lines: List[str] = [header]
    for ev in reels_events(segments, chunk_chars):
        raw = ev["text"]
        marked = "**" in raw
        text = _clean(raw.replace("**", ""))
        emph = marked or is_emphasis(text)
        tag = f"\\1c{_EMPH_YELLOW}\\fs{int(size * emph_scale)}" if emph else ""
        lines.append(
            f"Dialogue: 0,{_ts(ev['start'])},{_ts(ev['end'])},Reels,,0,0,0,,"
            f"{{\\an5\\pos(540,{y})\\blur1\\fad(40,30){tag}}}{text}"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def build_preset_ass(preset: str, segments: Iterable[Dict[str, Any]], out_path: Path, **kw: Any) -> Path:
    """프리셋 이름으로 ASS 를 만든다('default' 는 기존 하단 자막)."""
    if preset == "reels_clean":
        opts = {k: v for k, v in SUBTITLE_PRESETS["reels_clean"].items() if k in
                ("size", "outline", "shadow", "y_ratio", "chunk_chars", "emph_scale")}
        opts.update(kw)
        return build_reels_clean_ass(segments, out_path, **opts)
    if preset == "reels_center":
        opts = {k: v for k, v in SUBTITLE_PRESETS["reels_center"].items() if k in
                ("size", "outline", "shadow", "y_ratio", "chunk_chars")}
        opts.update(kw)
        return build_reels_center_ass(segments, out_path, **opts)
    return build_ass(segments, out_path, **kw)
