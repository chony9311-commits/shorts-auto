"""테스트는 data/ output/ 을 건드리지 않도록 임시 폴더를 쓴다."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="shorts_test_")
os.environ.setdefault("SHORTS_DATA_DIR", str(Path(_tmp) / "data"))
os.environ.setdefault("SHORTS_OUTPUT_DIR", str(Path(_tmp) / "output"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
