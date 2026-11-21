"""글씨만 얹는 썸네일(제목바·배지 없음). 영상 한 프레임 위에 큰 글씨 + 부드러운 그림자.

`**핵심어**` 는 노랑, 나머지는 흰색. 1080x1920 PNG.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .presets import resolve_font
from .render import ffmpeg_exe

W, H = 1080, 1920
YELLOW = (255, 225, 0)
WHITE = (255, 255, 255)


def grab_frame(video: Path, t: float, dst: Path) -> Path:
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                    "-frames:v", "1", str(dst)], check=True)
    return dst


def _parts(line: str) -> List[Tuple[str, bool]]:
    out: List[Tuple[str, bool]] = []
    for i, seg in enumerate(re.split(r"\*\*", line)):
        if seg:
            out.append((seg, i % 2 == 1))
    return out


def make_text_thumbnail(base_image: Path, lines: List[str], out_path: Path, dim: float = 0.28,
                        font_path: Optional[str] = None, y_ratio: float = 0.42,
                        base_size: int = 128) -> Path:
    """lines: 최대 3줄. 줄이 폭을 넘으면 글자 크기를 줄인다."""
    img = Image.open(base_image).convert("RGB")
    scale = max(W / img.width, H / img.height)
    img = img.resize((int(img.width * scale + 1), int(img.height * scale + 1)), Image.LANCZOS)
    img = img.crop(((img.width - W) // 2, (img.height - H) // 2, (img.width - W) // 2 + W, (img.height - H) // 2 + H))
    if dim > 0:
        img = ImageEnhance.Brightness(img).enhance(1 - dim)
    font_path = font_path or resolve_font()
    lines = [ln for ln in lines if ln.strip()][:3]

    def font_for(size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()

    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd, td = ImageDraw.Draw(shadow), ImageDraw.Draw(text_layer)
    heights, sizes = [], []
    for ln in lines:
        size = base_size
        while size > 48:
            width = sum(font_for(size).getlength(t) for t, _ in _parts(ln))
            if width <= W - 140:
                break
            size -= 4
        sizes.append(size)
        heights.append(int(size * 1.25))
    y = int(H * y_ratio - sum(heights) / 2)
    for ln, size, hgt in zip(lines, sizes, heights):
        f = font_for(size)
        total = sum(f.getlength(t) for t, _ in _parts(ln))
        x = (W - total) / 2
        for txt, emph in _parts(ln):
            color = YELLOW if emph else WHITE
            sd.text((x + 4, y + 6), txt, font=f, fill=(0, 0, 0, 200), stroke_width=3, stroke_fill=(0, 0, 0, 200))
            td.text((x, y), txt, font=f, fill=color + (255,), stroke_width=3, stroke_fill=(0, 0, 0, 255))
            x += f.getlength(txt)
        y += hgt
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))
    img = Image.alpha_composite(Image.alpha_composite(img.convert("RGBA"), shadow), text_layer).convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path
