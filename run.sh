#!/usr/bin/env bash
# shorts-factory 실행 스크립트 (macOS / Linux)
#   ./run.sh            -> http://localhost:8000
#   PORT=9000 ./run.sh  -> 포트 변경
set -euo pipefail
cd "$(dirname "$0")"

PY_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  echo "❌ python3가 없습니다. https://www.python.org/downloads/ 에서 설치하세요."
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1 && [ -z "${FFMPEG_BIN:-}" ]; then
  echo "❌ ffmpeg가 없습니다. 터미널에서:  brew install ffmpeg"
  echo "   (설치 후 다시 ./run.sh)"
  exit 1
fi

# 1) 가상환경
if [ ! -d ".venv" ]; then
  echo "📦 파이썬 가상환경 생성…"
  "$PY_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 2) 의존성 (requirements.txt 가 바뀔 때만 설치)
STAMP=".venv/.requirements.sha"
NEW_SHA="$(shasum -a 1 requirements.txt 2>/dev/null | awk '{print $1}')"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$NEW_SHA" ]; then
  echo "📦 의존성 설치…"
  python -m pip install --upgrade pip >/dev/null
  python -m pip install -r requirements.txt
  echo "$NEW_SHA" > "$STAMP"
fi

# 3) 실행
PORT="${PORT:-8000}"
echo ""
echo "🎬 shorts-factory  ->  http://localhost:${PORT}"
echo ""
exec python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload
