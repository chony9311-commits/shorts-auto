"""shorts-factory 로컬 서버 (FastAPI).

실행:  ./run.sh   ->  http://localhost:8000

엔드포인트 요약
  GET  /                        다크모드 웹 UI
  GET  /api/state               ffmpeg/폰트/프리셋/모드 상태
  POST /api/clips               영상 클립 업로드(다중)
  GET  /api/clips/{id}/file     미리보기용 원본 스트리밍
  DELETE /api/clips/{id}
  POST /api/audio               외부 TTS(MP3/WAV) 업로드
  GET  /api/audio/{id}/file
  POST /api/tts                 edge-tts로 음성 생성
  GET  /api/voices              edge-tts 한국어 보이스 목록
  POST /api/images              커스텀 썸네일 이미지 업로드
  GET  /api/images/{id}/file
  POST /api/subtitles           .srt 업로드 -> .ass 변환
  POST /api/transcribe          오디오 -> faster-whisper 자동 자막(.ass)
  POST /api/render              프로젝트 JSON -> MP4 (백그라운드 잡)
  GET  /api/jobs/{id}           진행률 조회
  GET  /api/download/{id}       완성 MP4 다운로드
  POST /api/thumbnail           프로젝트 JSON -> 썸네일 PNG
  GET  /api/download/thumb/{id}
"""
from __future__ import annotations

import asyncio
import os
import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import bot, pipeline, script_gen, scraper, settings, source_fetcher, store, subtitles
from .batch_queue import BatchQueue
from .presets import TITLE_PRESETS, list_font_files, presets_public, resolve_font
from .render import (RenderError, media_tools_ready, probe_duration, probe_media,
                     render_thumbnail, render_video)

app = FastAPI(title="shorts-factory", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

KOREAN_VOICES = [
    {"id": "ko-KR-SunHiNeural", "label": "선히 (여성 · 밝은 톤)"},
    {"id": "ko-KR-InJoonNeural", "label": "인준 (남성 · 차분한 톤)"},
    {"id": "ko-KR-HyunsuMultilingualNeural", "label": "현수 (남성 · 멀티링구얼)"},
]


# ------------------------------------------------------------------ helpers
def _job_set(jid: str, **kw: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.setdefault(jid, {})
        job.update(kw)
        job["updated_at"] = time.time()


def _job_get(jid: str) -> Optional[Dict[str, Any]]:
    with JOBS_LOCK:
        job = JOBS.get(jid)
        return dict(job) if job else None


async def _save_upload(upload: UploadFile, dest: Path) -> int:
    size = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > settings.MAX_UPLOAD_BYTES:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"파일이 너무 큽니다(최대 {settings.MAX_UPLOAD_MB}MB)")
            fh.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "빈 파일입니다.")
    return size


def _ext_of(name: str, allowed: set[str], fallback: str) -> str:
    ext = Path(name or "").suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"지원하지 않는 형식입니다: {ext or '(확장자 없음)'}")
    return ext or fallback


# ------------------------------------------------------------------ 상태
@app.get("/api/state")
def api_state() -> Dict[str, Any]:
    ok, msg = media_tools_ready()
    font = resolve_font()
    return {
        "ffmpeg": ok,
        "ffmpeg_message": msg,
        "font": font,
        "fonts": list_font_files(),
        "font_ok": bool(font),
        "presets": presets_public(),
        "default_preset": "chomolip",
        "output": {"width": settings.OUT_W, "height": settings.OUT_H, "fps": settings.OUT_FPS},
        "target_duration": list(settings.TARGET_DURATION_RANGE),
        "modes": [
            {"key": "shopping", "label": "쇼핑 쇼츠", "hint": "한 플랫폼 3~5개 제품 · 번호 배지 1~5"},
            {"key": "celebrity", "label": "셀럽 쇼츠", "hint": "@초몰입 벤치마크 · 출처/제품명 태그"},
        ],
        "voices": KOREAN_VOICES,
        "subtitle_presets": [{"key": k, "label": v.get("label", k)} for k, v in subtitles.SUBTITLE_PRESETS.items()],
        "default_subtitle_preset": subtitles.DEFAULT_AUTO_SUBTITLE_PRESET,
        "auto": {"voices": script_gen.VOICES, "rate": script_gen.DEFAULT_RATE,
                 "crop_pct": source_fetcher.CROP_DEFAULT, "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN"))},
        "max_upload_mb": settings.MAX_UPLOAD_MB,
    }


# ------------------------------------------------------------------ 클립
@app.post("/api/clips")
async def api_upload_clips(files: List[UploadFile] = File(...)) -> Dict[str, Any]:
    ok, msg = media_tools_ready()
    if not ok:
        raise HTTPException(500, msg)
    out = []
    for f in files:
        ext = _ext_of(f.filename or "", settings.VIDEO_EXTS, ".mp4")
        cid = store.new_id()
        dest = settings.CLIP_DIR / f"{cid}{ext}"
        size = await _save_upload(f, dest)
        try:
            info = probe_media(dest)
        except RenderError as exc:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, f"{f.filename}: {exc}") from exc
        meta = {
            "id": cid, "name": f.filename or dest.name, "path": str(dest), "size": size,
            "duration": info["duration"], "width": info["width"], "height": info["height"],
            "has_audio": info["has_audio"], "created_at": time.time(),
        }
        store.save_clip_meta(meta)
        out.append(meta)
    return {"clips": out}


@app.get("/api/clips")
def api_list_clips() -> Dict[str, Any]:
    return {"clips": store.list_clips()}


@app.get("/api/clips/{cid}/file")
def api_clip_file(cid: str) -> FileResponse:
    meta = store.clip_meta(cid)
    if not meta:
        raise HTTPException(404, "클립 없음")
    return FileResponse(meta["path"], media_type="video/mp4")


@app.delete("/api/clips/{cid}")
def api_delete_clip(cid: str) -> Dict[str, Any]:
    return {"deleted": store.delete_clip(cid)}


# ------------------------------------------------------------------ 오디오 / TTS
@app.post("/api/audio")
async def api_upload_audio(file: UploadFile = File(...)) -> Dict[str, Any]:
    ext = _ext_of(file.filename or "", settings.AUDIO_EXTS, ".mp3")
    aid = store.new_id()
    dest = settings.AUDIO_DIR / f"{aid}{ext}"
    await _save_upload(file, dest)
    dur = probe_duration(dest)
    meta = {"id": aid, "name": file.filename or dest.name, "path": str(dest),
            "duration": dur, "source": "upload", "created_at": time.time()}
    store.save_audio_meta(meta)
    return meta


@app.get("/api/audio")
def api_list_audio() -> Dict[str, Any]:
    return {"audio": store.list_audio()}


@app.get("/api/audio/{aid}/file")
def api_audio_file(aid: str) -> FileResponse:
    meta = store.audio_meta(aid)
    if not meta:
        raise HTTPException(404, "오디오 없음")
    return FileResponse(meta["path"], media_type="audio/mpeg")


@app.get("/api/voices")
def api_voices() -> Dict[str, Any]:
    return {"voices": KOREAN_VOICES}


@app.post("/api/tts")
async def api_tts(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "TTS로 만들 문장이 비어 있습니다.")
    voice = str(payload.get("voice") or "ko-KR-SunHiNeural")
    rate = str(payload.get("rate") or "+0%")
    if not rate.startswith(("+", "-")):
        rate = "+" + rate.replace("%", "") + "%"
    try:
        import edge_tts  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            400, "edge-tts가 설치되어 있지 않습니다. 터미널: pip install edge-tts"
        ) from exc

    aid = store.new_id()
    dest = settings.AUDIO_DIR / f"{aid}.mp3"
    try:
        await edge_tts.Communicate(text, voice, rate=rate).save(str(dest))
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(502, f"edge-tts 생성 실패(인터넷 연결 확인): {exc}") from exc
    if not dest.exists() or dest.stat().st_size == 0:
        raise HTTPException(502, "edge-tts가 빈 파일을 만들었습니다. 보이스 이름을 확인하세요.")

    dur = probe_duration(dest)
    meta = {"id": aid, "name": text[:40], "path": str(dest), "duration": dur,
            "source": "edge-tts", "voice": voice, "rate": rate, "text": text,
            "created_at": time.time()}
    store.save_audio_meta(meta)
    return meta


# ------------------------------------------------------------------ 썸네일 이미지
@app.post("/api/images")
async def api_upload_image(file: UploadFile = File(...)) -> Dict[str, Any]:
    ext = _ext_of(file.filename or "", settings.IMAGE_EXTS, ".png")
    iid = store.new_id()
    dest = settings.IMAGE_DIR / f"{iid}{ext}"
    await _save_upload(file, dest)
    meta = {"id": iid, "name": file.filename or dest.name, "path": str(dest),
            "created_at": time.time()}
    store.save_image_meta(meta)
    return meta


@app.get("/api/images/{iid}/file")
def api_image_file(iid: str) -> FileResponse:
    meta = store.get_image(iid)
    if not meta:
        raise HTTPException(404, "이미지 없음")
    return FileResponse(meta["path"])


# ------------------------------------------------------------------ 자막
@app.post("/api/subtitles")
async def api_upload_subtitles(file: UploadFile = File(...)) -> Dict[str, Any]:
    ext = _ext_of(file.filename or "", settings.SUB_EXTS, ".srt")
    sid = store.new_id()
    tmp = settings.SUB_DIR / f"{sid}{ext}"
    await _save_upload(file, tmp)
    out = settings.SUB_DIR / f"{sid}.ass"
    try:
        if ext == ".srt":
            subtitles.srt_to_ass(tmp, out)
        elif ext in (".ass", ".ssa"):
            shutil.copyfile(tmp, out)
        else:  # vtt -> srt처럼 파싱
            subtitles.build_ass(subtitles.parse_srt(tmp.read_text(encoding="utf-8", errors="ignore")), out)
    finally:
        if tmp != out:
            tmp.unlink(missing_ok=True)
    count = sum(1 for ln in out.read_text(encoding="utf-8", errors="ignore").splitlines()
                if ln.startswith("Dialogue:"))
    return {"id": sid, "name": file.filename, "segments": count}


@app.post("/api/transcribe")
async def api_transcribe(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    aid = payload.get("audio_id")
    meta = store.audio_meta(aid)
    if not meta:
        raise HTTPException(400, "자막을 만들 오디오를 먼저 선택하세요.")
    sid = store.new_id()
    out = settings.SUB_DIR / f"{sid}.ass"
    model_size = str(payload.get("model") or "small")
    word_level = bool(payload.get("word_level", False))
    try:
        info = await asyncio.to_thread(
            subtitles.transcribe_to_ass, Path(meta["path"]), out,
            model_size, str(payload.get("language") or "ko"), word_level,
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"자막 생성 실패: {exc}") from exc
    return {"id": sid, "audio_id": aid, **info}


# ------------------------------------------------------------------ 렌더 잡
def _render_worker(jid: str, project: Dict[str, Any], out_path: Path) -> None:
    def progress(pct: float, note: str) -> None:
        _job_set(jid, progress=round(pct * 100, 1), note=note, status="running")

    _job_set(jid, status="running", progress=0.0, note="준비")
    try:
        result = render_video(project, out_path, progress)
        _job_set(jid, status="done", progress=100.0, note="완료", result=result,
                 download=f"/api/download/{jid}")
    except RenderError as exc:
        _job_set(jid, status="error", note="렌더 실패", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        _job_set(jid, status="error", note="렌더 실패", error=f"{type(exc).__name__}: {exc}")


@app.post("/api/render")
def api_render(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    ok, msg = media_tools_ready()
    if not ok:
        raise HTTPException(500, msg)
    project = payload.get("project") if isinstance(payload.get("project"), dict) else payload
    clips = project.get("clips") or []
    if not clips:
        raise HTTPException(400, "클립이 없습니다.")
    jid = uuid.uuid4().hex[:12]
    out_path = settings.OUTPUT_DIR / f"shorts_{jid}.mp4"
    (settings.JOB_DIR / jid).mkdir(parents=True, exist_ok=True)
    (settings.JOB_DIR / jid / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _job_set(jid, id=jid, status="queued", progress=0.0, note="대기", created_at=time.time())
    threading.Thread(target=_render_worker, args=(jid, project, out_path), daemon=True).start()
    return {"job_id": jid}


@app.get("/api/jobs/{jid}")
def api_job(jid: str) -> Dict[str, Any]:
    job = _job_get(jid)
    if not job:
        raise HTTPException(404, "잡 없음")
    return job


@app.get("/api/download/{jid}")
def api_download(jid: str) -> FileResponse:
    out = settings.OUTPUT_DIR / f"shorts_{jid}.mp4"
    if not out.exists():
        raise HTTPException(404, "아직 렌더 결과가 없습니다.")
    return FileResponse(out, media_type="video/mp4", filename=f"shorts_{jid}.mp4")


# ------------------------------------------------------------------ 썸네일
@app.post("/api/thumbnail")
def api_thumbnail(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    project = payload.get("project") if isinstance(payload.get("project"), dict) else payload
    tid = uuid.uuid4().hex[:12]
    out = settings.OUTPUT_DIR / f"thumb_{tid}.png"
    try:
        render_thumbnail(project, out)
    except RenderError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": tid, "download": f"/api/download/thumb/{tid}", "path": str(out)}


@app.get("/api/download/thumb/{tid}")
def api_download_thumb(tid: str) -> FileResponse:
    out = settings.OUTPUT_DIR / f"thumb_{tid}.png"
    if not out.exists():
        raise HTTPException(404, "썸네일 없음")
    return FileResponse(out, media_type="image/png", filename=f"thumbnail_{tid}.png")


# ------------------------------------------------------------------ 완전자동 · 일괄 제작
BATCH = BatchQueue()


def _auto_defaults(p: Dict[str, Any]) -> Dict[str, Any]:
    """폼 공통 옵션(성우·속도·크롭·자막)만 추려 요청에 병합한다."""
    keys = ("voice", "rate", "crop_pct", "subtitle_style", "title_preset", "title_bar", "auto_search")
    return {k: p[k] for k in keys if p.get(k) not in (None, "")}


@app.post("/api/product/preview")
def api_product_preview(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """링크 하나를 읽어 상품명·가격·특징·키워드를 미리 보여 준다(막히면 manual_needed)."""
    url = str(payload.get("url") or "").strip()
    try:
        info = scraper.fetch_product(url)
    except scraper.ScrapeBlocked as exc:
        return {"ok": False, "manual_needed": True, "message": str(exc)}
    except scraper.ScrapeError as exc:
        return {"ok": False, "manual_needed": False, "message": str(exc)}
    kw = source_fetcher.build_keywords(info)
    return {"ok": True, "product": info.to_dict(), "keywords": kw,
            "search_links": source_fetcher.manual_search_links(kw)}


@app.post("/api/batch")
def api_batch_create(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """링크(줄바꿈 구분) 여러 개 -> 큐 등록. 줄에 '상품명 | 특징 | 가격' 이면 직접 입력 항목."""
    ok, msg = media_tools_ready()
    if not ok:
        raise HTTPException(500, msg)
    common = _auto_defaults(payload)
    extra = [u for u in (payload.get("extra_urls") or []) if str(u).strip()]
    added: List[Dict[str, Any]] = []
    lines = [ln.strip() for ln in str(payload.get("text") or "").splitlines() if ln.strip()]
    pending_url: Optional[str] = None
    entries: List[Dict[str, Any]] = []
    for ln in lines:
        urls = scraper.extract_urls(ln)
        manual = None if urls else pipeline.parse_manual_text(ln)
        if urls:
            if pending_url:
                entries.append({"url": pending_url})
            pending_url = urls[0]
        elif manual and pending_url:          # 바로 윗줄 URL의 직접 입력 보정
            entries.append({"url": pending_url, "manual": manual})
            pending_url = None
        elif manual:
            entries.append({"manual": manual})
    if pending_url:
        entries.append({"url": pending_url})
    if not entries:
        raise HTTPException(400, "상품 링크가 없습니다. 한 줄에 하나씩 붙여 넣어 주세요.")
    if len(entries) > 30:
        raise HTTPException(400, "한 번에 30개까지만 등록할 수 있어요.")
    for e in entries:
        req = {**common, **e, "extra_urls": extra}
        added.append(BATCH.add(req, label=pipeline.AutoRequest.from_dict(req).label()))
    return {"items": added}


@app.get("/api/batch")
def api_batch_list() -> Dict[str, Any]:
    items = []
    for it in BATCH.list():
        slim = {k: v for k, v in it.items() if k not in ("request", "result")}
        res = it.get("result") or {}
        slim["warnings"] = res.get("warnings", [])
        slim["script"] = res.get("script", [])
        slim["duration"] = res.get("duration")
        slim["search_links"] = res.get("search_links", {})
        items.append(slim)
    return {"items": items}


@app.post("/api/batch/{item_id}/retry")
def api_batch_retry(item_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    manual = None
    if payload.get("name"):
        manual = {"name": payload["name"], "features": payload.get("features", ""), "price": payload.get("price", "")}
    if not BATCH.retry(item_id, manual):
        raise HTTPException(400, "다시 시도할 수 없는 상태입니다.")
    return {"ok": True}


@app.delete("/api/batch/{item_id}")
def api_batch_delete(item_id: str) -> Dict[str, Any]:
    if not BATCH.remove(item_id):
        raise HTTPException(400, "진행 중인 항목은 지울 수 없어요.")
    return {"ok": True}


@app.on_event("startup")
def _start_bot() -> None:
    bot.start_in_background(BATCH)         # TELEGRAM_BOT_TOKEN 이 있을 때만 동작


# ------------------------------------------------------------------ 정적 UI
if settings.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    idx = settings.STATIC_DIR / "index.html"
    if not idx.exists():
        raise HTTPException(500, "app/static/index.html 이 없습니다.")
    return FileResponse(idx)


@app.get("/healthz")
def healthz() -> JSONResponse:
    ok, msg = media_tools_ready()
    return JSONResponse({"ok": ok, "detail": msg})
