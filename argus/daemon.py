"""The always-listening loop: microphone -> utterances (or a double clap) -> name check -> brain -> voice."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .audit import Audit
from .brain import Brain
from .hearing import SAMPLE_RATE, Segmenter, Transcriber, VoicePrint
from .micwatch import friendly
from .listener import ACK, ASK, COMMAND, IGNORE, Listener
from .voice import Speaker
from .approvals import close_word
from .wake import Command, command_of, is_hallucination, looks_like_echo, words

log = logging.getLogger("argus.daemon")

ACKNOWLEDGEMENTS = ("Yes?", "I'm listening.", "Go ahead.")
GUEST_WAIT = 25.0      # how long Argus waits for you to say whether it may answer someone else

_GUEST_NO = re.compile(r"\b(no|nope|don'?t|do not|not now|ignore (them|it|that)|never ?mind|leave it)\b")
_GUEST_YES = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|go ahead|answer|please do|of course|fine|go on)\b")
_GUEST_ALL = re.compile(r"\b(everyone|everybody|anyone|all of them|them all|from now on|for (the next |an |a )?"
                        r"(hour|while)|my friends|my guests|guests)\b")
_TAKE_OVER = re.compile(r"\b(let me (talk|speak|take (it|over))|i'?ll take (it|over|the call)|give me the "
                        r"(call|phone)|hand (it |the call )?over|take over|my turn)\b")
_HANG_UP = re.compile(r"\b(hang up|end (the |this )?call|cut (it|the call)|that'?s enough|bye)\b")
_YES_WORD = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|allow|approve|go ahead|do it)\b")


class Argus:
    def __init__(self, *, source, segmenter: Segmenter, transcriber: Transcriber, listener: Listener,
                 speaker: Speaker, brain: Brain, audit: Audit, voiceprint: Optional[VoicePrint] = None,
                 voice_lock: bool = False, greeting: str = "", min_utterance_seconds: float = 0.35,
                 barge_in_interval: float = 0.25, barge_in: str = "voice", talk_over_threshold: float = 0.38, on_change: Callable[[], None] = lambda: None, clap=None,
                 gate=None, voice_approval_threshold: float = 0.55, guests: str = "ask", guest_minutes: float = 60.0,
                 owner_name: str = "", clock: Callable[[], float] = time.monotonic, calls=None,
                 notify: Callable[[str], None] = lambda text: None):
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
        self.talk_over_threshold = talk_over_threshold
        self.barge_in = barge_in        # "voice": talking over Argus stops it; "name": only "Argus, stop"
        self._speech_since: Optional[float] = None
        self._barged_at = -1e9
        self._barge_hits = 0
        self.on_change = on_change
        self.clap = clap                # ClapDetector: a double clap wakes Argus / starts listening
        self.mic_wanted = True          # the user's choice (menu / voice / argusctl)
        self.locked = False             # Mac screen locked: microphone closed
        self.on_call: List[str] = []    # other apps holding a microphone (a call at the Mac): microphone closed
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
        self.calls = calls              # CallManager: phone calls from your number (calls.py)
        self.notify = notify            # a quiet on-screen note (Argus can't speak while it's on a call)
        self.brain.on_reply_finished = self._reply_finished
        self.extra_status: Callable[[], Dict[str, Any]] = dict   # the app adds its version, voice...
        self.started_at = time.monotonic()
        self.show_requested = False     # another copy was opened: the menu bar shows a note that Argus is here
        self._mic_open_since: Optional[float] = None
        self._last_sound: Optional[float] = None
        self._told_silent = False

    MIC_SILENT_AFTER = 20.0       # an open microphone giving nothing but zeros this long: no permission

    @property
    def voice_enrolled(self) -> bool:
        return bool(self.voiceprint is not None and self.voiceprint.enrolled)

    @property
    def talk_over(self) -> str:
        """How talking over Argus stops it, in words (status, report)."""
        if self.barge_in == "name":
            return "by name only (\"Argus, stop\")"
        if self.barge_in == "words":
            return "any new words (best with headphones)"
        if self.voice_enrolled:
            return "your voice"
        return "by name only until you run: argusctl enroll"

    @property
    def voice_lock_active(self) -> bool:
        return bool(self.voice_lock and self.voiceprint is not None and self.voiceprint.enrolled)

    def voice_approvals_available(self) -> bool:
        """A spoken yes only counts when Argus can tell it's the owner's voice."""
        return self.gate is not None and self.voice_lock_active and self.mic_wanted and not self.closed

    # ---- state shown in the menu bar / `argusctl status` -------------------------------------------
    def status(self) -> Dict[str, Any]:
        if self.locked:
            mode = "paused (Mac locked)"
        elif self.closed:
            mode = f"paused (on a call: {', '.join(friendly(a) for a in self.on_call)})"
        elif not self.mic_wanted:
            mode = "microphone off"
        elif self.speaker.speaking:
            mode = "speaking"
        elif self.brain.busy:
            mode = "working"
        else:
            mode = self.listener.mode()
        now = time.monotonic()
        status = {"ok": True, "mode": mode, "mic_open": bool(self.source.is_open), "conversation": self.listener.conversation,
                  "asleep": self.listener.sleeping, "screen_private": self.screen_private,
                  "voice_lock": self.voice_lock_active, "guests": self._guests_status(),
                  "ignored_sentences": self.ignored, "last_error": self.brain.last_error,
                  "pid": os.getpid(), "up_s": round(now - self.started_at), "locked": self.locked,
                  "on_call": [friendly(a) for a in self.on_call], "voice_enrolled": self.voice_enrolled,
                  "talk_over": self.talk_over, "mic_silent": self.mic_silent(now),
                  "sound_s_ago": None if self._last_sound is None else round(now - self._last_sound)}
        try:
            status.update(self.extra_status())
        except Exception:                 # status must always answer
            log.exception("extra status failed")
        return status

    def mic_silent(self, now: Optional[float] = None) -> bool:
        """The microphone is open but has given only exact zeros since it opened: macOS gives silence, not an
        error, to an app that isn't allowed to use the microphone."""
        now = time.monotonic() if now is None else now
        return bool(self._mic_open_since is not None and self._last_sound is None
                    and now - self._mic_open_since >= self.MIC_SILENT_AFTER)

    def _heard_sound(self, chunk: np.ndarray) -> None:
        now = time.monotonic()
        if self._mic_open_since is None:
            self._mic_open_since = now
        if chunk.size and np.any(chunk):
            self._last_sound = now
        elif not self._told_silent and self.mic_silent(now):
            self._told_silent = True
            log.warning("the microphone gives only silence; is Argus allowed to use it?")
            self.audit.log("mic_silent", seconds=round(now - self._mic_open_since))
            self.on_change()

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
        return self.mic_wanted and not self.closed

    def announce(self, text: str) -> None:
        """Say a reminder or a scheduled result now, or keep it until you're back at the Mac."""
        text = " ".join(str(text).split())
        if not text:
            return
        on_call = self.calls is not None and self.calls.active
        if self.present() and not on_call:
            self.speaker.say(text)
            return
        with self._held_lock:
            self._held = (self._held + [text])[-20:]
        self.audit.log("announcement_held", count=len(self._held))

    def _speak_held(self) -> None:
        if not self.present() or (self.calls is not None and self.calls.active):
            return
        with self._held_lock:
            held, self._held = self._held, []
        if held:
            self.speaker.say("While you were away:")
            for text in held:
                self.speaker.say(text)

    # ---- microphone control ------------------------------------------------------------------------
    def _sync_mic(self) -> None:
        want_open = self.mic_wanted and not self.closed
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

    @property
    def closed(self) -> bool:
        """Not listening: the Mac is locked, or you're on a call at the Mac (not one Argus is making for you)."""
        own_call = self.calls is not None and self.calls.active
        return self.locked or (bool(self.on_call) and not own_call)

    def set_on_call(self, apps: List[str]) -> None:
        """Another app took a microphone (FaceTime, Zoom...): stop talking and listening until it's done."""
        was = self.closed
        self.on_call = list(apps)
        if self.closed and not was:
            self.brain.cancel()
            self.listener.set_conversation(False)
            names = ", ".join(friendly(a) for a in apps)
            # say why it went quiet: an app can hold the microphone with no call going on (WhatsApp does)
            self.notify(f"Argus is paused while {names} uses the microphone. Not a call? "
                        f"argusctl ignore-app {friendly(apps[0])}")
        self.audit.log("on_call", apps=[friendly(a) for a in apps] or None)
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
                self._mic_open_since = None         # closed (locked, a call, switched off): start counting again
                self._stop.wait(0.2)
                continue
            chunk = self.source.read()
            if chunk is not None:
                self._heard_sound(chunk)
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
        if self.speaker.speaking or self.brain.busy or not self.mic_wanted or self.closed:
            return
        self.audit.log("clap")
        if self.listener.sleeping:
            self.run_command(Command.WAKE)
        else:
            self.speaker.say(ACKNOWLEDGEMENTS[self._ack % len(ACKNOWLEDGEMENTS)])
            self._ack += 1
            threading.Thread(target=self._ack_done, daemon=True).start()
        self.on_change()

    BARGE_MIN_SECONDS = 0.3       # this much of your voice before Argus stops (a cough or "mm" doesn't count)
    BARGE_SURE = 0.55             # a match this strong stops Argus on the first check, without waiting for a second
    BARGE_WINDOW = 12.0           # after stopping, what you say next needs no name

    def _maybe_barge_in(self) -> None:
        """While Argus is talking, you can talk over it, like with a person: it stops at once and listens.

        Its own voice from the speakers reaches the microphone too, and on a laptop it is loud and often
        misheard, so comparing words isn't enough to tell you apart (that made Argus stop itself mid-sentence
        and then answer its own words). With an enrolled voice (argusctl enroll) it stops when the voice is
        yours; without one, when you say its name: "Argus, stop" or "Argus, <something new>". barge_in = "words"
        brings back stopping for any new words, for headphones."""
        if not self.speaker.speaking or not self.segmenter.speech_active:
            self._speech_since = None
            self._barge_hits = 0
            return
        now = time.monotonic()
        if self._speech_since is None:
            self._speech_since = now
        talking = now - self._speech_since
        by_voice = self.barge_in == "voice" and self.voice_enrolled
        if talking < (self.BARGE_MIN_SECONDS if by_voice or self.barge_in == "words" else 0.0):
            return
        if now - self._last_barge_check < self.barge_in_interval:
            return
        self._last_barge_check = now
        audio = self.segmenter.recent_audio()[-int(min(talking + 0.3, 2.5) * SAMPLE_RATE):]
        if by_voice:
            score = self.voiceprint.score(audio)
            if score >= self.talk_over_threshold:
                # clearly you: stop now. A borderline match has to come twice in a row (one lucky match isn't enough)
                if score >= max(self.BARGE_SURE, self.talk_over_threshold) or self._barge_hits >= 1:
                    return self._barge(now, "your voice", score=round(score, 3))
                self._barge_hits += 1
            else:
                self._barge_hits = 0                # Argus's own voice (or someone else): keep talking
            return
        text = self.transcriber.transcribe(audio)
        heard = self.listener.matcher.hear(text)
        if heard.addressed and (not heard.rest or command_of(heard.rest) is Command.STOP):
            return self._barge(now, "Argus, stop", stopped=True)
        recent = self.speaker.recently_spoken()
        if heard.addressed and len(words(heard.rest)) >= 2 and not looks_like_echo(text, recent, threshold=0.5):
            return self._barge(now, "its name")     # "Argus, what about tomorrow?": stop and listen to the rest
        if (self.barge_in == "words" and len(words(text)) >= 3 and not is_hallucination(text)
                and not looks_like_echo(text, recent, threshold=0.34)):
            if self._barge_hits >= 1:               # clearly different words, twice in a row
                return self._barge(now, "new words", heard_words=len(words(text)))
            self._barge_hits += 1
        else:
            self._barge_hits = 0

    def _barge(self, now: float, why: str, stopped: bool = False, **details) -> None:
        self._barge_hits = 0
        self.audit.log("barge_in", why=why, **details)
        self.brain.cancel()                          # stops the voice and the rest of the answer at once
        self.listener.reply_finished()               # what you're saying now doesn't need the name
        self._barged_at = -1e9 if stopped else now
        self.on_change()

    def _barged_recently(self) -> bool:
        return time.monotonic() - self._barged_at < self.BARGE_WINDOW

    def _yours_after_barge(self, samples: np.ndarray, text: str) -> bool:
        """Right after Argus stopped for you, what you say next needs no name, but the echo of the sentence it
        was saying must not count as you: a looser echo check, and your voiceprint when there is one."""
        if looks_like_echo(text, self.speaker.recently_spoken(), threshold=0.34):
            return False
        if self.voice_enrolled and not self.voice_lock_active:     # (with voice lock on it was checked above)
            return self.voiceprint.score(samples) >= self.voiceprint.threshold
        return True

    def _fix_names(self, text: str) -> str:
        """Put right names the speech model garbled ("jacket in the box" -> "Jack in the Box"), see names.py."""
        known = getattr(self, "names", None)
        if known is None or not text:
            return text
        try:
            from .names import correct
            fixed, changes = correct(text, known.all())
        except Exception:
            log.exception("name correction failed")
            return text
        if changes:
            self.audit.log("names_fixed", changes=[f"{a} -> {b}" for a, b in changes][:5])
        return fixed

    def handle_utterance(self, samples: np.ndarray) -> None:
        if samples.size < self.min_samples or not self.mic_wanted or self.closed:
            return
        began = time.monotonic()
        text = self.transcriber.transcribe(samples)
        text = self._fix_names(text)
        self._heard = {"speech_to_text_s": round(time.monotonic() - began, 2),
                       "spoken_s": round(samples.size / SAMPLE_RATE, 1)}
        heard = self.listener.matcher.hear(text)
        if self.gate is not None and self.gate.pending:
            # Waiting for "yes, <code word>": no name needed. The answer repeats words from Argus's own
            # question, so the echo check looks at the rest of what was said.
            if looks_like_echo(self._without_answer_words(text), self.speaker.recently_spoken()):
                self.ignored += 1
            else:
                self._approval_answer(samples, text)
            return
        # Checks that must happen before the listener changes state (follow-up window, sleep, ...):
        if not heard.addressed and looks_like_echo(text, self.speaker.recently_spoken()):
            self.ignored += 1          # Argus hearing its own voice
            return
        if self.calls is not None and self.calls.active:
            self._during_call(samples, heard)
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
        barged = self._barged_recently()
        if barged and not heard.addressed and not self._yours_after_barge(samples, text):
            self.ignored += 1             # the tail of Argus's own sentence, half-heard: not a question
            return
        speaking = (self.speaker.speaking or self.speaker.quiet_for() < 0.8) and not barged  # not our own voice
        decision = self.listener.decide(text, busy=self.brain.busy and not barged, speaking=speaking)
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
            if barged:
                self._barged_at = -1e9
            self.brain.ask(text, heard=getattr(self, "_heard", None), interrupted=barged)
        self.on_change()

    # ---- spoken approvals ----------------------------------------------------------------------------
    def _without_answer_words(self, text: str) -> str:
        code = self.gate.code
        return " ".join(w for w in re.findall(r"[a-z']+", text.lower())
                        if not _YES_WORD.fullmatch(w) and not (code and close_word(w, code)))

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

    # ---- on a phone call ---------------------------------------------------------------------------
    def _during_call(self, samples: np.ndarray, heard) -> None:
        """While Argus is on a call, only the owner's "hang up" / "let me talk" count (the speakers are
        playing the other side, so everything else is ignored)."""
        if not heard.addressed:
            return
        if self.voice_lock_active and self.voiceprint.score(samples) < self.voiceprint.threshold:
            return
        rest = heard.rest
        if _TAKE_OVER.search(rest):
            self.audit.log("call_take_over")
            self.calls.take_over()
            self.notify("Your microphone is on the call now.")
        elif _HANG_UP.search(rest) or command_of(rest) is Command.STOP:
            self.audit.log("call_hang_up_asked")
            self.calls.hang_up()
            self.notify("Hanging up.")
        else:
            self.notify("I'm on a call. Say \"Argus, hang up\" or \"Argus, let me talk\".")

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
        if self._held:
            self._speak_held()          # reminders that came in during a phone call
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

        def show(req):                   # Argus.app opened again while running: say "I'm here" on screen
            self.show_requested = True
            self.on_change()
            return {"ok": True}

        return {"status": lambda req: self.status(), "say": say, "ask": ask, "mic": mic,
                "conversation": conversation, "sleep": sleep, "screen": screen, "stop": stop, "new": new,
                "show": show}
