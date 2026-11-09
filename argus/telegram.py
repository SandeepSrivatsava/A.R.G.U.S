"""Telegram channel, written for Argus: long-polling (no open ports), only your user ID is answered,
risky actions are approved with buttons, the Mac's screen stays off-limits unless you allowed it."""
from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from .agent import Agent, Events
from .approvals import DENY, ONCE, TASK, ApprovalRequest
from .audit import Audit
from .llm import connect
from .store import Store

log = logging.getLogger("argus.telegram")
API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str, base: str = API):
        self.token, self.base = token, base.rstrip("/")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30) -> Any:
        conn = connect(self.base, timeout)
        try:
            conn.request("POST", f"/bot{self.token}/{method}", body=json.dumps(params or {}).encode(),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read() or b"{}")
        except (OSError, ValueError) as exc:
            raise TelegramError(f"{method} failed: {exc}") from exc
        finally:
            conn.close()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description') or 'error'}")
        return data.get("result")

    def download(self, file_path: str, limit: int = 10_000_000) -> bytes:
        conn = connect(self.base, 60)
        try:
            conn.request("GET", f"/file/bot{self.token}/{file_path}")
            resp = conn.getresponse()
            if resp.status != 200:
                raise TelegramError(f"download failed: HTTP {resp.status}")
            return resp.read(limit)
        finally:
            conn.close()


class _ChatEvents(Events):
    def __init__(self, bot: "TelegramBot", chat_id: int, cancel: threading.Event):
        self.bot, self.chat_id, self.cancel = bot, chat_id, cancel
        self.parts: List[str] = []

    def text(self, delta: str) -> None:
        self.parts.append(delta)

    def approve(self, request: ApprovalRequest) -> str:
        return self.bot.ask_approval(self.chat_id, request, self.cancel)


class TelegramBot:
    def __init__(self, api: TelegramAPI, user_id: int, agent: Agent, store: Store, audit: Audit, *,
                 idle_minutes: float = 360, approval_timeout: float = 120):
        self.api = api
        self.user_id = int(user_id)
        self.agent = agent
        self.store = store
        self.audit = audit
        self.idle_minutes = idle_minutes
        self.approval_timeout = approval_timeout
        self._stop = threading.Event()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # ---- outgoing ----------------------------------------------------------------------------------
    def send(self, text: str, chat_id: Optional[int] = None, markup: Optional[Dict[str, Any]] = None) -> Optional[int]:
        chat = chat_id or self.user_id
        if not chat or not text:
            return None
        message_id = None
        for i in range(0, len(text), 3900):
            params: Dict[str, Any] = {"chat_id": chat, "text": text[i:i + 3900], "disable_web_page_preview": True}
            if markup and i + 3900 >= len(text):
                params["reply_markup"] = markup
            try:
                message_id = self.api.call("sendMessage", params)["message_id"]
            except (TelegramError, KeyError, TypeError) as exc:
                log.warning("couldn't send a Telegram message: %s", exc)
                return None
        return message_id

    def notify(self, text: str) -> bool:
        return self.send(text) is not None

    # ---- approvals with buttons --------------------------------------------------------------------
    def ask_approval(self, chat_id: int, request: ApprovalRequest, cancel: threading.Event) -> str:
        nonce = secrets.token_hex(6)
        waiter = {"event": threading.Event(), "choice": DENY}
        with self._lock:
            self._pending[nonce] = waiter
        buttons = [[{"text": "Allow once", "callback_data": f"ap:{nonce}:{ONCE}"}]]
        if request.task_scope_offered:
            buttons[0].append({"text": "Allow for this task", "callback_data": f"ap:{nonce}:{TASK}"})
        buttons[0].append({"text": "Deny", "callback_data": f"ap:{nonce}:{DENY}"})
        text = f"Argus wants to:\n{request.summary()}" + (f"\n({request.description})" if request.description
                                                           and request.description not in request.summary() else "")
        message_id = self.send(text, chat_id, {"inline_keyboard": buttons})
        deadline = time.monotonic() + self.approval_timeout
        while not waiter["event"].wait(0.25):
            if cancel.is_set() or time.monotonic() > deadline or message_id is None:
                break
        with self._lock:
            self._pending.pop(nonce, None)
        choice = waiter["choice"] if waiter["event"].is_set() else DENY
        if message_id is not None:
            label = {ONCE: "Allowed once", TASK: "Allowed for this task"}.get(choice, "Denied")
            try:
                self.api.call("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                                  "text": f"{text}\n\n→ {label}"})
            except TelegramError:
                pass
        return choice

    # ---- incoming ----------------------------------------------------------------------------------
    def _authorised(self, user: Dict[str, Any], chat: Dict[str, Any]) -> bool:
        ok = bool(self.user_id) and int(user.get("id") or 0) == self.user_id and chat.get("type") == "private"
        if not ok:
            self.audit.log("telegram_stranger", user=user.get("id"), chat_type=chat.get("type"))
        return ok

    def handle_update(self, update: Dict[str, Any]) -> None:
        if "callback_query" in update:
            cb = update["callback_query"]
            if not self._authorised(cb.get("from") or {}, (cb.get("message") or {}).get("chat") or {}):
                return
            parts = str(cb.get("data") or "").split(":")
            if len(parts) == 3 and parts[0] == "ap":
                with self._lock:
                    waiter = self._pending.get(parts[1])
                if waiter and parts[2] in (ONCE, TASK, DENY):
                    waiter["choice"] = parts[2]
                    waiter["event"].set()
            try:
                self.api.call("answerCallbackQuery", {"callback_query_id": cb.get("id")})
            except TelegramError:
                pass
            return
        msg = update.get("message") or {}
        if not msg or not self._authorised(msg.get("from") or {}, msg.get("chat") or {}):
            return
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if text in ("/start", "/help"):
            self.send("Argus here. Ask me anything, send a photo, or use /new for a fresh conversation and /stop "
                      "to cancel.", chat_id)
            return
        if text == "/new":
            self.store.new_session("telegram", str(chat_id))
            self.send("Fresh start.", chat_id)
            return
        if text == "/stop":
            self._cancel.set()
            self.send("Stopping.", chat_id)
            return
        content: Any = text
        if msg.get("photo"):
            try:
                best = msg["photo"][-1]
                path = self.api.call("getFile", {"file_id": best["file_id"]})["file_path"]
                data = self.api.download(path)
                content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.b64encode(data).decode()}},
                           {"type": "text", "text": text or "What's in this photo?"}]
            except (TelegramError, KeyError, TypeError) as exc:
                self.send(f"I couldn't open that photo ({exc}).", chat_id)
                return
        elif msg.get("voice") or msg.get("audio"):
            self.send("I can't listen to voice notes yet. Type it, or talk to me at the Mac.", chat_id)
            return
        if not content:
            return
        if self._worker is not None and self._worker.is_alive():
            self.send("Still working on your last message. Send /stop to cancel it.", chat_id)
            return
        self._cancel = threading.Event()
        self._worker = threading.Thread(target=self._answer, args=(chat_id, content, self._cancel), daemon=True)
        self._worker.start()

    def _answer(self, chat_id: int, content: Any, cancel: threading.Event) -> None:
        typing = threading.Event()

        def keep_typing():
            while not typing.wait(4.5):
                try:
                    self.api.call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
                except TelegramError:
                    pass

        threading.Thread(target=keep_typing, daemon=True).start()
        events = _ChatEvents(self, chat_id, cancel)
        try:
            session = self.store.session_for("telegram", str(chat_id), self.idle_minutes)
            turn = self.agent.run(session_id=session, channel="telegram", content=content, events=events, cancel=cancel)
            reply = turn.text.strip() or "".join(events.parts).strip()
            if turn.error:
                reply = (reply + "\n\n" if reply else "") + f"(Problem: {turn.error})"
            if turn.stopped:
                reply = reply or "Stopped."
        except Exception as exc:
            log.exception("Telegram request failed")
            reply = f"Sorry, that failed: {exc}"
        finally:
            typing.set()
        self.send(reply or "Done.", chat_id)

    # ---- the polling loop --------------------------------------------------------------------------
    def poll_once(self, timeout: int = 50) -> int:
        offset = int(self.store.get("telegram_offset", "0") or 0)
        updates = self.api.call("getUpdates", {"offset": offset, "timeout": timeout,
                                               "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 15)
        for update in updates or []:
            self.store.set("telegram_offset", str(int(update["update_id"]) + 1))
            try:
                self.handle_update(update)
            except Exception:
                log.exception("couldn't handle a Telegram update")
        return len(updates or [])

    def run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self.poll_once()
                backoff = 2.0
            except TelegramError as exc:
                log.warning("%s", exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)

    def start(self) -> None:
        threading.Thread(target=self.run, name="argus-telegram", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
