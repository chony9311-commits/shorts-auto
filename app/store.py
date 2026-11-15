"""업로드 파일/메타데이터 저장소.

파일은 data/ 아래에 두고, 옆에 같은 이름의 .json 메타를 남긴다.
서버를 껐다 켜도 목록이 유지된다.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from . import settings


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------- clips
def clip_meta_path(cid: str) -> Path:
    return settings.CLIP_DIR / f"{cid}.json"


def save_clip_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    _write_json(clip_meta_path(meta["id"]), meta)
    return meta


def clip_meta(cid: Optional[str]) -> Optional[Dict[str, Any]]:
    if not cid:
        return None
    data = _read_json(clip_meta_path(cid))
    if not data:
        return None
    p = Path(data.get("path", ""))
    if not p.exists():
        return None
    data["path"] = str(p)
    return data


def delete_clip(cid: str) -> bool:
    data = clip_meta(cid)
    ok = False
    if data:
        try:
            Path(data["path"]).unlink(missing_ok=True)
            ok = True
        except Exception:
            pass
    clip_meta_path(cid).unlink(missing_ok=True)
    return ok


def list_clips() -> list[dict]:
    out = []
    for p in sorted(settings.CLIP_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        d = _read_json(p)
        if d and Path(d.get("path", "")).exists():
            out.append(d)
    return out


# ---------------------------------------------------------------- audio
def audio_meta_path(aid: str) -> Path:
    return settings.AUDIO_DIR / f"{aid}.json"


def save_audio_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    _write_json(audio_meta_path(meta["id"]), meta)
    return meta


def audio_meta(aid: Optional[str]) -> Optional[Dict[str, Any]]:
    if not aid:
        return None
    data = _read_json(audio_meta_path(aid))
    if not data:
        return None
    p = Path(data.get("path", ""))
    if not p.exists():
        return None
    data["path"] = str(p)
    return data


def list_audio() -> list[dict]:
    out = []
    for p in sorted(settings.AUDIO_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        d = _read_json(p)
        if d and Path(d.get("path", "")).exists():
            out.append(d)
    return out


# ---------------------------------------------------------------- images (커스텀 썸네일)
def _image(aid: str) -> Optional[Dict[str, Any]]:
    data = _read_json(settings.IMAGE_DIR / f"{aid}.json")
    if not data:
        return None
    p = Path(data.get("path", ""))
    if not p.exists():
        return None
    data["path"] = str(p)
    return data


def save_image_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    _write_json(settings.IMAGE_DIR / f"{meta['id']}.json", meta)
    return meta


def get_image(aid: Optional[str]) -> Optional[Dict[str, Any]]:
    if not aid:
        return None
    return _image(aid)


# ---------------------------------------------------------------- subtitles
def subtitle_path(sid: Optional[str]) -> Optional[Path]:
    if not sid:
        return None
    p = settings.SUB_DIR / f"{sid}.ass"
    return p if p.exists() else None
