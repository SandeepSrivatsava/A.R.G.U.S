"""Decides what to do with each sentence the microphone heard.

Rules (in order):
  * asleep: only "Argus, wake up" does anything; nothing is sent anywhere.
  * while Argus is talking or working: only sentences with its name count (so it can't hear itself);
    "Argus, stop" stops it, anything else with the name interrupts and becomes the new request.
  * otherwise a sentence is for Argus if it contains the name, or comes right after a reply
    (follow-up window), or conversation mode is on.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .wake import Command, NameMatcher, command_of, is_hallucination

IGNORE, COMMAND, ASK, ACK = "ignore", "command", "ask", "ack"


@dataclass(frozen=True)
class Decision:
    kind: str                       # ignore | command | ask | ack (just the name: say "Yes?")
    text: str = ""
    command: Optional[Command] = None
    reason: str = ""
    interrupt: bool = False         # the request arrived while Argus was busy: stop that first
    addressed: bool = False


class Listener:
    def __init__(self, matcher: NameMatcher, *, followup_seconds: float = 8.0,
                 conversation_idle_seconds: float = 120.0, clock: Callable[[], float] = time.monotonic):
        self.matcher = matcher
        self.followup_seconds = followup_seconds
        self.conversation_idle_seconds = conversation_idle_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self.sleeping = False
        self.conversation = False
        self._conversation_until = 0.0
        self._followup_until = 0.0

    # ---- state -------------------------------------------------------------------------------
    def in_followup(self) -> bool:
        return self.clock() < self._followup_until

    def conversation_active(self) -> bool:
        if self.conversation and self.clock() >= self._conversation_until:
            self.conversation = False        # went quiet for too long
        return self.conversation

    def set_conversation(self, on: bool) -> None:
        with self._lock:
            self.conversation = on
            self._conversation_until = self.clock() + self.conversation_idle_seconds if on else 0.0
            if not on:
                self._followup_until = 0.0

    def set_sleeping(self, on: bool) -> None:
        with self._lock:
            self.sleeping = on
            if on:
                self.conversation = False
                self._followup_until = 0.0

    def reply_finished(self) -> None:
        """Argus just finished talking: the next sentence doesn't need the name."""
        with self._lock:
            now = self.clock()
            self._followup_until = now + self.followup_seconds
            if self.conversation:
                self._conversation_until = max(self._conversation_until, now + self.conversation_idle_seconds)

    def mode(self) -> str:
        if self.sleeping:
            return "asleep"
        if self.conversation_active():
            return "conversation"
        if self.in_followup():
            return "follow-up"
        return "listening"

    # ---- the decision ------------------------------------------------------------------------
    def decide(self, text: str, *, busy: bool = False, speaking: bool = False) -> Decision:
        with self._lock:
            text = " ".join(text.split())
            if not text or is_hallucination(text):
                return Decision(IGNORE, reason="noise")
            heard = self.matcher.hear(text)
            open_window = self.in_followup() or self.conversation_active()
            command = command_of(heard.rest) if (heard.addressed or open_window) else None

            if self.sleeping:
                if heard.addressed and command is Command.WAKE:
                    return self._apply(Decision(COMMAND, text, command, addressed=True))
                return Decision(IGNORE, reason="asleep")

            if busy or speaking:
                if not heard.addressed:
                    return Decision(IGNORE, reason="busy")
                if command is not None:
                    return self._apply(Decision(COMMAND, text, command, addressed=True, interrupt=True))
                if not heard.rest:
                    return Decision(COMMAND, text, Command.STOP, addressed=True, interrupt=True)
                return self._accepted(Decision(ASK, text, addressed=True, interrupt=True))

            if not (heard.addressed or open_window):
                return Decision(IGNORE, reason="not addressed")
            if command is not None:
                return self._apply(Decision(COMMAND, text, command, addressed=heard.addressed))
            if not heard.rest:
                # Just the name: answer with a short acknowledgement and open the follow-up window.
                return self._accepted(Decision(ACK, text, addressed=True))
            return self._accepted(Decision(ASK, text, addressed=heard.addressed))

    def _accepted(self, decision: Decision) -> Decision:
        self._followup_until = 0.0
        if self.conversation:
            self._conversation_until = self.clock() + self.conversation_idle_seconds
        return decision

    def _apply(self, decision: Decision) -> Decision:
        command = decision.command
        if command is Command.WAKE:
            self.sleeping = False
        elif command is Command.SLEEP:
            self.sleeping, self.conversation, self._followup_until = True, False, 0.0
        elif command is Command.CONVERSATION_ON:
            self.conversation = True
            self._conversation_until = self.clock() + self.conversation_idle_seconds
        elif command is Command.CONVERSATION_OFF:
            self.conversation, self._followup_until = False, 0.0
        elif command in (Command.STOP, Command.MIC_OFF):
            self._followup_until = 0.0
        return decision
