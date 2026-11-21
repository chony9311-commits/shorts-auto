import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app import settings, source_fetcher as sf, store
from app.render import ffmpeg_exe, probe_media
from app.scraper import ProductInfo
from tests.helpers import make_video


class FakeDownloader:
    """네트워크 없이 로컬에서 만든 영상을 '다운로드'한다."""
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.searched = []
        self.downloaded = []

    def search(self, query, n, site="youtube"):
        self.searched.append((site, query))
        return [{"url": f"fake://{site}/{i}", "title": f"{site} 영상 {i}", "duration": 8, "id": f"{site}{i}"}
                for i in range(n)]

    def download(self, url, out_dir):
        self.downloaded.append(url)
        if url.endswith("/3"):
            raise RuntimeError("조건에 맞지 않음")
        out_dir.mkdir(parents=True, exist_ok=True)
        dims = (720, 1280) if url.endswith("/1") else (1280, 720)
        return make_video(out_dir / (url.replace("://", "_").replace("/", "_") + ".mp4"),
                          w=dims[0], h=dims[1], sec=7.0, audio=True)


PRODUCT = ProductInfo(name="모아홈 접이식 대용량 정리함 3개입", brand="모아홈", features=["방수"])


def test_keywords_ko_en_zh():
    kw = sf.build_keywords(PRODUCT, translate=lambda t, lang: None)
    assert "정리함" in kw["terms"]
    assert any("organizer" in q for q in kw["en"])
    assert any("收纳盒" in q for q in kw["zh"])
    assert all("3개입" not in q and "모아홈" not in q for q in kw["ko"])
    links = sf.manual_search_links(kw)
    assert "xiaohongshu.com" in links["샤오홍슈"] and "douyin.com" in links["도우인"]


def test_unknown_word_uses_translator_then_warns_without():
    p = ProductInfo(name="쿠쿠 뽀로로")
    kw = sf.build_keywords(p, translate=lambda t, lang: {"en": "poro", "zh-CN": "啵乐乐"}[lang])
    assert kw["en"] and kw["zh"]
    kw2 = sf.build_keywords(p, translate=lambda t, lang: None)
    assert not kw2["en"] and not kw2["zh"] and len(kw2["warnings"]) == 2


def test_crop_pct_clamped():
    assert sf.clamp_crop_pct(115) == 115 and sf.clamp_crop_pct(50) == 100 and sf.clamp_crop_pct(500) == 130
    assert sf.clamp_crop_pct("abc") == sf.CROP_DEFAULT


def _pixel(video: Path, x: int, y: int, tmp: Path) -> tuple:
    png = tmp / "f.png"
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-ss", "1", "-i", str(video), "-frames:v", "1", str(png)], check=True)
    return Image.open(png).convert("RGB").getpixel((x, y))


def test_normalize_vertical_and_center_crop(tmp_path):
    src = make_video(tmp_path / "s.mp4", w=1080, h=1920, sec=3, border=True, audio=True)
    a = tmp_path / "a.mp4"; b = tmp_path / "b.mp4"
    ia = sf.normalize_to_vertical(src, a, 100)
    ib = sf.normalize_to_vertical(src, b, 115)
    for i in (ia, ib):
        assert (i["width"], i["height"]) == (1080, 1920)
    assert not probe_media(a)["has_audio"]
    # 100%: 가장자리가 빨간 테두리 / 115%: 테두리가 잘려 나가 빨강이 아님
    r, g, bl = _pixel(a, 4, 960, tmp_path)
    assert r > 200 and g < 80
    r2, g2, b2 = _pixel(b, 4, 960, tmp_path)
    assert not (r2 > 200 and g2 < 80)


def test_collect_sources_mixed(tmp_path):
    local = make_video(tmp_path / "mine.mp4", sec=6)
    dl = FakeDownloader(tmp_path / "dl")
    res = sf.collect_sources(PRODUCT, downloader=dl, want_seconds=30, max_clips=6,
                             extra_urls=["fake://user/0"], local_paths=[str(local), "/no/such.mp4"],
                             translate=lambda t, l: None)
    assert res.total_seconds >= 30 or len(res.clips) == 6
    origins = [c["origin"] for c in res.clips]
    assert origins[0] == "local" and "url" in origins           # 사용자 투입이 먼저
    assert any("로컬 파일이 없습니다" in w for w in res.warnings)
    for c in res.clips:
        assert (c["width"], c["height"]) == (1080, 1920)
        assert store.clip_meta(c["id"])                        # 렌더러가 찾을 수 있게 등록됨
    assert dl.searched                                         # 부족분은 자동 검색
    assert not list(settings.TMP_DIR.glob("dl_*"))              # 임시 폴더 정리


def test_failed_download_is_skipped(tmp_path):
    dl = FakeDownloader(tmp_path)
    res = sf.collect_sources(PRODUCT, downloader=dl, want_seconds=100, max_clips=10, translate=lambda t, l: None)
    assert any("건너뜀" in w for w in res.warnings)
    assert len(res.clips) >= 2


def test_nothing_collected_warns(tmp_path):
    class Empty(FakeDownloader):
        def search(self, *a, **k): return []
    res = sf.collect_sources(PRODUCT, downloader=Empty(tmp_path), translate=lambda t, l: None)
    assert res.clips == [] and any("하나도" in w for w in res.warnings)
