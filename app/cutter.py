"""자동 컷편집 플래너.

오디오(문장 타임라인) 길이에 맞춰 컷을 1.5~2.5초로 나누고, 컷마다 소스 영상의 "다른 구간"을 배정한다.

  * plan_cuts()      : 컷 경계 결정. 문장이 끝나는 지점에 최대한 맞춘다(DP).
  * assign_sources() : 소스가 모자라면 반복·배속하지 않고 남은 다른 구간을 잘라 채운다.
                       한 컷은 최대 2개 조각(서로 다른 클립)으로 구성될 수 있다.
결과는 render_video() 프로젝트 JSON 의 clips 항목으로 바로 바뀐다(to_render_clips).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

CUT_MIN, CUT_MAX = 1.5, 2.5
STEP = 0.05                 # 컷 경계 탐색 격자(초)
HEAD_SKIP = 0.30            # 클립 앞 0.3초는 쓰지 않는다(페이드·로고 인트로 회피)
TAIL_SKIP = 0.20
PIECE_GAP = 0.15            # 같은 클립에서 연속으로 뽑을 때 사이를 비운다(거의 같은 장면 반복 방지)
MIN_PIECE = 0.6


class NotEnoughFootage(RuntimeError):
    """소스 영상이 영상 길이를 채우기에 모자란다(반복/배속은 하지 않는다)."""

    def __init__(self, missing: float, total: float) -> None:
        self.missing = round(missing, 1)
        super().__init__(f"영상 소스가 약 {self.missing}초 모자랍니다(필요 {total:.1f}초). "
                         "클립이나 URL을 더 추가해 주세요.")


# ------------------------------------------------------------------ 컷 경계
def sentence_boundaries(timeline: Sequence[Dict[str, Any]]) -> List[float]:
    """문장 사이 빈틈의 한가운데(마지막 문장 끝은 제외)."""
    b: List[float] = []
    for a, nxt in zip(timeline, timeline[1:]):
        b.append((float(a["end"]) + float(nxt["start"])) / 2.0)
    return b


def plan_cuts(total: float, boundaries: Sequence[float], lo: float = CUT_MIN,
              hi: float = CUT_MAX, step: float = STEP) -> List[Tuple[float, float]]:
    """[0,total]을 lo~hi 길이의 연속 구간으로 나눈다. 경계는 문장 끝에 가까울수록 좋다."""
    if total <= 0:
        return []
    for relax in (1.0, 0.85, 0.7):                          # 안 풀리면 범위를 조금씩 넓힌다
        cuts = _dp_cuts(total, boundaries, lo * relax, hi / relax if relax < 1 else hi, step)
        if cuts:
            return cuts
    n = max(1, math.ceil(total / hi))                       # 최후: 균등 분할
    w = total / n
    return [(round(i * w, 3), round((i + 1) * w, 3)) for i in range(n)]


def _dp_cuts(total: float, boundaries: Sequence[float], lo: float, hi: float,
             step: float) -> Optional[List[Tuple[float, float]]]:
    n = int(round(total / step))
    if n <= 0:
        return None
    lo_i, hi_i = max(1, int(math.ceil(lo / step - 1e-9))), int(math.floor(hi / step + 1e-9))
    bset = sorted(boundaries)

    def pen(j: int) -> float:                                # 격자 j 가 문장 경계에서 얼마나 먼가
        if j == n or not bset:
            return 0.0
        t = j * step
        return min(abs(t - b) for b in bset)

    INF = float("inf")
    dp = [INF] * (n + 1)
    prev = [-1] * (n + 1)
    dp[0] = 0.0
    for j in range(1, n + 1):
        pj = pen(j)
        for k in range(lo_i, hi_i + 1):
            i = j - k
            if i < 0:
                break
            if dp[i] == INF:
                continue
            c = dp[i] + pj * 4.0 + abs(k * step - 2.0) * 0.15       # 경계 우선, 길이는 2초 부근 선호
            if c < dp[j]:
                dp[j], prev[j] = c, i
    if dp[n] == INF:
        return None
    pts = [n]
    while pts[-1] > 0:
        pts.append(prev[pts[-1]])
    pts.reverse()
    out = [(round(a * step, 3), round(b * step, 3)) for a, b in zip(pts, pts[1:])]
    out[-1] = (out[-1][0], round(total, 3))                      # 격자 반올림 오차 흡수
    return out


# ------------------------------------------------------------------ 소스 배정
@dataclass
class Piece:
    clip_id: str
    start: float
    end: float
    cut: int

    @property
    def dur(self) -> float:
        return self.end - self.start


def assign_sources(cuts: Sequence[Tuple[float, float]], clips: Sequence[Dict[str, Any]],
                   head: float = HEAD_SKIP, tail: float = TAIL_SKIP, gap: float = PIECE_GAP) -> List[Piece]:
    """컷마다 서로 겹치지 않는 소스 구간을 배정한다. 부족하면 NotEnoughFootage."""
    total = sum(b - a for a, b in cuts)
    cursor = {c["id"]: head for c in clips}
    dur = {c["id"]: float(c.get("duration") or 0.0) for c in clips}
    order = [c["id"] for c in clips]

    def remaining(cid: str) -> float:
        return dur[cid] - tail - cursor[cid]

    avail = sum(max(0.0, remaining(c)) for c in order)
    if avail + 1e-6 < total:
        raise NotEnoughFootage(total - avail, total)

    pieces: List[Piece] = []
    last: Optional[str] = None
    for ci, (a, b) in enumerate(cuts):
        need = b - a
        # 1) 한 클립으로 통째로: 직전 컷과 다른 클립 중 남은 구간이 가장 긴 것
        fit = [c for c in order if remaining(c) + 1e-6 >= need]
        pref = [c for c in fit if c != last] or fit
        if pref:
            cid = max(pref, key=remaining)
            pieces.append(Piece(cid, round(cursor[cid], 3), round(cursor[cid] + need, 3), ci))
            cursor[cid] += need + gap
            last = cid
            continue
        # 2) 두 조각(서로 다른 클립): 가장 긴 남은 구간 + 나머지
        ranked = sorted(order, key=remaining, reverse=True)
        done = False
        for first in ranked:
            r1 = remaining(first)
            if r1 < MIN_PIECE:
                break
            rest = need - r1
            second = next((c for c in ranked if c != first and remaining(c) + 1e-6 >= rest), None)
            if rest < MIN_PIECE:        # 거의 다 채워짐: 첫 조각을 남은 만큼 쓰고 둘째로 마무리
                rest = max(rest, MIN_PIECE)
            if second is None:
                continue
            r1 = need - rest
            if r1 < MIN_PIECE:
                continue
            pieces.append(Piece(first, round(cursor[first], 3), round(cursor[first] + r1, 3), ci))
            cursor[first] += r1 + gap
            pieces.append(Piece(second, round(cursor[second], 3), round(cursor[second] + rest, 3), ci))
            cursor[second] += rest + gap
            last = second
            done = True
            break
        if not done:                                             # 조각 규칙으로 못 채움 -> 모자람으로 처리
            raise NotEnoughFootage(max(0.1, need - max(remaining(c) for c in order)), total)
    return pieces


def to_render_clips(pieces: Sequence[Piece]) -> List[Dict[str, Any]]:
    """render_video() 의 project["clips"] 형식(소스는 이미 1080x1920 이라 zoom 1.0)."""
    return [{"clip_id": p.clip_id, "start": p.start, "end": p.end, "zoom": 1.0, "pan_x": 0.0,
             "pan_y": 0.0, "mute": True, "masks": [], "cut": p.cut} for p in pieces]
