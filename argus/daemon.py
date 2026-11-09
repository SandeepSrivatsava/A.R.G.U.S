"""The always-listening loop: microphone -> utterances -> (name check) -> Hermes -> voice."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

import numpy as np

from .audit import Audit
from .brain import Brain
from .hearing import SAMPLE_RATE, Segmenter, Transcriber, VoicePrint
from .listener import ACK, ASK, COMMAND, IGNORE, Listener
from .voice import Speaker
from .wake import Command, command_of, looks_like_echo

log = logging.getLogger("argus.daemon")

ACKNOWLEDGEMENTS = ("Yes?", "I'm listening.", "Go ahead.")


class Argus:
    def __init__(self, *, source, segmenter: Segmenter, transcriber: Transcriber, listener: Listener,
                 speaker: Speaker, brain: Brain, audit: Audit, voiceprint: Optional[VoicePrint] = None,
                 voice_lock: bool = False, greeting: str = "", min_utterance_seconds: float = 0.35,
                 barge_in_interval: float = 0.8, on_change: Callable[[], None] = lambda: None):
        self.source = source
        self.segmenter = segmenter
        self.transcriber = transcriber
        self.listener = listener
        self.speaker = speaker
        self.brain = brain
        self.audit = audit
        self.voiceprint = voiceprint
        self.voice_lock = voice_lock
        self.greeting = greeting
        self.min_samples = int(min_utterance_seconds * SAMPLE_RATE)
        self.barge_in_interval = barge_in_interval
        self.on_change = on_change
        self.mic_wanted = True          # the user's choice (menu / voice / argusctl)
        self.locked = False             # Mac screen locked: microphone closed
        self.screen_private = False     # "don't look at my screen" switch, enforced by the hook
        self.ignored = 0                # how many sentences were not for Argus (counted, never stored)
        self.last_heard: Optional[str] = None
        self._stop = threading.Event()
        self._last_barge_check = 0.0
        self._ack = 0
        self.brain.on_reply_finished = self._reply_finished

    # ---- state shown in the menu bar / `argusctl status` -------------------------------------------
    def status(self) -> Dict[str, Any]:
        if self.locked:
            mode = "paused (Mac locked)"
        elif not self.mic_wanted:
            mode = "microphone off"
        elif self.speaker.speaking:
            mode = "speaking"
        elif self.brain.busy:
            mode = "working"
        else:
            mode = self.listener.mode()
        return {"ok": True, "mode": mode, "mic_open": bool(self.source.is_open), "conversation": self.listener.conversation,
                "asleep": self.listener.sleeping, "screen_private": self.screen_private,
                "voice_lock": self.voice_lock and bool(self.voiceprint and self.voiceprint.enrolled),
                "ignored_sentences": self.ignored, "last_error": self.brain.last_error}

    def guard_state(self) -> Dict[str, Any]:
        return {"screen_private": self.screen_private, "mic": self.status()["mode"]}

    # ---- microphone control ------------------------------------------------------------------------
    def _sync_mic(self) -> None:
        want_open = self.mic_wanted and not self.locked
        if want_open and not self.source.is_open:
            self.source.open()
        elif not want_open and self.source.is_open:
            self.source.close()
            self.segmenter.reset()
        self.on_change()

    def set_mic(self, on: bool) -> None:
        self.mic_wanted = on
        if not on:
            self.brain.cancel()
        self.audit.log("microphone", on=on)
        self._sync_mic()

    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        if locked:
            self.brain.cancel()
            self.listener.set_conversation(False)
        self.audit.log("screen_lock", locked=locked)
        self._sync_mic()

    def set_screen_private(self, on: bool) -> None:
        self.screen_private = on
        self.audit.log("screen_private", on=on)
        self.on_change()

    # ---- the main loop -----------------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._sync_mic()
        if self.greeting:
            self.speaker.say(self.greeting)
        while not self._stop.is_set():
            if not self.source.is_open:
                self._stop.wait(0.2)
                continue
            chunk = self.source.read()
            if chunk is None:
                if getattr(self.source, "finished", False):
                    for utterance in self.segmenter.flush():
                        self.handle_utterance(utterance)
                    return
                continue
            for utterance in self.segmenter.feed(chunk):
                self.handle_utterance(utterance)
            self._maybe_barge_in()

    def _maybe_barge_in(self) -> None:
        """While Argus is talking, listen for "Argus, stop" without waiting for the sentence to end."""
        if not self.speaker.speaking or not self.segmenter.speech_active:
            return
        now = time.monotonic()
        if now - self._last_barge_check < self.barge_in_interval:
            return
        self._last_barge_check = now
        audio = self.segmenter.recent_audio()[-int(2.5 * SAMPLE_RATE):]
        text = self.transcriber.transcribe(audio)
        heard = self.listener.matcher.hear(text)
        if heard.addressed and (not heard.rest or command_of(heard.rest) is Command.STOP):
            self.audit.log("barge_in")
            self.brain.cancel()
            self.listener.reply_finished()

    def handle_utterance(self, samples: np.ndarray) -> None:
        if samples.size < self.min_samples or not self.mic_wanted or self.locked:
            return
        text = self.transcriber.transcribe(samples)
        heard = self.listener.matcher.hear(text)
        # Checks that must happen before the listener changes state (follow-up window, sleep, ...):
        if not heard.addressed and looks_like_echo(text, self.speaker.recently_spoken()):
            self.ignored += 1          # Argus hearing its own voice
            return
        if self.voice_lock and self.voiceprint is not None and self.voiceprint.enrolled and (
                heard.addressed or self.listener.in_followup() or self.listener.conversation_active()):
            score = self.voiceprint.score(samples)
            if score < self.voiceprint.threshold:
                self.audit.log("voice_not_recognised", score=round(score, 3))
                self.ignored += 1
                return
        speaking = self.speaker.speaking or self.speaker.quiet_for() < 0.8   # don't answer our own voice
        decision = self.listener.decide(text, busy=self.brain.busy, speaking=speaking)
        if decision.kind == IGNORE:
            self.ignored += 1
            return
        self.last_heard = text
        if decision.kind == COMMAND:
            self.run_command(decision.command, spoken=text)
        elif decision.kind == ACK:
            self.speaker.say(ACKNOWLEDGEMENTS[self._ack % len(ACKNOWLEDGEMENTS)])
            self._ack += 1
            threading.Thread(target=self._ack_done, daemon=True).start()
        elif decision.kind == ASK:
            if decision.interrupt:
                self.brain.cancel()
            self.brain.ask(text)
        self.on_change()

    def _ack_done(self) -> None:
        self.speaker.wait_idle(timeout=10)
        self.listener.reply_finished()

    def _reply_finished(self) -> None:
        self.listener.reply_finished()
        self.on_change()

    def run_command(self, command: Optional[Command], spoken: str = "") -> None:
        self.audit.log("voice_command", command=command.value if command else None)
        if command is Command.STOP:
            self.brain.cancel()
        elif command is Command.SLEEP:
            self.brain.cancel()
            self.listener.set_sleeping(True)
            self.speaker.say("Going quiet. Say my name and wake up when you need me.")
        elif command is Command.WAKE:
            self.listener.set_sleeping(False)
            self.speaker.say("I'm back.")
        elif command is Command.MIC_OFF:
            self.speaker.say("Microphone off. Turn it back on from the menu bar.")
            self.speaker.wait_idle(timeout=10)
            self.set_mic(False)
        elif command is Command.CONVERSATION_ON:
            self.listener.set_conversation(True)
            self.speaker.say("Sure, I'm all ears. Say that's all when you're done.")
        elif command is Command.CONVERSATION_OFF:
            self.listener.set_conversation(False)
            self.speaker.say("Okay.")
        elif command is Command.NEW_SESSION:
            self.brain.sessions.rotate()
            self.speaker.say("Fresh start.")
        self.on_change()

    # ---- control socket handlers -------------------------------------------------------------------
    def handlers(self) -> Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]:
        def say(req):
            self.speaker.say(str(req.get("text") or ""))
            return {"ok": True}

        def ask(req):
            text = str(req.get("text") or "").strip()
            if text:
                self.brain.ask(text)
            return {"ok": bool(text)}

        def mic(req):
            self.set_mic(bool(req.get("on")))
            return self.status()

        def conversation(req):
            self.listener.set_conversation(bool(req.get("on")))
            self.on_change()
            return self.status()

        def sleep(req):
            self.listener.set_sleeping(bool(req.get("on", True)))
            self.on_change()
            return self.status()

        def screen(req):
            self.set_screen_private(not bool(req.get("allow", True)))
            return self.status()

        def stop(req):
            self.brain.cancel()
            return self.status()

        def new(req):
            self.brain.sessions.rotate()
            return {"ok": True}

        return {"status": lambda req: self.status(), "say": say, "ask": ask, "mic": mic,
                "conversation": conversation, "sleep": sleep, "screen": screen, "stop": stop, "new": new}
