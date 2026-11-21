"""자막/썸네일용 추천 폰트 카탈로그. 파일은 fonts/ 에 넣는다(Git 제외).

설치: python scripts/download_fonts.py  (전부) 또는  python scripts/download_fonts.py jua blackhansans
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import settings

_G = "https://raw.githubusercontent.com/google/fonts/main/ofl"
FONT_CATALOG: List[Dict[str, Any]] = [
    {"key": "jua", "label": "주아 (둥글둥글·귀여움)", "family": "Jua", "bold": False,
     "file": "Jua-Regular.ttf", "url": f"{_G}/jua/Jua-Regular.ttf"},
    {"key": "blackhansans", "label": "블랙한산스 (굵고 임팩트)", "family": "Black Han Sans", "bold": False,
     "file": "BlackHanSans-Regular.ttf", "url": f"{_G}/blackhansans/BlackHanSans-Regular.ttf"},
    {"key": "dohyeon", "label": "도현 (또렷한 쇼츠체)", "family": "Do Hyeon", "bold": False,
     "file": "DoHyeon-Regular.ttf", "url": f"{_G}/dohyeon/DoHyeon-Regular.ttf"},
    {"key": "pretendard", "label": "프리텐다드 Bold (깔끔)", "family": "Pretendard", "bold": True,
     "file": "Pretendard-Bold.otf",
     "url": "https://github.com/orioncactus/pretendard/raw/main/packages/pretendard/dist/public/static/Pretendard-Bold.otf"},
    {"key": "notosanskr", "label": "노토 산스 KR Bold (무난)", "family": "Noto Sans KR", "bold": True,
     "file": "NotoSansKR-Bold.ttf", "url": f"{_G}/notosanskr/NotoSansKR%5Bwght%5D.ttf"},
    {"key": "gowunbatang", "label": "고운바탕 (감성·부드러움)", "family": "Gowun Batang", "bold": True,
     "file": "GowunBatang-Bold.ttf", "url": f"{_G}/gowunbatang/GowunBatang-Bold.ttf"},
]
BY_KEY = {f["key"]: f for f in FONT_CATALOG}


def font_file(key: str) -> Optional[Path]:
    f = BY_KEY.get(key)
    if not f:
        return None
    p = settings.FONT_DIR / f["file"]
    return p if p.is_file() else None


def resolve(key: Optional[str]) -> Optional[Dict[str, Any]]:
    """설치돼 있는 카탈로그 폰트면 {family,bold,path}, 아니면 None(자동 선택으로 폴백)."""
    f = BY_KEY.get(key or "")
    path = font_file(key or "")
    if not f or not path:
        return None
    return {"family": f["family"], "bold": f["bold"], "path": str(path)}


def public_list() -> List[Dict[str, Any]]:
    return [{"key": f["key"], "label": f["label"], "installed": font_file(f["key"]) is not None}
            for f in FONT_CATALOG]
