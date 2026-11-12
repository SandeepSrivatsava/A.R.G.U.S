"""Voice front-end to the agent: runs what you said through Argus's brain and speaks the answer as it
streams in, sentence by sentence. Approvals go to the screen + Touch ID, never to your voice."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

from .agent import Agent, Events
from .approvals import ApprovalRequest, Approver, DenyAll
from .audit import Audit
from .store import Store
from .voice import SentenceBuffer, Speaker

log = logging.getLogger("argus.brain")


class Sessions:
    """One conversation thread per channel, replaced after a quiet spell or on request."""

    def __init__(self, store: Store, channel: str = "voice", key: str = "mac", idle_minutes: float = 30.0):
        self.store, self.channel, self.key, self.idle = store, channel, key, idle_minutes

    def current(self) -> str:
        return self.store.session_for(self.channel, self.key, self.idle)

    def rotate(self) -> None:
        self.store.new_session(self.channel, self.key)


def spoken_error(error: str) -> str:
    text = error.lower()
    if "rejected" in text or "401" in text:
        return "My Claude API key isn't working. Please check it."
    if "credit" in text:
        return "The Claude account is out of credit."
    if "token budget" in text or "too many steps" in text:
        return "That was getting long, so I stopped there. Tell me if you want me to keep going."
    if "cut off" in text:
        return "I lost the connection partway through. Please ask again."
    if "reach" in text or "connection" in text or "quiet" in text:
        return "I can't reach my brain right now. Check the internet connection."
    return "Sorry, something went wrong with that."


class _VoiceEvents(Events):
    def __init__(self, brain: "Brain", cancelled: threading.Event, approver: Optional[Approver] = None):
        self.brain, self.cancelled = brain, cancelled
        self.approver = approver or brain.approver
        self.buffer = SentenceBuffer()
        self.spoke = False
        self.started = time.monotonic()
        self.first_word_at: Optional[float] = None

    def say(self, sentence: str) -> None:
        if not self.cancelled.is_set():
            if self.first_word_at is None:
                self.first_word_at = time.monotonic()
            self.brain.speaker.say(sentence)
            self.spoke = True

    def flush(self) -> None:
        for sentence in self.buffer.flush():
            self.say(sentence)

    def text(self, delta: str) -> None:
        for sentence in self.buffer.feed(delta):
            self.say(sentence)

    def tool_started(self, name: str, description: str) -> None:
        if not self.spoke and time.monotonic() - self.started > self.brain.progress_after:
            self.say("One moment.")

    def approve(self, request: ApprovalRequest) -> str:
        self.flush()
        if self.cancelled.is_set():
            return "deny"
        self.approver.prepare(request)          # picks the spoken code word, if a spoken answer is allowed
        self.say(request.spoken())
        if self.cancelled.is_set():
            self.approver.withdraw(request)
            return "deny"
        return self.approver.ask(request)


class Brain:
    def __init__(self, agent: Agent, speaker: Speaker, approver: Approver, audit: Audit, sessions: Sessions, *,
                 on_reply_finished: Callable[[], None] = lambda: None, progress_after: float = 1.5,
                 guest_sessions: Optional[Sessions] = None):
        self.agent = agent
        self.speaker = speaker
        self.approver = approver
        self.audit = audit
        self.sessions = sessions
        self.guest_sessions = guest_sessions or Sessions(sessions.store, "guest", "mac", 10.0)
        self.on_reply_finished = on_reply_finished
        self.progress_after = progress_after
        self._thread: Optional[threading.Thread] = None
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def ask(self, text: str, guest: bool = False, heard: Optional[Dict[str, float]] = None) -> None:
        """Handle a request in the background (cancelling any request still running).
        guest=True: someone other than the owner, whom the owner allowed: no personal context, no Mac."""
        with self._lock:
            if self.busy:
                self.cancel()                       # cancelling also cuts the old request's connection,
                self._thread.join(timeout=3)        # so the old turn ends almost at once
            self._cancelled = flag = threading.Event()
            self._thread = threading.Thread(target=self._handle, args=(text, flag, guest, heard or {}),
                                            name="argus-brain", daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()
        self.speaker.stop()

    def warm(self) -> None:
        """Get a connection to Claude ready before it's needed, so the answer starts sooner."""
        warm = getattr(self.agent.llm, "warm", None)
        if warm is not None:
            warm()

    def wait(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _handle(self, text: str, cancelled: threading.Event, guest: bool = False,
                heard: Optional[Dict[str, float]] = None) -> None:
        events = _VoiceEvents(self, cancelled, DenyAll() if guest else None)
        sessions = self.guest_sessions if guest else self.sessions
        try:
            turn = self.agent.run(session_id=sessions.current(), channel="guest" if guest else "voice", content=text,
                                  events=events, cancel=cancelled)
        except Exception as exc:  # never let one request take the voice loop down
            log.exception("request failed")
            self.last_error = str(exc)
            events.say(spoken_error(str(exc)))
            turn = None
        events.flush()
        if events.first_word_at is not None:      # where the wait went: `argusctl timing` shows these
            self.audit.log("timing", **dict(heard or {}), first_word_s=round(events.first_word_at - events.started, 2),
                           **(turn.timings if turn is not None else {}))
        if turn is not None:
            self.last_error = turn.error
            if turn.error and not cancelled.is_set():
                events.say(spoken_error(turn.error))
            if turn.stopped:
                self.audit.log("run_stopped")
        if not cancelled.is_set():
            self.speaker.wait_idle(timeout=600)
            if not cancelled.is_set():
                self.on_reply_finished()
