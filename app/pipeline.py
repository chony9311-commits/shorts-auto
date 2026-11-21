"""완전자동 파이프라인: 상품 링크 -> 소스 수집 -> 대본 -> AI 성우 -> 컷편집 -> 중앙 자막 -> 9:16 MP4.

기존 render_video()/store/subtitles 를 그대로 재사용한다. 이 모듈은 단계만 이어 붙인다.
  1) scraper.fetch_product / product_from_manual
  2) source_fetcher.collect_sources
  3) script_gen.generate_script -> synthesize_script (무음 제거 + 실측 길이)
  4) cutter.plan_cuts / assign_sources, subtitles.build_preset_ass(reels_center)
  5) render.render_video
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import cutter, script_gen, settings, store, subtitles
from .render import RenderError, render_video
from .scraper import (ProductInfo, ScrapeBlocked, ScrapeError, extract_urls, fetch_html,
                      fetch_product, product_from_manual)
from .source_fetcher import CROP_DEFAULT, Downloader, collect_sources

TAIL_HOLD = 0.3          # 마지막 자막이 끝난 뒤 영상을 잠깐 더 보여 준다(초)
Progress = Callable[[float, str], None]


class PipelineError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 실패 메시지."""


class NeedsManualInput(PipelineError):
    """상품 페이지가 막혀 있어 '상품명 + 특징' 직접 입력이 필요하다."""


@dataclass
class AutoRequest:
    url: str = ""
    manual: Optional[Dict[str, Any]] = None        # {"name","features","price"}
    extra_urls: List[str] = field(default_factory=list)     # 샤오홍슈/도우인/틱톡 등 직접 고른 URL
    local_paths: List[str] = field(default_factory=list)    # 서버(=내 Mac)에 있는 로컬 클립 경로
    voice: str = script_gen.DEFAULT_VOICE
    rate: str = script_gen.DEFAULT_RATE
    crop_pct: int = CROP_DEFAULT
    subtitle_style: str = subtitles.DEFAULT_AUTO_SUBTITLE_PRESET
    title_preset: str = "kkultem"
    auto_search: bool = True
    seed: Optional[int] = None
    keep_sources: bool = False

    @classmethod
    def from_dict(cls, d: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> "AutoRequest":
        merged = dict(defaults or {})
        merged.update({k: v for k, v in (d or {}).items() if v not in (None, "")})
        known = {f for f in cls.__dataclass_fields__}      # type: ignore[attr-defined]
        return cls(**{k: v for k, v in merged.items() if k in known})

    def label(self) -> str:
        if self.manual and self.manual.get("name"):
            return str(self.manual["name"])[:40]
        return (self.url or "?")[:60]


def parse_manual_text(text: str) -> Optional[Dict[str, Any]]:
    """'상품명 | 특징1, 특징2 | 19,800원' (URL 없이) -> {"name","features","price"}. 아니면 None."""
    if "|" not in (text or "") or extract_urls(text):
        return None
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 2 or not parts[0]:
        return None
    price = parts[2] if len(parts) > 2 else ""
    return {"name": parts[0], "features": parts[1], "price": price}


def _prefer(progress: Optional[Progress]) -> Progress:
    return progress or (lambda _p, _n: None)


def load_product(req: AutoRequest, fetcher: Callable = fetch_html) -> ProductInfo:
    """링크 우선, 막히면 직접 입력 폴백. 둘 다 없으면 NeedsManualInput."""
    manual = req.manual or {}
    if req.url:
        try:
            info = fetch_product(req.url, fetcher=fetcher)
            if manual.get("name"):       # 사용자가 준 값이 있으면 덮어쓴다(수정 입력 우선)
                m = product_from_manual(manual["name"], manual.get("features", ""), manual.get("price"), req.url)
                info.name = m.name
                info.features = m.features or info.features
                info.price, info.price_text = m.price or info.price, m.price_text or info.price_text
            return info
        except ScrapeBlocked as exc:
            if manual.get("name"):
                return product_from_manual(manual["name"], manual.get("features", ""), manual.get("price"), req.url)
            raise NeedsManualInput(
                f"{exc} 상품명과 특징을 직접 입력해 주세요.  예)  미니 믹서기 | 원터치 세척, USB 충전 | 15,900원"
            ) from exc
        except ScrapeError as exc:
            if manual.get("name"):
                return product_from_manual(manual["name"], manual.get("features", ""), manual.get("price"), req.url)
            raise PipelineError(f"상품 정보를 읽지 못했어요: {exc}") from exc
    if manual.get("name"):
        return product_from_manual(manual["name"], manual.get("features", ""), manual.get("price"))
    raise PipelineError("상품 링크나 상품명을 입력해 주세요.")


def run_pipeline(req: AutoRequest, job_id: str, *, progress: Optional[Progress] = None,
                 downloader: Optional[Downloader] = None, tts: Optional[script_gen.TtsBackend] = None,
                 fetcher: Callable = fetch_html) -> Dict[str, Any]:
    """한 상품 -> MP4. 결과 dict(path, duration, script, warnings ...)."""
    say = _prefer(progress)
    warnings: List[str] = []
    created_clip_ids: List[str] = []
    out_path = settings.OUTPUT_DIR / f"shorts_{job_id}.mp4"
    job_dir = settings.JOB_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1) 상품
        say(2, "상품 정보 읽는 중")
        product = load_product(req, fetcher)
        if len(product.features) < 3:
            warnings.append("특징을 3줄 이상 찾지 못해 일반 문구로 채웁니다(직접 입력하면 더 좋아요).")

        # 2) 소스 수집
        say(8, "영상 소스 수집 중")
        src = collect_sources(product, downloader=downloader, want_seconds=45.0, max_clips=8,
                              extra_urls=req.extra_urls, local_paths=req.local_paths,
                              crop_pct=req.crop_pct, auto_search=req.auto_search,
                              progress=lambda m: say(12, "영상 소스 수집 중 · " + m))
        created_clip_ids = [c["id"] for c in src.clips]
        warnings += src.warnings
        if not src.clips:
            raise PipelineError("쓸 수 있는 영상 소스를 못 구했어요. 영상 URL(샤오홍슈·도우인 등)이나 "
                                "로컬 클립을 추가해 주세요.")

        # 3) 대본 + 성우
        say(38, "대본 쓰는 중")
        script = script_gen.generate_script(product, seed=req.seed, rate=req.rate)
        bad = script_gen.validate_script(script)
        if bad:
            warnings += bad
        say(42, "AI 성우 녹음 중")
        voice = script_gen.synthesize_script(script, voice=req.voice, rate=req.rate, backend=tts)
        warnings += voice.warnings

        # 4) 컷 + 자막
        say(62, "컷편집 · 자막 맞추는 중")
        total = round(voice.duration + TAIL_HOLD, 3)
        cuts = cutter.plan_cuts(total, cutter.sentence_boundaries(voice.timeline))
        try:
            pieces = cutter.assign_sources(cuts, src.clips)
        except cutter.NotEnoughFootage as exc:
            raise PipelineError(str(exc)) from exc
        render_clips = cutter.to_render_clips(pieces)

        sid = store.new_id()
        subtitles.build_preset_ass(req.subtitle_style, voice.timeline, settings.SUB_DIR / f"{sid}.ass")

        title_lines = list(script["title_lines"])
        project: Dict[str, Any] = {
            "mode": "shopping", "width": settings.OUT_W, "height": settings.OUT_H, "fps": settings.OUT_FPS,
            "clips": render_clips,
            "title": {"enabled": True, "preset": req.title_preset,
                      "lines": [title_lines[0], "이거 꿀템 발견" if product.price_text else title_lines[1]],
                      "badge_text": product.price_text, "badge_number_text": "",
                      "font_scale": 1.0, "dim": 0.0},
            "badges": {"enabled": False},
            "audio": {"source": "file", "audio_id": voice.audio_id, "volume": 1.0},
            "subtitles": {"enabled": True, "subtitle_id": sid, "style": req.subtitle_style},
            "thumbnail": {"prepend": False},
        }
        (job_dir / "project.json").write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")

        # 5) 렌더
        say(66, "영상 렌더링 중")
        result = render_video(project, out_path, lambda p, n: say(66 + 33 * p, "영상 렌더링 중"))
        info = {
            "path": str(out_path), "duration": result["duration"], "cuts": len(cuts),
            "pieces": len(pieces), "voice": voice.voice, "rate": voice.rate,
            "product": product.to_dict(), "script": [x["text"] for x in voice.lines],
            "timeline": voice.timeline, "warnings": warnings,
            "search_links": src.search_links, "keywords": {k: src.keywords.get(k) for k in ("ko", "en", "zh")},
            "sources": [{"name": c["name"], "origin": c.get("origin"), "duration": c["duration"]} for c in src.clips],
        }
        (job_dir / "result.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        say(100, "완료")
        return info
    except (RenderError, ScrapeError) as exc:
        raise PipelineError(str(exc)) from exc
    except RuntimeError as exc:
        if isinstance(exc, PipelineError):
            raise
        raise PipelineError(str(exc)) from exc
    finally:
        if created_clip_ids and not req.keep_sources:       # 자동 변환본은 쌓이지 않게 정리(원본 로컬 파일은 그대로)
            for cid in created_clip_ids:
                store.delete_clip(cid)
