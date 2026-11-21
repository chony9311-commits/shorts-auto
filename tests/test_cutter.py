import random

import pytest

from app import cutter as ct


def timeline(durs, gap=0.06):
    t, out = 0.0, []
    for d in durs:
        out.append({"start": t, "end": t + d}); t += d + gap
    return out


def test_cuts_within_range_and_tile_exactly():
    rnd = random.Random(1)
    for _ in range(40):
        durs = [rnd.uniform(1.0, 2.2) for _ in range(rnd.randint(12, 18))]
        tl = timeline(durs)
        total = tl[-1]["end"] + 0.3
        cuts = ct.plan_cuts(total, ct.sentence_boundaries(tl))
        assert cuts[0][0] == 0 and cuts[-1][1] == pytest.approx(total, abs=2e-3)
        for (a, b), (c, d) in zip(cuts, cuts[1:]):
            assert b == c
        for a, b in cuts:
            assert 1.5 - 0.06 <= b - a <= 2.5 + 0.06, (a, b)


def test_cuts_prefer_sentence_boundaries():
    tl = timeline([2.0] * 11)                 # 문장이 2초씩 -> 컷도 문장 경계에 떨어져야 한다
    total = tl[-1]["end"]
    cuts = ct.plan_cuts(total, ct.sentence_boundaries(tl))
    bounds = ct.sentence_boundaries(tl)
    snapped = sum(1 for _, b in cuts[:-1] if min(abs(b - x) for x in bounds) <= 0.06)
    assert snapped >= len(cuts) - 2


def test_infeasible_total_falls_back():
    cuts = ct.plan_cuts(2.7, [])              # 1.5~2.5 합으로 못 만드는 길이
    assert sum(b - a for a, b in cuts) == pytest.approx(2.7, abs=0.01)


def clips(*durs):
    return [{"id": f"c{i}", "duration": d} for i, d in enumerate(durs)]


def overlaps(pieces):
    by = {}
    for p in pieces:
        by.setdefault(p.clip_id, []).append((p.start, p.end))
    for v in by.values():
        v.sort()
        for (a, b), (c, d) in zip(v, v[1:]):
            if c < b - 1e-9:
                return True
    return False


def test_assign_never_repeats_and_fills_all_cuts():
    cuts = ct.plan_cuts(23.0, [])
    pieces = ct.assign_sources(cuts, clips(9, 8, 7, 6, 5))
    assert not overlaps(pieces)
    assert sum(p.dur for p in pieces) == pytest.approx(23.0, abs=0.01)
    for p in pieces:                              # 소스 범위 안, 배속 없음(구간 길이 == 컷 길이)
        assert 0 <= p.start < p.end <= 9
    by_cut = {}
    for p in pieces:
        by_cut.setdefault(p.cut, 0.0)
        by_cut[p.cut] += p.dur
    for i, (a, b) in enumerate(cuts):
        assert by_cut[i] == pytest.approx(b - a, abs=0.01)


def test_adjacent_cuts_use_different_clips_when_possible():
    cuts = ct.plan_cuts(20.0, [])
    pieces = ct.assign_sources(cuts, clips(12, 12, 12))
    first = [next(p.clip_id for p in pieces if p.cut == i) for i in range(len(cuts))]
    assert all(a != b for a, b in zip(first, first[1:]))


def test_short_clips_split_cut_into_two_pieces():
    cuts = [(0.0, 2.4), (2.4, 4.8)]
    pieces = ct.assign_sources(cuts, clips(2.0, 2.0, 2.0, 2.0))   # 각 클립은 사용 가능 1.5초뿐 (head .3, tail .2)
    assert not overlaps(pieces)
    assert sum(p.dur for p in pieces) == pytest.approx(4.8, abs=0.01)
    assert len({p.cut for p in pieces}) == 2 and len(pieces) > 2


def test_not_enough_footage_reports_missing_seconds():
    with pytest.raises(ct.NotEnoughFootage) as e:
        ct.assign_sources(ct.plan_cuts(22.0, []), clips(5, 5))
    assert e.value.missing > 10
    assert "모자랍니다" in str(e.value)


def test_to_render_clips():
    pieces = ct.assign_sources([(0.0, 2.0)], clips(6))
    rc = ct.to_render_clips(pieces)
    assert rc[0]["clip_id"] == "c0" and rc[0]["zoom"] == 1.0 and rc[0]["mute"] is True
