"""FFmpeg 렌더링 엔진.

UI가 보내는 프로젝트 JSON -> 9:16(1080x1920) MP4 / 썸네일 PNG.

코덱스에서 확장할 때 손대는 지점
  * compute_crop()      : 소스 -> 9:16 크롭 창(줌/팬)
  * mask_chain()        : 원본 자막/워터마크 가림(delogo / boxblur / 박스+한글자막)
  * decor_chain()       : 상단 제목바 + 형광펜 강조 + 금액/번호 배지
  * build_segment_chain(): 클립 한 조각의 필터 체인
  * build_audio_graph() : 원본음 / TTS / 외부 MP3 선택
  * render_video()      : 입력·필터·인코딩 오케스트레이션
  * render_thumbnail()  : 썸네일 카드 PNG(프레임 캡처 또는 업로드 이미지)

프로젝트 JSON(요약)
{
  "mode": "shopping" | "celebrity",
  "width": 1080, "height": 1920, "fps": 30,
  "clips": [{"clip_id": "...", "start": 0, "end": 5.2, "zoom": 1.2,
             "pan_x": 0.0, "pan_y": 0.0, "mute": false,
             "masks": [{"x":0.1,"y":0.8,"w":0.5,"h":0.1,
                        "mode":"delogo|blur|box", "text":"", "color":"0x000000"}]}],
  "title": {"enabled": true, "preset": "chomolip",
            "lines": ["**하나로** 끝", "이거 실화임"],
            "badge_text": "80만 원 세이브", "badge_number_text": "TOP 3",
            "font_scale": 1.0, "dim": 0.0},
  "badges": {"enabled": true, "color": "0xFFE100"},
  "audio": {"source": "clip|tts|file", "audio_id": "...", "volume": 1.0},
  "subtitles": {"enabled": true, "subtitle_id": "..."},
  "thumbnail": {"prepend": true, "prepend_duration": 1.0, "dim": 0.35,
                "source": {"type": "frame|image", "clip_id": "...", "time": 1.2,
                            "image_id": "...", "as_is": false}}
}
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import settings, store
from .presets import get_preset, measure_text, resolve_font


class RenderError(RuntimeError):
    """렌더 실패(사용자에게 그대로 보여줄 수 있는 메시지)."""


# ------------------------------------------------------------------ 기본 유틸
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _even(n: float) -> int:
    return max(2, int(round(n / 2.0)) * 2)


def ffmpeg_exe() -> str:
    return os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_exe() -> str:
    return os.getenv("FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"


def _exists(exe: str) -> bool:
    return bool(shutil.which(exe) or Path(exe).exists())


def media_tools_ready() -> Tuple[bool, str]:
    for exe, name in ((ffmpeg_exe(), "ffmpeg"), (ffprobe_exe(), "ffprobe")):
        if not _exists(exe):
            return False, f"{name}을(를) 찾을 수 없습니다. macOS: brew install ffmpeg"
    return True, "ok"


def _run(cmd: Sequence[str], timeout: int = 180) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RenderError(f"{cmd[0]} 실행 파일을 찾지 못했습니다. ffmpeg 설치를 확인하세요.") from exc


# ------------------------------------------------------------------ 미디어 정보
def probe_media(path: str | Path) -> Dict[str, Any]:
    cp = _run(
        [ffprobe_exe(), "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(path)]
    )
    if cp.returncode != 0:
        raise RenderError((cp.stderr or "ffprobe 실패").strip()[:400])
    info = json.loads(cp.stdout or "{}")
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise RenderError("영상 스트림이 없는 파일입니다. 오디오만 있는 파일은 TTS/배경음으로 넣어주세요.")

    w = int(v.get("width") or 0)
    h = int(v.get("height") or 0)

    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(float(sd["rotation"]))
            except Exception:
                rot = 0
    if not rot:
        try:
            rot = int(float((v.get("tags") or {}).get("rotate") or 0))
        except Exception:
            rot = 0
    if abs(rot) % 180 == 90:
        w, h = h, w

    dur = 0.0
    for src in (v.get("duration"), (info.get("format") or {}).get("duration")):
        try:
            dur = float(src)
            break
        except Exception:
            continue

    fps = 30.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = v.get(key) or ""
        try:
            num, den = raw.split("/")
            if float(den) > 0 and float(num) > 0:
                fps = float(num) / float(den)
                break
        except Exception:
            continue

    return {
        "duration": round(dur, 3),
        "width": w,
        "height": h,
        "fps": round(fps, 3),
        "rotation": rot,
        "has_audio": a is not None,
    }


def probe_duration(path: str | Path) -> float:
    cp = _run([ffprobe_exe(), "-v", "error", "-print_format", "json",
               "-show_format", str(path)])
    if cp.returncode != 0:
        raise RenderError((cp.stderr or "ffprobe 실패").strip()[:400])
    info = json.loads(cp.stdout or "{}")
    try:
        return round(float((info.get("format") or {}).get("duration") or 0), 3)
    except Exception:
        return 0.0


# ------------------------------------------------------------------ 크롭(줌/팬)
def compute_crop(src_w: int, src_h: int, out_w: int, out_h: int,
                 zoom: float, pan_x: float, pan_y: float) -> Tuple[int, int, int, int]:
    """9:16 창을 소스 안에서 잘라낼 좌표.

    zoom 1.0 = 원본에서 9:16 창을 최대한 크게, zoom 2.0 = 그 절반 영역을 확대.
    pan_x/pan_y -1..1 = 남는 여백 안에서 좌우/상하 이동.
    """
    if src_w <= 0 or src_h <= 0:
        raise RenderError("영상 크기를 읽지 못했습니다.")
    zoom = _clamp(float(zoom or 1.0), 1.0, 2.0)
    target_ar = out_w / out_h
    src_ar = src_w / src_h
    if src_ar >= target_ar:            # 가로가 넓다 -> 높이 기준
        base_w, base_h = src_h * target_ar, float(src_h)
    else:                              # 세로가 길다 -> 폭 기준
        base_w, base_h = float(src_w), src_w / target_ar

    cw = _even(min(base_w / zoom, float(src_w)))
    ch = _even(min(base_h / zoom, float(src_h)))
    slack_x = max(0, src_w - cw)
    slack_y = max(0, src_h - ch)
    x = int(round(slack_x * (0.5 + _clamp(pan_x, -1.0, 1.0) / 2.0)))
    y = int(round(slack_y * (0.5 + _clamp(pan_y, -1.0, 1.0) / 2.0)))
    x = int(_clamp(x, 0, slack_x))
    y = int(_clamp(y, 0, slack_y))
    return cw, ch, x, y


# ------------------------------------------------------------------ 필터 문자열 헬퍼
def _esc_path(p: str | Path) -> str:
    """filtergraph 안에 들어가는 경로 이스케이프."""
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _write_text(tmpdir: Path, name: str, text: str) -> Path:
    """drawtext는 textfile= 로 읽는다(한글/특수문자 이스케이프 사고 방지)."""
    f = tmpdir / f"{name}.txt"
    f.write_text(text, encoding="utf-8")
    return f


def _drawtext(tmpdir: Path, name: str, text: str, font_path: Optional[str],
              size: float, color: str, x: float, y: float,
              border: int = 0, border_color: str = "0x000000",
              alpha: float = 1.0) -> str:
    tf = _write_text(tmpdir, name, text)
    parts = [
        f"textfile={_esc_path(tf)}",
        f"fontsize={int(round(size))}",
        f"fontcolor={color}" + (f"@{alpha}" if alpha < 1.0 else ""),
    ]
    if font_path:
        parts.append(f"fontfile={_esc_path(font_path)}")
    if border:
        parts.append(f"borderw={int(border)}")
        parts.append(f"bordercolor={border_color}")
    parts.append(f"x={int(round(x))}")
    parts.append(f"y={int(round(y))}")
    return "drawtext=" + ":".join(parts)


def _drawbox(x: float, y: float, w: float, h: float, color: str, alpha: float = 1.0) -> str:
    return (f"drawbox=x={int(round(x))}:y={int(round(y))}:w={int(round(w))}:h={int(round(h))}"
            f":color={color}@{alpha}:t=fill")


def _dim_filter(dim: float) -> str:
    """배경 딤(0~0.9). RGB에서 곱연산으로 어둡게 한다.

    drawbox의 알파 혼합은 yuv420p에서 색이 회색으로 빠지기 때문에 lutrgb를 쓴다.
    """
    k = 1.0 - _clamp(float(dim or 0.0), 0.0, 0.9)
    expr = f"r=val*{k:.3f}:g=val*{k:.3f}:b=val*{k:.3f}"
    return f"format=gbrp,lutrgb={expr},format=yuv420p"


# ------------------------------------------------------------------ 가림막(원본 자막 제거)
def _mask_rect(mask: Dict[str, Any], out_w: int, out_h: int) -> Optional[Tuple[int, int, int, int]]:
    try:
        x = float(mask.get("x", 0)); y = float(mask.get("y", 0))
        w = float(mask.get("w", 0)); h = float(mask.get("h", 0))
    except Exception:
        return None
    if w <= 0.005 or h <= 0.005:
        return None
    px = int(round(_clamp(x, 0.0, 1.0) * out_w))
    py = int(round(_clamp(y, 0.0, 1.0) * out_h))
    pw = _even(_clamp(w, 0.0, 1.0) * out_w)
    ph = _even(_clamp(h, 0.0, 1.0) * out_h)
    pw = max(8, min(pw, out_w - px - 2))
    ph = max(8, min(ph, out_h - py - 2))
    px = int(_clamp(px, 0, out_w - pw - 2))
    py = int(_clamp(py, 0, out_h - ph - 2))
    return px, py, pw, ph


def _blur_radius(w: int, h: int) -> int:
    return int(_clamp(min(w, h) / 3.0, 8, 60))


def mask_chain(filters: List[str], cur: str, masks: List[Dict[str, Any]],
               out_w: int, out_h: int, tag: str, tmpdir: Path,
               font_path: Optional[str]) -> str:
    for j, mask in enumerate(masks or []):
        rect = _mask_rect(mask, out_w, out_h)
        if not rect:
            continue
        x, y, w, h = rect
        mode = str(mask.get("mode") or "blur").lower()
        nxt = f"[{tag}m{j}]"
        if mode == "blur":
            r = _blur_radius(w, h)
            filters.append(f"{cur}split=2[{tag}ba{j}][{tag}bb{j}]")
            filters.append(
                f"[{tag}bb{j}]crop={w}:{h}:{x}:{y},boxblur=luma_radius={r}:luma_power=4[{tag}bc{j}]"
            )
            filters.append(f"[{tag}ba{j}][{tag}bc{j}]overlay={x}:{y}{nxt}")
            cur = nxt
        elif mode == "box":
            filters.append(f"{cur}{_drawbox(x, y, w, h, str(mask.get('color') or '0x000000'), 1.0)}{nxt}")
            cur = nxt
            text = str(mask.get("text") or "").strip()
            if text:
                size = max(14.0, h * 0.5)
                tw = measure_text(text, font_path, size)
                if tw > w * 0.86 and tw > 0:      # 박스 밖으로 삐져나가면 줄인다
                    size = max(12.0, size * (w * 0.86) / tw)
                    tw = measure_text(text, font_path, size)
                tx = x + (w - tw) / 2.0
                ty = y + (h - size) / 2.0
                nxt2 = f"[{tag}mt{j}]"
                filters.append(f"{cur}{_drawtext(tmpdir, f'{tag}m{j}', text, font_path, size, '0xFFFFFF', tx, ty, border=2)}{nxt2}")
                cur = nxt2
        else:  # delogo: 주변 픽셀로 주변부를 뭉갠다
            dx = max(1, min(x, out_w - 4))
            dy = max(1, min(y, out_h - 4))
            dw = max(4, min(w, out_w - dx - 2))
            dh = max(4, min(h, out_h - dy - 2))
            filters.append(f"{cur}delogo=x={dx}:y={dy}:w={dw}:h={dh}{nxt}")
            cur = nxt
    return cur


# ------------------------------------------------------------------ 제목바 + 배지
def _parse_highlight(line: str) -> List[Tuple[str, bool]]:
    """'**키워드**' 구간을 (텍스트, 강조여부)로 나눈다."""
    parts = re.split(r"(\*\*.+?\*\*)", line)
    out: List[Tuple[str, bool]] = []
    for p in parts:
        if not p:
            continue
        if p.startswith("**") and p.endswith("**") and len(p) > 4:
            out.append((p[2:-2], True))
        else:
            out.append((p.replace("**", ""), False))
    return out


def _title_lines(project: Dict[str, Any]) -> List[str]:
    title = project.get("title") or {}
    raw = title.get("lines")
    if raw is None:
        raw = [title.get("line1") or "", title.get("line2") or ""]
    lines = [str(x).strip() for x in raw if str(x or "").strip()]
    return lines[:3]


def decor_chain(project: Dict[str, Any], out_w: int, out_h: int, tmpdir: Path,
                font_path: Optional[str], clip_index: int, total_clips: int,
                tag: str) -> Tuple[List[str], int]:
    """상단 제목바 + 형광펜 + 금액/번호 배지 필터 목록과 제목바 높이를 돌려준다."""
    filters: List[str] = []
    title = project.get("title") or {}
    preset = get_preset(title.get("preset"))
    scale = _clamp(float(title.get("font_scale") or 1.0), 0.5, 2.0)
    if not title.get("enabled", True):
        return filters, 0

    lines = _title_lines(project)
    font = float(preset["font_size"]) * scale
    margin = int(preset["margin_x"])
    pad_y = int(preset["pad_y"])
    gap = int(preset["line_gap"])
    usable = out_w - margin * 2

    # 자동 축소: 가장 긴 줄이 폭을 넘으면 폰트를 줄인다.
    if lines:
        widest = 0.0
        for line in lines:
            widest = max(widest, sum(measure_text(t, font_path, font) for t, _ in _parse_highlight(line)))
        if widest > usable > 0:
            font = max(26.0, font * usable / widest)

    line_h = font * 1.18
    bar_h = int(round(pad_y * 2 + line_h * len(lines) + gap * max(0, len(lines) - 1)))

    y = 0.0
    for idx, line in enumerate(lines):
        bars = preset["bars"]
        style = bars[min(idx, len(bars) - 1)]
        filters.append(_drawbox(0, y, out_w, line_h, style["bg"], float(style["alpha"])))
        segs = _parse_highlight(line)
        total_w = sum(measure_text(t, font_path, font) for t, _ in segs)
        x0 = float(margin) if preset["align"] == "left" else max(float(margin), (out_w - total_w) / 2.0)

        hl = preset.get("highlight")
        if hl:
            cx = x0
            for text, is_hl in segs:
                seg_w = measure_text(text, font_path, font)
                if is_hl:
                    filters.append(_drawbox(cx - font * 0.06, y + line_h * 0.14,
                                            seg_w + font * 0.12, line_h * 0.72,
                                            hl["bg"], float(hl["alpha"])))
                cx += seg_w

        plain = "".join(t for t, _ in segs) or line
        filters.append(_drawtext(tmpdir, f"{tag}line{idx}", plain, font_path, font,
                                 style["color"], x0, y + line_h * 0.08))
        y += line_h + gap

    if not lines:
        return filters, 0

    # ---- 배지 (금액 손실회피 / 제품 번호)
    badge_cfg = preset["badge"]
    badge_font = float(badge_cfg["font_size"]) * scale
    by = bar_h + 20
    amount = str(title.get("badge_text") or "").strip()
    num_text = str(title.get("badge_number_text") or "").strip()
    show_number = bool((project.get("badges") or {}).get("enabled", True)) and project.get("mode") == "shopping"
    if show_number and not num_text:
        num_text = f"{clip_index + 1}/{max(1, total_clips)}"

    if amount:
        tw = measure_text(amount, font_path, badge_font)
        bh = badge_font * 1.62
        bw = tw + badge_font * 0.9
        filters.append(_drawbox(margin, by, bw, bh, badge_cfg["color"], 1.0))
        filters.append(_drawtext(tmpdir, f"{tag}amount", amount, font_path, badge_font,
                                 badge_cfg["text_color"], margin + badge_font * 0.45,
                                 by + bh * 0.16))
    if num_text:
        tw = measure_text(num_text, font_path, badge_font)
        bh = badge_font * 1.62
        bw = tw + badge_font * 0.9
        x = out_w - margin - bw
        filters.append(_drawbox(x, by, bw, bh, badge_cfg["color"], 1.0))
        filters.append(_drawtext(tmpdir, f"{tag}num", num_text, font_path, badge_font,
                                 badge_cfg["text_color"], x + badge_font * 0.45, by + bh * 0.16))

    # ---- 출처 태그 (셀럽/벤치마크 모드)
    credit = str(title.get("credit") or (project.get("credit") or {}).get("text") or "").strip()
    if credit:
        size = 40.0 * scale
        tw = measure_text(credit, font_path, size)
        ch = size * 1.7
        filters.append(_drawbox(out_w - tw - 64, out_h - ch - 90, tw + 48, ch, "0x000000", 0.55))
        filters.append(_drawtext(tmpdir, f"{tag}credit", credit, font_path, size,
                                 "0xFFFFFF", out_w - tw - 40, out_h - ch - 90 + ch * 0.16))

    return filters, bar_h


# ------------------------------------------------------------------ 클립 한 조각
def build_segment_chain(idx: int, meta: Dict[str, Any], clip: Dict[str, Any],
                        project: Dict[str, Any], out_w: int, out_h: int, fps: int,
                        tmpdir: Path, font_path: Optional[str],
                        clip_index: int, total_clips: int) -> Tuple[List[str], str]:
    filters: List[str] = []
    cw, ch, cx, cy = compute_crop(meta["width"], meta["height"], out_w, out_h,
                                  clip.get("zoom", 1.0), clip.get("pan_x", 0.0), clip.get("pan_y", 0.0))
    filters.append(
        f"[{idx}:v]fps={fps},crop={cw}:{ch}:{cx}:{cy},"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[vs{idx}]"
    )
    cur = mask_chain(filters, f"[vs{idx}]", clip.get("masks") or [], out_w, out_h,
                     f"c{idx}", tmpdir, font_path)

    dim = _clamp(float((project.get("title") or {}).get("dim") or 0.0), 0.0, 0.9)
    if dim > 0:  # 본문을 어둡게(제목바 가독성). 미리보기·썸네일과 동일하게 적용된다.
        filters.append(f"{cur}{_dim_filter(dim)}[dim{idx}]")
        cur = f"[dim{idx}]"

    decor, _bar_h = decor_chain(project, out_w, out_h, tmpdir, font_path,
                                clip_index, total_clips, f"c{idx}")
    for k, item in enumerate(decor):
        nxt = f"[vde{idx}_{k}]"
        filters.append(f"{cur}{item}{nxt}")
        cur = nxt

    filters.append(f"{cur}format=yuv420p[vf{idx}]")
    return filters, f"[vf{idx}]"


# ------------------------------------------------------------------ 썸네일 카드 PNG
def render_thumbnail(project: Dict[str, Any], out_path: Path,
                     base_frame: Optional[Dict[str, Any]] = None) -> Path:
    """썸네일 1080x1920 PNG 한 장.

    base: 특정 클립의 특정 타임스탬프 프레임(기본) 또는 업로드한 이미지.
    """
    ok, msg = media_tools_ready()
    if not ok:
        raise RenderError(msg)
    thumb = project.get("thumbnail") or {}
    src = thumb.get("source") or {}
    out_w = int(project.get("width") or settings.OUT_W)
    out_h = int(project.get("height") or settings.OUT_H)
    font_path = resolve_font()
    tmpdir = Path(tempfile.mkdtemp(prefix="thumb_", dir=str(settings.TMP_DIR)))
    filters: List[str] = []

    base_frame = base_frame or {}
    kind = src.get("type") or "frame"
    cmd: List[str] = [ffmpeg_exe(), "-hide_banner", "-y", "-loglevel", "error"]

    if kind == "image" and src.get("image_id"):
        img = store.get_image(src["image_id"])
        if not img:
            raise RenderError("업로드한 썸네일 이미지를 찾을 수 없습니다.")
        cmd += ["-i", str(img["path"])]
        filters.append(
            f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},setsar=1[v0]"
        )
        cur = "[v0]"
    else:
        cid = src.get("clip_id") or base_frame.get("clip_id")
        cmeta = store.clip_meta(cid)
        if not cmeta:
            raise RenderError("썸네일로 캡처할 클립이 없습니다. 클립을 올리거나 이미지를 선택하세요.")
        info = probe_media(cmeta["path"])
        t = float(src.get("time") if src.get("time") is not None else info["duration"] * 0.3)
        t = _clamp(t, 0.0, max(0.0, info["duration"] - 0.05))
        clip = next((c for c in (project.get("clips") or []) if c.get("clip_id") == cid), {}) or {}
        cmd += ["-ss", f"{t:.3f}", "-i", str(cmeta["path"])]
        cw, ch, cx, cy = compute_crop(info["width"], info["height"], out_w, out_h,
                                      clip.get("zoom", 1.0), clip.get("pan_x", 0.0), clip.get("pan_y", 0.0))
        filters.append(f"[0:v]crop={cw}:{ch}:{cx}:{cy},scale={out_w}:{out_h}:flags=lanczos,setsar=1[v0]")
        cur = mask_chain(filters, "[v0]", clip.get("masks") or [], out_w, out_h,
                         "th", tmpdir, font_path)

    dim = _clamp(float(thumb.get("dim", (project.get("title") or {}).get("dim") or 0.0) or 0.0), 0.0, 0.9)
    if dim > 0:
        filters.append(f"{cur}{_dim_filter(dim)}[vdim]")
        cur = "[vdim]"

    if not src.get("as_is"):
        decor, _ = decor_chain(project, out_w, out_h, tmpdir, font_path, 0,
                               max(1, len(project.get("clips") or [])), "th")
        for k, item in enumerate(decor):
            nxt = f"[vth{k}]"
            filters.append(f"{cur}{item}{nxt}")
            cur = nxt

    filters.append(f"{cur}format=rgb24[vout]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]",
            "-frames:v", "1", "-q:v", "2", str(out_path)]
    cp = _run(cmd, timeout=300)
    if cp.returncode != 0 or not out_path.exists():
        raise RenderError("썸네일 생성 실패: " + (cp.stderr or "").strip()[-500:])
    shutil.rmtree(tmpdir, ignore_errors=True)
    return out_path


# ------------------------------------------------------------------ 본 렌더
def render_video(project: Dict[str, Any], out_path: Path,
                 progress: Optional[Callable[[float, str], None]] = None) -> Dict[str, Any]:
    ok, msg = media_tools_ready()
    if not ok:
        raise RenderError(msg)

    def report(pct: float, note: str = "") -> None:
        if progress:
            progress(_clamp(pct, 0.0, 1.0), note)

    out_w = int(project.get("width") or settings.OUT_W)
    out_h = int(project.get("height") or settings.OUT_H)
    fps = int(project.get("fps") or settings.OUT_FPS)
    clips = [_c for _c in (project.get("clips") or []) if _c.get("clip_id")]
    if not clips:
        raise RenderError("렌더링할 클립이 없습니다. 영상을 먼저 올려주세요.")

    font_path = resolve_font()
    tmpdir = Path(tempfile.mkdtemp(prefix="render_", dir=str(settings.TMP_DIR)))

    # ---------- 1) 구간 목록(썸네일 카드 선행 -> 클립들)
    segments: List[Dict[str, Any]] = []
    thumb = project.get("thumbnail") or {}
    if thumb.get("prepend"):
        dur = _clamp(float(thumb.get("prepend_duration") or 1.0), 0.2, 5.0)
        thumb_png = render_thumbnail(project, tmpdir / "thumb_card.png")
        segments.append({"kind": "thumb", "seg": dur, "path": str(thumb_png), "has_audio": False})
        report(0.05, "썸네일 카드 생성")

    resolved: List[Dict[str, Any]] = []
    for c in clips:
        meta = store.clip_meta(c.get("clip_id"))
        if meta is None:
            raise RenderError(f"클립을 찾을 수 없습니다: {c.get('clip_id')}")
        info = probe_media(meta["path"])
        dur = float(info["duration"] or 0.0)
        start = _clamp(float(c.get("start") or 0.0), 0.0, max(0.0, dur - 0.1))
        end = float(c.get("end") if c.get("end") is not None else dur)
        end = _clamp(end, start + 0.2, dur) if dur else end
        seg = max(0.2, end - start)
        resolved.append({
            "clip": c, "meta": meta, "info": info,
            "start": start, "seg": seg,
            "path": str(meta["path"]), "has_audio": bool(info["has_audio"]),
        })
    segments.extend(resolved)

    total = sum(s["seg"] for s in segments)
    if total <= 0:
        raise RenderError("총 길이가 0초입니다. 클립 구간을 확인하세요.")

    # ---------- 2) 입력
    inputs: List[str] = []
    for s in segments:
        if s.get("kind") == "thumb":
            inputs += ["-loop", "1", "-t", f"{s['seg']:.3f}", "-i", s["path"]]
        else:
            inputs += ["-ss", f"{s['start']:.3f}", "-t", f"{s['seg']:.3f}", "-i", s["path"]]

    audio_cfg = project.get("audio") or {}
    audio_src = (audio_cfg.get("source") or "clip").lower()
    ext_audio_path: Optional[str] = None
    if audio_src in ("tts", "file"):
        ameta = store.audio_meta(audio_cfg.get("audio_id"))
        if not ameta:
            raise RenderError("오디오 파일을 찾을 수 없습니다. TTS를 다시 만들거나 파일을 올려주세요.")
        ext_audio_path = ameta["path"]

    silent_idx: Dict[int, int] = {}
    if not ext_audio_path:
        nxt_idx = len(segments)
        for i, s in enumerate(segments):
            if not s.get("has_audio"):
                inputs += ["-f", "lavfi", "-t", f"{s['seg']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
                silent_idx[i] = nxt_idx
                nxt_idx += 1
    ext_idx: Optional[int] = None
    if ext_audio_path:
        ext_idx = len(segments) + len(silent_idx)
        inputs += ["-i", ext_audio_path]

    # ---------- 3) 비디오 체인
    filters: List[str] = []
    labels: List[str] = []
    clip_no = 0
    for i, s in enumerate(segments):
        if s.get("kind") == "thumb":
            filters.append(
                f"[{i}:v]scale={out_w}:{out_h},setsar=1,fps={fps},format=yuv420p[vf{i}]"
            )
            labels.append(f"[vf{i}]")
            continue
        chain, label = build_segment_chain(i, s["info"], s["clip"], project, out_w, out_h, fps,
                                          tmpdir, font_path, clip_no, len(resolved))
        filters.extend(chain)
        labels.append(label)
        clip_no += 1

    if len(labels) == 1:
        vcat = labels[0]
    else:
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[vcat]")
        vcat = "[vcat]"

    # ---------- 4) 자막
    sub_cfg = project.get("subtitles") or {}
    vout = vcat
    if sub_cfg.get("enabled", True) and sub_cfg.get("subtitle_id"):
        sp = store.subtitle_path(sub_cfg.get("subtitle_id"))
        if sp:
            filters.append(f"{vcat}ass=filename={_esc_path(sp)}[vsub]")
            vout = "[vsub]"

    # ---------- 5) 오디오 체인
    aout: Optional[str] = None
    if ext_audio_path and ext_idx is not None:
        vol = _clamp(float(audio_cfg.get("volume") or 1.0), 0.0, 3.0)
        filters.append(
            f"[{ext_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={vol},apad[audext]"
        )
        aout = "[audext]"
    else:
        alabels: List[str] = []
        for i, s in enumerate(segments):
            d = f"{s['seg']:.3f}"
            if i in silent_idx:
                filters.append(
                    f"[{silent_idx[i]}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"atrim=0:{d},asetpts=PTS-STARTPTS[aud{i}]"
                )
            else:
                vol = 0.0 if (s.get("clip") or {}).get("mute") else 1.0
                filters.append(
                    f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"asetpts=PTS-STARTPTS,volume={vol},apad,atrim=0:{d}[aud{i}]"
                )
            alabels.append(f"[aud{i}]")
        if len(alabels) == 1:
            aout = alabels[0]
        else:
            filters.append("".join(alabels) + f"concat=n={len(alabels)}:v=0:a=1[aout]")
            aout = "[aout]"

    # ---------- 6) 실행
    preset = str(project.get("x264_preset") or "veryfast")
    crf = int(project.get("crf") or 20)
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats"] + inputs + ["-filter_complex", ";".join(filters),
                                                          "-map", vout]
    if aout:
        cmd += ["-map", aout]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", "-shortest", "-t", f"{total:.3f}",
        str(out_path),
    ]

    report(0.12, "인코딩 시작")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    tail: List[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        m = re.match(r"out_time_ms=(\d+)", line)
        if m and total > 0:
            sec = int(m.group(1)) / 1_000_000.0
            report(0.12 + 0.86 * _clamp(sec / total, 0.0, 1.0), "인코딩")
        elif line and not line.startswith("frame=") and "=" not in line:
            tail.append(line)
            tail = tail[-25:]
        elif line and ("error" in line.lower() or "invalid" in line.lower()):
            tail.append(line)
            tail = tail[-25:]
    proc.wait()
    shutil.rmtree(tmpdir, ignore_errors=True)
    if proc.returncode != 0 or not out_path.exists():
        raise RenderError("FFmpeg 렌더 실패:\n" + "\n".join(tail[-12:])[:1200])
    report(1.0, "완료")
    return {"path": str(out_path), "duration": round(total, 2),
            "width": out_w, "height": out_h, "segments": len(segments)}
