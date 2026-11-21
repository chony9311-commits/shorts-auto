#!/usr/bin/env python3
"""추천 한글 폰트를 fonts/ 로 내려받는다(전부 무료 라이선스). 인터넷이 되는 내 Mac에서 실행하세요.

  python scripts/download_fonts.py                 # 6종 전부
  python scripts/download_fonts.py jua pretendard  # 골라서
"""
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.fonts import FONT_CATALOG  # noqa: E402

keys = set(sys.argv[1:]) or {f["key"] for f in FONT_CATALOG}
dest = ROOT / "fonts"
dest.mkdir(exist_ok=True)
ok = fail = 0
for f in FONT_CATALOG:
    if f["key"] not in keys:
        continue
    out = dest / f["file"]
    if out.exists():
        print(f"✓ 이미 있음  {f['file']}")
        ok += 1
        continue
    try:
        req = urllib.request.Request(f["url"], headers={"User-Agent": "shorts-factory"})
        data = urllib.request.urlopen(req, timeout=60).read()
        if len(data) < 10_000:
            raise RuntimeError("파일이 너무 작음")
        out.write_bytes(data)
        print(f"↓ 받음      {f['file']} ({len(data)//1024}KB)")
        ok += 1
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 실패      {f['label']}: {exc}\n    직접 받아서 fonts/{f['file']} 로 넣어도 됩니다: {f['url']}")
        fail += 1
print(f"\n완료 {ok}개, 실패 {fail}개")
