"""일괄 제작 큐: 상품 여러 개를 순차(동시 1개)로 파이프라인에 태운다.

웹 UI와 텔레그램 봇이 같은 큐를 공유한다. 러너는 주입 가능(테스트에서 가짜 사용).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from . import pipeline, settings

Runner = Callable[..., Dict[str, Any]]
Listener = Callable[[Dict[str, Any]], None]

STATE_FILE = settings.JOB_DIR / "batch.json"


class BatchQueue:
    def __init__(self, runner: Optional[Runner] = None, persist: bool = True, **runner_kw: Any) -> None:
        self._runner = runner or pipeline.run_pipeline
        self._kw = runner_kw
        self._items: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._pending: List[str] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._listeners: List[Listener] = []
        self._thread: Optional[threading.Thread] = None
        self._persist = persist
        if persist:
            self._load()

    # ---- 구독/조회
    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(self._items[i]) for i in self._order]

    def get(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            it = self._items.get(item_id)
            return dict(it) if it else None

    # ---- 등록
    def add(self, request: Dict[str, Any], label: str = "", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        item_id = uuid.uuid4().hex[:12]
        item = {"id": item_id, "label": label or (request.get("url") or "상품")[:60], "request": request,
                "meta": meta or {}, "status": "queued", "progress": 0.0, "note": "대기 중",
                "created_at": time.time(), "download": None, "error": None, "result": None}
        with self._lock:
            self._items[item_id] = item
            self._order.append(item_id)
            self._pending.append(item_id)
            ahead = len(self._pending) - 1
        self._save()
        self._ensure_thread()
        self._wake.set()
        return {**item, "ahead": ahead}

    def retry(self, item_id: str, manual: Optional[Dict[str, Any]] = None) -> bool:
        """막힘/실패 항목을 (수동 입력을 더해) 다시 줄 세운다."""
        with self._lock:
            it = self._items.get(item_id)
            if not it or it["status"] not in ("error", "needs_manual"):
                return False
            if manual:
                it["request"]["manual"] = manual
            it.update(status="queued", progress=0.0, note="대기 중", error=None)
            self._pending.append(item_id)
        self._save()
        self._ensure_thread()
        self._wake.set()
        return True

    def remove(self, item_id: str) -> bool:
        with self._lock:
            it = self._items.get(item_id)
            if not it or it["status"] == "running":
                return False
            self._items.pop(item_id)
            self._order.remove(item_id)
            if item_id in self._pending:
                self._pending.remove(item_id)
        self._save()
        return True

    def wait_idle(self, timeout: float = 60.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                busy = bool(self._pending) or any(i["status"] == "running" for i in self._items.values())
            if not busy:
                return True
            time.sleep(0.05)
        return False

    # ---- 내부
    def _set(self, item_id: str, **kw: Any) -> None:
        with self._lock:
            if item_id in self._items:
                self._items[item_id].update(kw)

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="batch-worker")
        self._thread.start()

    def _emit(self, item_id: str) -> None:
        snap = self.get(item_id)
        for fn in list(self._listeners):
            try:
                fn(snap or {})
            except Exception:  # noqa: BLE001  리스너 오류가 큐를 멈추면 안 됨
                pass

    def _loop(self) -> None:
        while True:
            with self._lock:
                item_id = self._pending.pop(0) if self._pending else None
            if item_id is None:
                if not self._wake.wait(timeout=5):
                    with self._lock:
                        if not self._pending:
                            return
                self._wake.clear()
                continue
            self._run_one(item_id)

    def _run_one(self, item_id: str) -> None:
        item = self.get(item_id)
        if not item:
            return
        self._set(item_id, status="running", note="시작", progress=1.0)
        self._emit(item_id)

        def progress(pct: float, note: str) -> None:
            self._set(item_id, progress=round(float(pct), 1), note=note)

        try:
            req = pipeline.AutoRequest.from_dict(item["request"])
            result = self._runner(req, item_id, progress=progress, **self._kw)
            self._set(item_id, status="done", progress=100.0, note="완료", result=result,
                      download=f"/api/download/{item_id}")
        except pipeline.NeedsManualInput as exc:
            self._set(item_id, status="needs_manual", note="직접 입력 필요", error=str(exc))
        except pipeline.PipelineError as exc:
            self._set(item_id, status="error", note="실패", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            self._set(item_id, status="error", note="실패", error=f"{type(exc).__name__}: {exc}")
        self._save()
        self._emit(item_id)

    def _save(self) -> None:
        if not self._persist:
            return
        try:
            with self._lock:
                slim = [{k: v for k, v in self._items[i].items() if k != "result"} for i in self._order]
            STATE_FILE.write_text(json.dumps(slim, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _load(self) -> None:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for it in data:
            if it.get("status") in ("queued", "running"):       # 서버 재시작으로 끊긴 항목
                it.update(status="error", note="서버 재시작으로 중단됨", error="다시 시도를 눌러 주세요.")
            self._items[it["id"]] = it
            self._order.append(it["id"])
