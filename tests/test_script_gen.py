import pytest

from app import script_gen as sg
from app.render import probe_duration
from app.scraper import ProductInfo, product_from_manual
from tests.helpers import FakeTts

P1 = ProductInfo(name="모아홈 접이식 대용량 정리함 3개입", price=12900, price_text="12,900원", brand="모아홈",
                 features=["뚜껑까지 접히는 간편한 수납", "방수 원단이라 물세척이 쉬워요", "스테인리스 프레임으로 튼튼하게 오래 사용", "가벼워서 휴대하기 좋은 손잡이"])


def test_template_pools_follow_line_rule():
    pools = sg.HOOKS + sg.HOOK2 + sg.PROBLEMS + sg.SOLUTIONS + sg.PRICE_FALLBACK + sg.FILLERS + sg.CTAS
    bad = [t for t in pools if not (sg.LINE_MIN <= sg.visible_len(t) <= sg.LINE_MAX)]
    assert bad == []


@pytest.mark.parametrize("p", [
    P1,
    ProductInfo(name="무선 핸디 선풍기", price=19800, price_text="19,800원", features=["저소음 BLDC 모터", "한 번 충전으로 8시간 사용"]),
    product_from_manual("미니 믹서기", "원터치 세척, USB 충전"),
    ProductInfo(name="초고가 안마의자 프리미엄", price=1299000, price_text="1,299,000원"),
])
def test_generate_script_valid(p):
    s = sg.generate_script(p)
    assert sg.validate_script(s) == []
    roles = [x["role"] for x in s["lines"]]
    assert roles[0] == "hook" and roles[-1] == "cta"
    assert 18 <= s["estimated_seconds"] <= 27
    assert all(len(t) <= 14 for t in [x.replace("*", "") for x in s["title_lines"]])


def test_script_deterministic_and_seedable():
    assert sg.generate_script(P1) == sg.generate_script(P1)
    assert sg.generate_script(P1, seed=1)["lines"] != sg.generate_script(P1, seed=2)["lines"]


def test_split_to_lines():
    assert sg.split_to_lines("뚜껑까지 접히는 간편한 수납") == ["뚜껑까지 접히는", "간편한 수납"] or \
        all(sg.LINE_MIN <= sg.visible_len(x) <= sg.LINE_MAX for x in sg.split_to_lines("뚜껑까지 접히는 간편한 수납"))
    assert sg.split_to_lines("초경량초강력초대용량초특가상품") == []


def test_voice_and_rate_normalization():
    assert sg.resolve_voice("female") == "ko-KR-SunHiNeural"
    assert sg.resolve_voice("male") == "ko-KR-InJoonNeural"
    assert sg.resolve_voice(None) == "ko-KR-SunHiNeural"
    assert sg.resolve_voice("ko-KR-InJoonNeural") == "ko-KR-InJoonNeural"
    assert sg.DEFAULT_RATE == "+18%"
    assert [sg.normalize_rate(x) for x in ("18", "+18%", "-10", None, "999")] == ["+18%", "+18%", "-10%", "+18%", "+50%"]


def test_trim_silence_removes_breath(tmp_path):
    import asyncio
    tts = FakeTts()
    raw = tmp_path / "r.mp3"
    asyncio.run(tts.synth("가나다라마바사아자차", "v", "+18%", raw))     # 1.4초 음성 + 앞 0.4 + 뒤 0.6
    raw_d = probe_duration(raw)
    assert raw_d > 2.3
    out = tmp_path / "o.wav"
    d = sg.trim_silence(raw, out)
    assert 1.35 <= d <= 1.55 and raw_d - d > 0.9      # 숨 1초가량 제거, 음성 본체는 유지


def test_synthesize_timeline_sync(tmp_path):
    s = sg.generate_script(P1)
    tts = FakeTts()
    vt = sg.synthesize_script(s, voice="male", rate="+18%", backend=tts)
    assert all(c[1] == "ko-KR-InJoonNeural" and c[2] == "+18%" for c in tts.calls)
    tl = vt.timeline
    assert tl[0]["start"] == 0
    for a, b in zip(tl, tl[1:]):                                   # 문장 사이는 gap 만큼만 비어 있다
        assert b["start"] == pytest.approx(a["end"] + sg.GAP_SECONDS, abs=0.002)
    assert vt.duration == pytest.approx(tl[-1]["end"], abs=0.06)   # 타임라인 == 실제 오디오 길이
    assert 20.0 <= vt.duration <= 25.0, (vt.duration, vt.warnings)
    assert probe_duration(vt.path) == pytest.approx(vt.duration, abs=0.01)
    assert [x["id"] for x in vt.lines] == list(range(1, len(vt.lines) + 1))
    assert vt.lines[0]["role"] == "hook" and vt.lines[-1]["role"] == "cta"
    # 문장별 길이가 숨(앞 0.4+뒤 0.6)을 포함하지 않는다
    assert all(x["dur"] < sg.visible_len(x["text"]) * FakeTts.SEC_PER_CHAR + 0.15 for x in tl)


def test_too_short_script_gets_reserve_lines():
    s = sg.generate_script(P1)
    short = dict(s, lines=s["lines"][:6] + [s["lines"][-1]])
    vt = sg.synthesize_script(short, backend=FakeTts())
    assert len(vt.lines) > 7 and vt.duration >= 20 - 0.5
