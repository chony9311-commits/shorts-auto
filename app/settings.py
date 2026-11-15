"""경로/환경 설정.

로컬(Mac)에서 바로 돌아가도록 모든 경로는 프로젝트 폴더 기준 상대경로다.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.getenv("SHORTS_DATA_DIR", BASE_DIR / "data")).resolve()
CLIP_DIR = DATA_DIR / "clips"
AUDIO_DIR = DATA_DIR / "audio"
SUB_DIR = DATA_DIR / "subtitles"
IMAGE_DIR = DATA_DIR / "images"
JOB_DIR = DATA_DIR / "jobs"
TMP_DIR = DATA_DIR / "tmp"

OUTPUT_DIR = Path(os.getenv("SHORTS_OUTPUT_DIR", BASE_DIR / "output")).resolve()
STATIC_DIR = Path(__file__).resolve().parent / "static"
FONT_DIR = BASE_DIR / "fonts"

for _d in (DATA_DIR, CLIP_DIR, AUDIO_DIR, SUB_DIR, IMAGE_DIR, JOB_DIR, TMP_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 업로드 상한 (MB). 휴대폰 원본 영상이 커도 받도록 넉넉하게.
MAX_UPLOAD_MB = int(os.getenv("SHORTS_MAX_UPLOAD_MB", "2048"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}

# 렌더 기본값
OUT_W = 1080
OUT_H = 1920
OUT_FPS = 30
TARGET_DURATION_RANGE = (20.0, 24.0)  # 숏폼 리듬 권장 구간(초)
