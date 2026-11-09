"""Voice front-end to the agent: runs what you said through Argus's brain and speaks the answer as it
streams in, sentence by sentence. Approvals go to the screen + Touch ID, never to your voice."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .agent import Agent, Events
from .approvals import ApprovalRequest, Approver
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
    if "reach" in text or "connection" in text or "quiet" in text:
        return "I can't reach my brain right now. Check the internet connection."
    return "Sorry, something went wrong with that."


class _VoiceEvents(Events):
    def __init__(self, brain: "Brain", cancelled: threading.Event):
        self.brain, self.cancelled = brain, cancelled
        self.buffer = SentenceBuffer()
        self.spoke = False
        self.started = time.monotonic()

    def say(self, sentence: str) -> None:
        if not self.cancelled.is_set():
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
        self.say(request.spoken())
        if self.cancelled.is_set():
            return "deny"
        return self.brain.approver.ask(request)


class Brain:
    def __init__(self, agent: Agent, speaker: Speaker, approver: Approver, audit: Audit, sessions: Sessions, *,
                 on_reply_finished: Callable[[], None] = lambda: None, progress_after: float = 3.0):
        self.agent = agent
        self.speaker = speaker
        self.approver = approver
        self.audit = audit
        self.sessions = sessions
        self.on_reply_finished = on_reply_finished
        self.progress_after = progress_after
        self._thread: Optional[threading.Thread] = None
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def ask(self, text: str) -> None:
        """Handle a request in the background (cancelling any request still running)."""
        with self._lock:
            if self.busy:
                self.cancel()
                self._thread.join(timeout=10)
            self._cancelled = flag = threading.Event()
            self._thread = threading.Thread(target=self._handle, args=(text, flag), name="argus-brain", daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()
        self.speaker.stop()

    def wait(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _handle(self, text: str, cancelled: threading.Event) -> None:
        events = _VoiceEvents(self, cancelled)
        try:
            turn = self.agent.run(session_id=self.sessions.current(), channel="voice", content=text,
                                  events=events, cancel=cancelled)
        except Exception as exc:  # never let one request take the voice loop down
            log.exception("request failed")
            self.last_error = str(exc)
            events.say(spoken_error(str(exc)))
            turn = None
        events.flush()
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
