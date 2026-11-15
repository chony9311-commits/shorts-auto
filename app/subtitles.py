"""자막(.ass) 생성.

faster-whisper로 뽑은 구간/단어 타임스탬프를 9:16 화면용 ASS로 굽는다.
ASS는 libass가 폰트를 찾아 그리므로, macOS면 'Apple SD Gothic Neo'가 잡힌다.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
