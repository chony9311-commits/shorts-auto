"""텔레그램 봇: 상품 링크를 보내면 백그라운드 큐로 순차 제작 -> 완료 보고 + MP4 전송.

실행:  python -m app.bot          (웹 서버 없이 봇만)
       또는 app.main 기동 시 TELEGRAM_BOT_TOKEN 이 있으면 같이 뜬다.
.env:  TELEGRAM_BOT_TOKEN=...,  TELEGRAM_ALLOWED_USER_IDS=123,456  (비우면 전부 거절 + 내 ID 안내)

핵심 로직(BotService)은 텔레그램에 의존하지 않아 가짜 sender 로 테스트한다.
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Set

from . import pipeline, script_gen
from .batch_queue import BatchQueue
from .scraper import extract_urls

TG_VIDEO_LIMIT = 49 * 1024 * 1024       # 봇 API 업로드 상한(50MB)보다 살짝 작게

HELP = (
    "상품 링크를 보내면 쇼츠를 만들어 드려요.\n"
    "• 링크 여러 개를 한 번에 보내도 순서대로 만들어요.\n"
    "• 링크가 막히면: 링크 아래 줄에  상품명 | 특징1, 특징2 | 19,800원  을 같이 보내세요.\n"
    "• 링크 없이  상품명 | 특징1, 특징2  만 보내도 돼요(영상은 자동 검색).\n"
    "• 추가 영상 URL(샤오홍슈/도우인/틱톡)도 같은 메시지에 같이 붙이면 소스로 써요.\n"
    "명령어: /voice female|male · /rate +18% · /status"
)


class Sender(Protocol):
    def send_text(self, chat_id: int, text: str) -> None: ...
    def send_video(self, chat_id: int, path: str, caption: str) -> None: ...


def allowed_ids(raw: Optional[str] = None) -> Set[int]:
    raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "") if raw is None else raw
    out: Set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def _is_source_url(url: str) -> bool:
    """상품몰이 아닌 영상 플랫폼 URL이면 소스로 취급."""
    return any(k in url for k in ("youtube.com", "youtu.be", "tiktok.com", "douyin.com", "xiaohongshu.com",
                                  "xhslink.com", "bilibili.com", "instagram.com", "v.douyin"))


class BotService:
    def __init__(self, queue: BatchQueue, sender: Sender, allowed: Optional[Set[int]] = None) -> None:
        self.queue, self.sender = queue, sender
        self.allowed = allowed if allowed is not None else allowed_ids()
        self.prefs: Dict[int, Dict[str, str]] = {}
        queue.subscribe(self._on_item)

    # ---- 들어오는 메시지
    def handle_text(self, user_id: int, chat_id: int, text: str) -> None:
        text = (text or "").strip()
        if not self.allowed or user_id not in self.allowed:
            self.sender.send_text(chat_id, f"허용된 사용자가 아니에요. 내 ID는 {user_id} 입니다. "
                                           ".env 의 TELEGRAM_ALLOWED_USER_IDS 에 추가하고 서버를 다시 켜 주세요.")
            return
        if text.startswith("/"):
            return self._command(chat_id, text)
        self._enqueue(chat_id, text)

    def _command(self, chat_id: int, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        pref = self.prefs.setdefault(chat_id, {})
        if cmd in ("/start", "/help"):
            self.sender.send_text(chat_id, HELP)
        elif cmd == "/voice":
            if arg not in script_gen.VOICES:
                self.sender.send_text(chat_id, "female 또는 male 로 골라 주세요. 예) /voice male")
            else:
                pref["voice"] = script_gen.VOICES[arg]
                self.sender.send_text(chat_id, f"성우를 {arg} 로 바꿨어요.")
        elif cmd == "/rate":
            if not any(ch.isdigit() for ch in arg):
                self.sender.send_text(chat_id, "속도 형식이 달라요. 예) /rate +18%")
            else:
                pref["rate"] = script_gen.normalize_rate(arg)
                self.sender.send_text(chat_id, f"말 속도를 {pref['rate']} 로 바꿨어요.")
        elif cmd == "/status":
            mine = [i for i in self.queue.list() if i["meta"].get("chat_id") == chat_id][-8:]
            if not mine:
                self.sender.send_text(chat_id, "대기/진행 중인 작업이 없어요.")
            else:
                icon = {"queued": "⏳", "running": "🎬", "done": "✅", "error": "❌", "needs_manual": "✍️"}
                self.sender.send_text(chat_id, "\n".join(
                    f"{icon.get(i['status'], '·')} {i['label']} — {i['note']} ({i['progress']:.0f}%)" for i in mine))
        else:
            self.sender.send_text(chat_id, HELP)

    def _enqueue(self, chat_id: int, text: str) -> None:
        urls = extract_urls(text)
        shop = [u for u in urls if not _is_source_url(u)]
        extra = [u for u in urls if _is_source_url(u)]
        manual = None
        for line in text.splitlines():
            m = pipeline.parse_manual_text(line) if not extract_urls(line) else None
            if m:
                manual = m
                break
        pref = self.prefs.get(chat_id, {})
        base = {**pref, "extra_urls": extra}
        reqs: List[Dict[str, Any]] = []
        if shop:
            for n, u in enumerate(shop):
                reqs.append({**base, "url": u, "manual": manual if (manual and len(shop) == 1) else None})
        elif manual:
            reqs.append({**base, "manual": manual})
        if not reqs:
            self.sender.send_text(chat_id, "상품 링크를 못 찾았어요. /help 를 눌러 보세요.")
            return
        for r in reqs:
            it = self.queue.add(r, label=pipeline.AutoRequest.from_dict(r).label(), meta={"chat_id": chat_id})
            self.sender.send_text(chat_id, f"접수했어요 ✅ {it['label']}\n앞에 {it['ahead']}건 대기 중. 끝나면 영상 보내 드릴게요.")

    # ---- 큐 이벤트
    def _on_item(self, item: Dict[str, Any]) -> None:
        chat_id = item.get("meta", {}).get("chat_id")
        if chat_id is None:
            return
        status = item.get("status")
        if status == "running":
            self.sender.send_text(chat_id, f"🎬 제작 시작: {item['label']}")
        elif status == "needs_manual":
            self.sender.send_text(chat_id, f"✍️ {item['error']}\n(링크 + 다음 줄에 직접 입력해서 다시 보내 주세요)")
        elif status == "error":
            self.sender.send_text(chat_id, f"❌ 실패: {item['label']}\n{item['error']}")
        elif status == "done":
            res = item.get("result") or {}
            path = Path(res.get("path", ""))
            cap = f"✅ 완료: {item['label']} · {res.get('duration', 0):.1f}초"
            if res.get("warnings"):
                cap += "\n⚠️ " + " / ".join(res["warnings"][:3])
            thumb = res.get("thumbnail")
            if thumb and Path(thumb).is_file() and hasattr(self.sender, "send_photo"):
                self.sender.send_photo(chat_id, thumb, "썸네일")
            if path.is_file() and path.stat().st_size <= TG_VIDEO_LIMIT:
                self.sender.send_video(chat_id, str(path), cap[:1000])
            else:
                self.sender.send_text(chat_id, cap + f"\n파일이 커서 직접 전송은 못 했어요. 저장 위치: {path}")


# ---------------------------------------------------------------- python-telegram-bot 어댑터
class _PtbSender:
    """PTB(비동기)를 큐 워커 스레드(동기)에서 부르기 위한 브리지."""

    def __init__(self, app: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.app, self.loop = app, loop

    def _run(self, coro: Any) -> None:
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=300)
        except Exception as exc:  # noqa: BLE001
            print(f"[bot] 전송 실패: {exc}")

    def send_text(self, chat_id: int, text: str) -> None:
        self._run(self.app.bot.send_message(chat_id=chat_id, text=text))

    def send_photo(self, chat_id: int, path: str, caption: str) -> None:
        async def go() -> None:
            with open(path, "rb") as fh:
                await self.app.bot.send_photo(chat_id=chat_id, photo=fh, caption=caption)
        self._run(go())

    def send_video(self, chat_id: int, path: str, caption: str) -> None:
        async def go() -> None:
            with open(path, "rb") as fh:
                await self.app.bot.send_video(chat_id=chat_id, video=fh, caption=caption,
                                              supports_streaming=True, width=1080, height=1920,
                                              read_timeout=180, write_timeout=180)
        self._run(go())


def build_application(queue: BatchQueue, token: str) -> Any:
    """PTB Application 생성(핸들러 등록). python-telegram-bot v20+ 필요."""
    from telegram.ext import Application, MessageHandler, filters  # 지연 import

    async def post_init(app: Any) -> None:
        svc = BotService(queue, _PtbSender(app, asyncio.get_running_loop()))
        app.bot_data["svc"] = svc

    async def on_message(update: Any, _ctx: Any) -> None:
        msg = update.effective_message
        if not msg or not msg.text or not update.effective_user:
            return
        svc: BotService = _ctx.application.bot_data["svc"]
        await asyncio.to_thread(svc.handle_text, update.effective_user.id, msg.chat_id, msg.text)

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT, on_message))     # 명령어(/..)도 텍스트로 받아 직접 분기
    return app


def start_in_background(queue: BatchQueue) -> Optional[threading.Thread]:
    """웹 서버와 같은 프로세스에서 봇을 별도 스레드로 띄운다. 토큰/라이브러리가 없으면 조용히 건너뜀."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    try:
        import telegram  # noqa: F401
    except ImportError:
        print("[bot] python-telegram-bot 이 설치돼 있지 않아 봇을 건너뜁니다. (pip install python-telegram-bot)")
        return None

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = build_application(queue, token)
        print("[bot] 텔레그램 봇 시작")
        app.run_polling(stop_signals=None, close_loop=False)    # 서브스레드에서는 시그널 핸들러 불가

    t = threading.Thread(target=runner, daemon=True, name="telegram-bot")
    t.start()
    return t


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 이 없습니다. .env 에 넣어 주세요 (.env.example 참고).")
    app = build_application(BatchQueue(), token)
    print("[bot] 텔레그램 봇 시작 (Ctrl+C 로 종료)")
    app.run_polling()


if __name__ == "__main__":
    main()
