"""The always-listening loop: microphone -> utterances (or a double clap) -> name check -> brain -> voice."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .audit import Audit
from .brain import Brain
from .hearing import SAMPLE_RATE, Segmenter, Transcriber, VoicePrint
from .listener import ACK, ASK, COMMAND, IGNORE, Listener
from .voice import Speaker
from .wake import Command, command_of, looks_like_echo

log = logging.getLogger("argus.daemon")

ACKNOWLEDGEMENTS = ("Yes?", "I'm listening.", "Go ahead.")
GUEST_WAIT = 25.0      # how long Argus waits for you to say whether it may answer someone else

_GUEST_NO = re.compile(r"\b(no|nope|don'?t|do not|not now|ignore (them|it|that)|never ?mind|leave it)\b")
_GUEST_YES = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|go ahead|answer|please do|of course|fine|go on)\b")
_GUEST_ALL = re.compile(r"\b(everyone|everybody|anyone|all of them|them all|from now on|for (the next |an |a )?"
                        r"(hour|while)|my friends|my guests|guests)\b")
_YES_WORD = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|allow|approve|go ahead|do it)\b")


class Argus:
    def __init__(self, *, source, segmenter: Segmenter, transcriber: Transcriber, listener: Listener,
                 speaker: Speaker, brain: Brain, audit: Audit, voiceprint: Optional[VoicePrint] = None,
                 voice_lock: bool = False, greeting: str = "", min_utterance_seconds: float = 0.35,
                 barge_in_interval: float = 0.8, on_change: Callable[[], None] = lambda: None, clap=None,
                 gate=None, voice_approval_threshold: float = 0.55, guests: str = "ask", guest_minutes: float = 60.0,
                 owner_name: str = "", clock: Callable[[], float] = time.monotonic):
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
        self.clap = clap                # ClapDetector: a double clap wakes Argus / starts listening
        self.mic_wanted = True          # the user's choice (menu / voice / argusctl)
        self.locked = False             # Mac screen locked: microphone closed
        self.screen_private = False     # "don't look at my screen" switch, enforced by the hook
        self.ignored = 0                # how many sentences were not for Argus (counted, never stored)
        self.last_heard: Optional[str] = None
        self._stop = threading.Event()
        self._last_barge_check = 0.0
        self._ack = 0
        self._held: List[str] = []      # reminders that came while you were away, spoken when you're back
        self._held_lock = threading.Lock()
        self.gate = gate                # VoiceGate: a spoken approval Argus is waiting for (approvals.py)
        self.voice_approval_threshold = voice_approval_threshold
        self.guests = guests            # "ask": someone else talks to Argus -> ask the owner first; "never"
        self.guest_minutes = guest_minutes
        self.guests_until = 0.0         # "talk to everyone" lasts until then
        self.owner_name = owner_name
        self.clock = clock
        self._guest_question: Optional[tuple] = None   # (what they asked, when Argus asked the owner)
        self.brain.on_reply_finished = self._reply_finished

    @property
    def voice_lock_active(self) -> bool:
        return bool(self.voice_lock and self.voiceprint is not None and self.voiceprint.enrolled)

    def voice_approvals_available(self) -> bool:
        """A spoken yes only counts when Argus can tell it's the owner's voice."""
        return self.gate is not None and self.voice_lock_active and self.mic_wanted and not self.locked

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
                "voice_lock": self.voice_lock_active, "guests": self._guests_status(),
                "ignored_sentences": self.ignored, "last_error": self.brain.last_error}

    def _guests_status(self) -> str:
        if not self.voice_lock_active:
            return "everyone (voice lock is off)"
        if self.clock() < self.guests_until:
            return f"everyone for {max(1, int((self.guests_until - self.clock()) / 60))} more min"
        return "only you" + (" (asks you about others)" if self.guests == "ask" else "")

    def guard_state(self) -> Dict[str, Any]:
        return {"screen_private": self.screen_private, "mic": self.status()["mode"]}

    # ---- reminders and scheduled results -------------------------------------------------------------
    def present(self) -> bool:
        """Is someone at the Mac to hear it? Not while it's locked or the microphone is switched off."""
        return self.mic_wanted and not self.locked

    def announce(self, text: str) -> None:
        """Say a reminder or a scheduled result now, or keep it until you're back at the Mac."""
        text = " ".join(str(text).split())
        if not text:
            return
        if self.present():
            self.speaker.say(text)
            return
        with self._held_lock:
            self._held = (self._held + [text])[-20:]
        self.audit.log("announcement_held", count=len(self._held))

    def _speak_held(self) -> None:
        if not self.present():
            return
        with self._held_lock:
            held, self._held = self._held, []
        if held:
            self.speaker.say("While you were away:")
            for text in held:
                self.speaker.say(text)

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
        self._speak_held()

    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        if locked:
            self.brain.cancel()
            self.listener.set_conversation(False)
        self.audit.log("screen_lock", locked=locked)
        self._sync_mic()
        self._speak_held()

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
            if self.clap is not None and self.clap.feed(chunk):
                self.on_clap()
            for utterance in self.segmenter.feed(chunk):
                self.handle_utterance(utterance)
            if self.segmenter.speech_active and not self.listener.sleeping:
                self.brain.warm()       # someone is talking: have a connection to Claude ready (sends nothing)
            self._maybe_barge_in()

    def on_clap(self) -> None:
        """Clap clap: wake up from sleep, or listen for the next sentence without needing the name."""
        if self.speaker.speaking or self.brain.busy or not self.mic_wanted or self.locked:
            return
        self.audit.log("clap")
        if self.listener.sleeping:
            self.run_command(Command.WAKE)
        else:
            self.speaker.say(ACKNOWLEDGEMENTS[self._ack % len(ACKNOWLEDGEMENTS)])
            self._ack += 1
            threading.Thread(target=self._ack_done, daemon=True).start()
        self.on_change()

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
        if self.gate is not None and self.gate.pending:
            self._approval_answer(samples, text)      # waiting for "yes, <code word>": no name needed
            return
        if self.voice_lock_active and (heard.addressed or self.listener.in_followup()
                                       or self.listener.conversation_active() or self._guest_pending()):
            score = self.voiceprint.score(samples)
            if score < self.voiceprint.threshold:
                self.audit.log("voice_not_recognised", score=round(score, 3))
                self.ignored += 1
                self._someone_else(heard, text)
                return
            if self._guest_pending() and self._guest_answer(text):
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

    # ---- spoken approvals ----------------------------------------------------------------------------
    def _approval_answer(self, samples: np.ndarray, text: str) -> None:
        if not self.voice_lock_active:
            return
        score = self.voiceprint.score(samples)
        if score < self.voice_approval_threshold:
            self.audit.log("approval_voice_rejected", score=round(score, 3))
            self.ignored += 1
            return
        if self.gate.offer(text) is None and _YES_WORD.search(text.lower()) and self.gate.code:
            self.speaker.say(f"Please say yes and the word {self.gate.code}.")

    # ---- other people ------------------------------------------------------------------------------
    def _guest_pending(self) -> bool:
        if self._guest_question is None:
            return False
        if self.clock() - self._guest_question[1] > GUEST_WAIT:
            self._guest_question = None
            return False
        return True

    def _someone_else(self, heard, text: str) -> None:
        """Not the owner's voice. Answer only if the owner allowed it; otherwise ask the owner first."""
        if self.guests == "never":
            return
        now = self.clock()
        allowed = now < self.guests_until
        if not (heard.addressed or (allowed and self.listener.in_followup())):
            return
        question = (heard.rest if heard.addressed else text).strip()
        if not question:
            return
        if allowed:
            self._answer_guest(question)
            return
        if self._guest_pending():
            return                     # already asked; waiting for the owner
        self._guest_question = (question, now)
        self.audit.log("guest_waiting")
        who = f"{self.owner_name}, someone" if self.owner_name else "Someone"
        self.speaker.say(f"{who} else is asking me something. Should I answer them?")

    def _guest_answer(self, text: str) -> bool:
        """The owner's reply to "should I answer them?". False if it wasn't an answer."""
        said = " ".join(re.findall(r"[a-z']+", text.lower()))
        question = self._guest_question[0]
        if _GUEST_NO.search(said):
            self._guest_question = None
            self.audit.log("guest_declined")
            self.speaker.say("Okay, I won't.")
            return True
        if not _GUEST_YES.search(said):
            return False
        self._guest_question = None
        if _GUEST_ALL.search(said):
            self.guests_until = self.clock() + self.guest_minutes * 60
            self.audit.log("guests_allowed", minutes=self.guest_minutes)
        self._answer_guest(question)
        return True

    def _answer_guest(self, question: str) -> None:
        if self.brain.busy:
            self.speaker.say("I'm in the middle of something. Please ask me again in a moment.")
            return
        self.audit.log("guest_question")
        self.brain.ask(question, guest=True)

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
        elif command is Command.GUESTS_ON:
            if not self.voice_lock_active:
                self.speaker.say("I can't tell voices apart yet, so I already answer everyone. Run argusctl enroll "
                                 "to teach me your voice.")
            else:
                self.guests_until = self.clock() + self.guest_minutes * 60
                self.audit.log("guests_allowed", minutes=self.guest_minutes)
                minutes = int(self.guest_minutes)
                span = "the next hour" if minutes == 60 else f"the next {minutes} minutes"
                self.speaker.say(f"Okay, I'll answer everyone for {span}.")
        elif command is Command.GUESTS_OFF:
            self.guests_until = 0.0
            self._guest_question = None
            self.audit.log("guests_stopped")
            self.speaker.say("Okay, just you.")
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
