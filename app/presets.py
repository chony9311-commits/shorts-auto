"""썸네일 / 상단 제목바 프리셋.

프리셋은 "선택"일 뿐이며 UI에서 언제든 바꿀 수 있다. 각 프리셋은
  * bars      : 줄마다 깔리는 띠(배경색/투명도/글자색)
  * highlight : 핵심 키워드 형광펜 색
  * badge     : 금액/번호 배지 색
  * css       : 미리보기(HTML)용 같은 값
을 담는다. FFmpeg 색은 0xRRGGBB, CSS 색은 #RRGGBB.

주의: 좌표/크기 계산은 render.py의 decor_chain()이 한다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import settings

# 한국어 폰트 후보. fonts/ 폴더에 ttf를 넣으면 그게 1순위.
FONT_CANDIDATES = [
    str(settings.FONT_DIR / "NanumGothicBold.ttf"),
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/Library/Fonts/NanumGothicBold.ttf",
    os.path.expanduser("~/Library/Fonts/NanumGothicBold.ttf"),
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
]

_font_cache: Optional[str] = None
_font_checked = False


def list_font_files() -> list[str]:
    seen: list[str] = []
    if settings.FONT_DIR.exists():
        for p in sorted(settings.FONT_DIR.iterdir()):
            if p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                seen.append(str(p))
    for cand in FONT_CANDIDATES:
        if cand not in seen and Path(cand).exists():
            seen.append(cand)
    return seen


def resolve_font() -> Optional[str]:
    """렌더에 쓸 한국어 폰트 경로."""
    global _font_cache, _font_checked
    if _font_checked:
        return _font_cache
    _font_checked = True
    _font_cache = None
    env = os.getenv("SHORTS_FONT")
    if env and Path(env).exists():
        _font_cache = env
        return _font_cache
    for cand in FONT_CANDIDATES:
        if Path(cand).exists():
            _font_cache = cand
            break
    return _font_cache


def measure_text(text: str, font_path: Optional[str], size: float) -> float:
    """글자 폭(px) 측정. Pillow가 있으면 실제 폰트로, 없으면 근사."""
    if not text:
        return 0.0
    if font_path and Path(font_path).exists():
        try:
            from PIL import ImageFont  # type: ignore

            font = ImageFont.truetype(str(font_path), int(max(8, size)))
            return float(font.getlength(text))
        except Exception:
            pass
    total = 0.0
    for ch in text:
        if ord(ch) > 0x2E80:      # 한글/한자/가나 = 전각
            total += 1.0
        elif ch == " ":
            total += 0.32
        else:
            total += 0.56
    return total * size


TITLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "chomolip": {
        "key": "chomolip",
        "label": "@초몰입형",
        "desc": "노랑+흰색 2줄 굵은 제목바 · 핵심 키워드 형광펜 강조",
        "bars": [
            {"bg": "0xFFE100", "alpha": 1.0, "color": "0x111111"},
            {"bg": "0xFFFFFF", "alpha": 1.0, "color": "0x111111"},
        ],
        "font_size": 86,
        "line_gap": 6,
        "pad_y": 26,
        "margin_x": 44,
        "align": "left",
        "highlight": {"bg": "0x111111", "alpha": 0.16},
        "badge": {"color": "0xFF3B30", "text_color": "0xFFFFFF", "font_size": 58},
        "css": {
            "bars": [{"bg": "#FFE100", "color": "#111111"}, {"bg": "#FFFFFF", "color": "#111111"}],
            "highlight": "rgba(17,17,17,0.16)",
            "badgeBg": "#FF3B30",
            "badgeColor": "#FFFFFF",
            "align": "left",
        },
    },
    "kkultem": {
        "key": "kkultem",
        "label": "꿀템담아형",
        "desc": "다크 바 + 빨강/노랑 금액 손실회피 배지 (예: 80만 원 세이브)",
        "bars": [{"bg": "0x101010", "alpha": 0.9, "color": "0xFFFFFF"}],
        "font_size": 80,
        "line_gap": 10,
        "pad_y": 24,
        "margin_x": 44,
        "align": "center",
        "highlight": {"bg": "0xFFE100", "alpha": 0.42},
        "badge": {"color": "0xE5231B", "text_color": "0xFFE100", "font_size": 62},
        "css": {
            "bars": [{"bg": "rgba(16,16,16,0.9)", "color": "#FFFFFF"}],
            "highlight": "rgba(255,225,0,0.42)",
            "badgeBg": "#E5231B",
            "badgeColor": "#FFE100",
            "align": "center",
        },
    },
    "mypick": {
        "key": "mypick",
        "label": "마이픽형",
        "desc": "상단 컬러 배너 + 제품 번호 배지 (1/3, TOP 3)",
        "bars": [{"bg": "0x1F6BFF", "alpha": 1.0, "color": "0xFFFFFF"}],
        "font_size": 78,
        "line_gap": 8,
        "pad_y": 24,
        "margin_x": 44,
        "align": "center",
        "highlight": {"bg": "0xFFFFFF", "alpha": 0.26},
        "badge": {"color": "0xFFFFFF", "text_color": "0x1F6BFF", "font_size": 56},
        "css": {
            "bars": [{"bg": "#1F6BFF", "color": "#FFFFFF"}],
            "highlight": "rgba(255,255,255,0.26)",
            "badgeBg": "#FFFFFF",
            "badgeColor": "#1F6BFF",
            "align": "center",
        },
    },
    "simple": {
        "key": "simple",
        "label": "심플 화이트/옐로우",
        "desc": "고대비 화이트 카드 + 옐로우 포인트",
        "bars": [{"bg": "0xFFFFFF", "alpha": 0.96, "color": "0x111111"}],
        "font_size": 82,
        "line_gap": 8,
        "pad_y": 24,
        "margin_x": 48,
        "align": "center",
        "highlight": {"bg": "0xB8FF00", "alpha": 0.85},
        "badge": {"color": "0xFFD400", "text_color": "0x111111", "font_size": 54},
        "css": {
            "bars": [{"bg": "rgba(255,255,255,0.96)", "color": "#111111"}],
            "highlight": "rgba(184,255,0,0.85)",
            "badgeBg": "#FFD400",
            "badgeColor": "#111111",
            "align": "center",
        },
    },
}

DEFAULT_PRESET = "chomolip"


def get_preset(name: Optional[str]) -> Dict[str, Any]:
    return TITLE_PRESETS.get((name or DEFAULT_PRESET)) or TITLE_PRESETS[DEFAULT_PRESET]


def presets_public() -> list[dict]:
    """UI에 내려줄 프리셋 목록."""
    out = []
    for key, p in TITLE_PRESETS.items():
        out.append(
            {
                "key": key,
                "label": p["label"],
                "desc": p["desc"],
                "font_size": p["font_size"],
                "line_gap": p["line_gap"],
                "pad_y": p["pad_y"],
                "margin_x": p["margin_x"],
                "align": p["align"],
                **p["css"],
            }
        )
    return out
