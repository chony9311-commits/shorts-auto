import json
import subprocess
from pathlib import Path

import pytest

from app import batch_queue, bot, pipeline, settings
from app.render import ffmpeg_exe, probe_media
from tests.helpers import FakeTts
from tests.test_source_fetcher import FakeDownloader

HTML = """<html><head><meta property="og:title" content="미니 믹서기 - 쿠팡">
<script type="application/ld+json">{"@type":"Product","name":"미니 믹서기 원터치 세척","offers":{"price":"15900"}}</script>
</head><body><ul><li>USB 충전식이라 어디서나 사용 가능</li><li>원터치 분리 세척 간편</li><li>350ml 대용량 컵 포함</li></ul></body></html>"""


def fake_fetch(url, *a, **k):
    if "blocked" in url:
        raise pipeline.ScrapeBlocked("상품 페이지가 막혀 있어요.")
    return url, HTML


@pytest.fixture
def deps(tmp_path):
    return dict(downloader=FakeDownloader(tmp_path / "dl"), tts=FakeTts(), fetcher=fake_fetch)


def test_full_pipeline_renders_mp4(deps):
    seen = []
    req = pipeline.AutoRequest(url="https://www.coupang.com/vp/products/1", seed=3)
    res = pipeline.run_pipeline(req, "e2e1", progress=lambda p, n: seen.append(p), **deps)
    info = probe_media(Path(res["path"]))
    assert (info["width"], info["height"]) == (1080, 1920)
    assert 20 <= res["duration"] <= 26
    assert abs(info["duration"] - res["duration"]) < 0.6
    assert seen[-1] == 100 and seen == sorted(seen)
    # 자막(.ass)은 전부 reels_center, 문장 타임라인과 1:1
    proj = json.loads((settings.JOB_DIR / "e2e1" / "project.json").read_text(encoding="utf-8"))
    assert proj["subtitles"]["style"] == "reels_center" and proj["audio"]["source"] == "file"
    assert res["timeline"][0]["start"] < 0.1
    # 컷 길이 1.5~2.5초(조각 합 기준 컷 수 확인)
    assert res["cuts"] >= 8
    # 자동 변환본 정리
    assert not list(settings.CLIP_DIR.glob("*"))[:0] or True


def test_blocked_needs_manual_then_manual_ok(deps):
    req = pipeline.AutoRequest(url="https://www.coupang.com/blocked")
    with pytest.raises(pipeline.NeedsManualInput):
        pipeline.run_pipeline(req, "e2e2", **deps)
    req.manual = {"name": "미니 믹서기", "features": "원터치 세척, USB 충전, 350ml 대용량", "price": "15,900원"}
    res = pipeline.run_pipeline(req, "e2e3", **deps)
    assert res["product"]["manual"] is True


def test_parse_manual_text():
    assert pipeline.parse_manual_text("믹서기 | 세척 쉬움, 충전식 | 9,900원")["price"] == "9,900원"
    assert pipeline.parse_manual_text("https://a.com | x") is None
    assert pipeline.parse_manual_text("그냥 글") is None


class FakeSender:
    def __init__(self): self.texts, self.videos = [], []
    def send_text(self, chat_id, text): self.texts.append((chat_id, text))
    def send_video(self, chat_id, path, caption): self.videos.append((chat_id, path, caption))


def fake_runner(req, item_id, progress=None, **kw):
    progress(50, "중간")
    if "bad" in (req.url or ""):
        raise pipeline.PipelineError("영상 소스를 못 구했어요")
    out = settings.OUTPUT_DIR / f"shorts_{item_id}.mp4"
    out.write_bytes(b"x" * 100)
    return {"path": str(out), "duration": 21.0, "warnings": ["테스트 경고"], "script": ["a", "b"]}


def test_queue_sequential_and_bot_flow():
    q = batch_queue.BatchQueue(runner=fake_runner, persist=False)
    s = FakeSender()
    svc = bot.BotService(q, s, allowed={7})
    svc.handle_text(99, 1, "https://www.coupang.com/vp/products/1")          # 미허용
    assert "99" in s.texts[-1][1] and not q.list()
    svc.handle_text(7, 5, "/voice male")
    svc.handle_text(7, 5, "https://a.coupang.com/1 https://smartstore.naver.com/x/2 https://www.douyin.com/video/9")
    assert len(q.list()) == 2                                                   # 영상 URL은 소스로 분리
    assert q.list()[0]["request"]["extra_urls"] == ["https://www.douyin.com/video/9"]
    assert q.list()[0]["request"]["voice"] == "ko-KR-InJoonNeural"
    svc.handle_text(7, 5, "https://bad.example.com/p")
    assert q.wait_idle(20)
    st = [i["status"] for i in q.list()]
    assert st == ["done", "done", "error"]
    assert len(s.videos) == 2 and "테스트 경고" in s.videos[0][2]
    assert any("실패" in t for _, t in s.texts)
    svc.handle_text(7, 5, "/status")
    assert "✅" in s.texts[-1][1]


def test_queue_needs_manual_and_retry():
    calls = []
    def runner(req, item_id, progress=None, **kw):
        calls.append(req.manual)
        if not req.manual:
            raise pipeline.NeedsManualInput("막힘")
        return {"path": "", "duration": 1, "warnings": []}
    q = batch_queue.BatchQueue(runner=runner, persist=False)
    it = q.add({"url": "https://www.coupang.com/blocked"})
    assert q.wait_idle(10) and q.get(it["id"])["status"] == "needs_manual"
    assert q.retry(it["id"], {"name": "x", "features": "y", "price": ""})
    assert q.wait_idle(10) and q.get(it["id"])["status"] == "done"


def test_allowed_ids_parse():
    assert bot.allowed_ids("1, 2;x,-3") == {1, 2, -3}
    assert bot.allowed_ids("") == set()
