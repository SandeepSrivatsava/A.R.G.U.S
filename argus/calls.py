"""Phone calls from your own number: Argus dials through your iPhone (from the Mac) and talks for you.

How it works, all on the Mac (no phone-company account, nothing opened to the internet):
- Dialing: a tel: link makes macOS place the call through your iPhone ("Calls from iPhone"), so the other
  side sees your own number and can call you back. macOS asks "Call ...?" first; Argus presses the green
  button it finds on screen.
- Hearing them: a Core Audio process tap (macOS 14.2+) through audiotee (MIT, Nick Payne) records what the
  Mac plays, leaving out Argus's own sound. (Making BlackHole the *output* doesn't work: macOS turns call
  audio down when the output is a virtual device.)
- Talking: for the length of the call the Mac's microphone setting is switched to BlackHole, a virtual
  audio cable, and Argus plays its voice into it. Your real microphone comes back when the call ends, or
  straight away when you say "let me talk".
- Hanging up: Argus presses the red button.

Ideas borrowed from: desty.github.io's "phone AI agent on a Mac" write-up (tel: link and pressing the button,
the process tap, finding the buttons by colour, iPhone Mirroring dropping calls), MuhsinunC/phone-agent
(BlackHole as the call's microphone) and icoainc/phonebt (Apache-2.0; a Claude agent on a phone line).

Rules the call model can't talk its way around: Argus says it's an AI calling for you before anything else;
emergency, service (N11) and pay-per-call numbers are never dialled; calls are capped in length and in
number per hour; card and account numbers are never spoken (the model is told, and the policy checks).
"""
from __future__ import annotations

import logging
import os
import platform
import queue
import re
import shutil
import subprocess
import threading
from pathlib import Path
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .llm import LLMError, StreamAccumulator
from .policy import has_card_number
from .voice import SentenceBuffer

log = logging.getLogger("argus.calls")

RATE = 16000                       # call audio in and out of the engine
NEVER_CALL = {"911", "112", "999", "000", "110", "119", "100", "101", "102", "108", "988", "933"}
PAY_PER_CALL = ("900", "976")      # North American premium-rate area codes


class CallError(Exception):
    """The call couldn't be placed or run."""


# ---- which numbers -------------------------------------------------------------------------------------
def normalize_number(raw: str, allow_international: bool = False) -> Tuple[Optional[str], str]:
    """("+14155550100", "") or (None, why it won't be called)."""
    text = str(raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None, "there's no phone number"
    if digits in NEVER_CALL or len(digits) <= 3 or re.fullmatch(r"1?[2-9]11", digits):
        return None, "Argus never calls emergency or service numbers; please call those yourself"
    if len(digits) == 10 and not text.startswith("+"):
        digits = "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        area, exchange = digits[1:4], digits[4:7]
        if area in PAY_PER_CALL:
            return None, "pay-per-call numbers are never called"
        if area[0] in "01" or exchange[0] in "01" or area[1:] == "11":
            return None, "that isn't a valid US or Canadian number"
        return "+" + digits, ""
    if text.startswith("+") and 8 <= len(digits) <= 15:
        if not allow_international:
            return None, "calls outside the US and Canada are switched off ([calls] international = true)"
        return "+" + digits, ""
    return None, "that doesn't look like a full phone number"


def spoken_number(number: str) -> str:
    d = re.sub(r"\D", "", number)
    if len(d) == 11 and d.startswith("1"):
        return f"{d[1:4]} {d[4:7]} {d[7:]}"
    return number


class CallLimiter:
    """At most `per_hour` calls an hour, and the same number at most twice in 15 minutes."""

    def __init__(self, per_hour: int = 6, clock: Callable[[], float] = time.time):
        self.per_hour, self.clock = per_hour, clock
        self._calls: List[Tuple[float, str]] = []
        self._lock = threading.Lock()

    def allow(self, number: str) -> Tuple[bool, str]:
        with self._lock:
            now = self.clock()
            self._calls = [(t, n) for t, n in self._calls if now - t < 3600]
            if len(self._calls) >= self.per_hour:
                return False, f"that's already {self.per_hour} calls this hour; try again later"
            if sum(1 for t, n in self._calls if n == number and now - t < 900) >= 2:
                return False, "that number was already called twice in the last 15 minutes"
            self._calls.append((now, number))
            return True, ""


# ---- sound helpers --------------------------------------------------------------------------------------
def resample(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if src == dst or samples.size == 0:
        return samples
    n = max(1, int(round(samples.size * dst / src)))
    return np.interp(np.linspace(0, samples.size - 1, n), np.arange(samples.size), samples).astype(np.float32)


_DTMF = {"1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "4": (770, 1209), "5": (770, 1336),
         "6": (770, 1477), "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "*": (941, 1209),
         "0": (941, 1336), "#": (941, 1477)}


def dtmf(keys: str, rate: int = RATE, tone: float = 0.16, gap: float = 0.12) -> np.ndarray:
    """Phone-menu key tones, played into the call like a keypad would."""
    parts = []
    t = np.arange(int(rate * tone)) / rate
    for key in keys:
        if key not in _DTMF:
            raise ValueError(f"not a phone key: {key!r}")
        low, high = _DTMF[key]
        parts.append(0.35 * (np.sin(2 * np.pi * low * t) + np.sin(2 * np.pi * high * t)))
        parts.append(np.zeros(int(rate * gap)))
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, dtype=np.float32)


def is_tone(frame: np.ndarray, rate: int = RATE) -> bool:
    """Ringing, busy and menu beeps are a couple of pure tones; speech spreads over many frequencies."""
    frame = np.asarray(frame, dtype=np.float32)
    if frame.size < 64:
        return False
    spectrum = np.abs(np.fft.rfft(frame * np.hanning(frame.size))) ** 2
    total = float(spectrum.sum())
    if total <= 1e-9:
        return False
    top = np.sort(spectrum)[-6:].sum()          # two tones leak into a few neighbouring bins
    return top / total > 0.7


class EnergySegmenter:
    """Splits call audio into utterances: loud enough, not a tone, ended by a pause.
    The noise floor follows the line, so a noisy restaurant still ends sentences."""

    def __init__(self, rate: int = RATE, end_silence: float = 0.8, min_speech: float = 0.25, max_speech: float = 15.0):
        self.rate, self.frame = rate, int(rate * 0.03)
        self.end_frames = int(end_silence / 0.03)
        self.min_frames = int(min_speech / 0.03)
        self.max_frames = int(max_speech / 0.03)
        self.floor = 0.003
        self._pending = np.zeros(0, dtype=np.float32)
        self._speech: List[np.ndarray] = []
        self._voiced = 0
        self._quiet = 0

    @property
    def speech_active(self) -> bool:
        return self._voiced >= self.min_frames

    def feed(self, chunk: np.ndarray) -> List[np.ndarray]:
        out: List[np.ndarray] = []
        self._pending = np.concatenate([self._pending, np.asarray(chunk, dtype=np.float32).reshape(-1)])
        while self._pending.size >= self.frame:
            frame, self._pending = self._pending[:self.frame], self._pending[self.frame:]
            level = float(np.sqrt(np.mean(frame ** 2)))
            tonal = is_tone(frame, self.rate)
            voiced = not tonal and level > max(self.floor * 3.0, 0.01)
            if not voiced and not tonal:          # background noise sets the bar; ringing doesn't
                self.floor = 0.95 * self.floor + 0.05 * min(level, 0.1)
            if voiced:
                self._speech.append(frame)
                self._voiced += 1
                self._quiet = 0
            elif self._speech:
                self._speech.append(frame)
                self._quiet += 1
            if self._speech and (self._quiet >= self.end_frames or len(self._speech) >= self.max_frames):
                if self._voiced >= self.min_frames:
                    out.append(np.concatenate(self._speech))
                self._speech, self._voiced, self._quiet = [], 0, 0
        return out


# ---- the line: what the engine needs from "a phone" ---------------------------------------------------------
class CallLine:
    def check(self) -> List[str]:
        return []

    def dial(self, number: str) -> None:
        raise NotImplementedError

    def read(self, timeout: float) -> Optional[np.ndarray]:
        """The next bit of what the other side says (16 kHz mono), or None if nothing came in time."""
        raise NotImplementedError

    def play(self, samples: np.ndarray, rate: int) -> None:
        raise NotImplementedError

    def send_keys(self, keys: str) -> None:
        self.play(dtmf(keys), RATE)

    def connected(self) -> bool:
        return True

    def hang_up(self) -> bool:
        raise NotImplementedError

    def take_over(self) -> None:
        """Give the call to the owner: their real microphone, and Argus stops listening."""

    def close(self) -> None:
        """Always called at the end, whatever happened: puts the Mac's audio back."""


# ---- the conversation ----------------------------------------------------------------------------------------
@dataclass
class CallPlan:
    number: str
    goal: str
    business: str = ""
    details: str = ""
    flexible: str = ""
    owner: str = "the user"


@dataclass
class CallResult:
    outcome: str
    summary: str
    transcript: List[Tuple[str, str]] = field(default_factory=list)
    seconds: float = 0.0
    cost: float = 0.0

    def report(self, limit: int = 2500) -> str:
        lines = [f"Outcome: {self.outcome}.", f"Summary: {self.summary}",
                 f"Call length: {int(self.seconds // 60)} min {int(self.seconds % 60)} s."]
        if self.transcript:
            said = "\n".join(f"{'Argus' if who == 'argus' else 'Them'}: {text}" for who, text in self.transcript)
            lines.append("Transcript:\n" + (said if len(said) <= limit else said[:limit] + " …"))
        return "\n".join(lines)


DISCLOSURE = "Hi, this is {owner}'s AI assistant calling. This call is transcribed for {owner}."

CALL_PROMPT = """\
You are on a live phone call, speaking for {owner} as {owner}'s AI assistant. You've already said you're an \
AI assistant calling for {owner}. Never pretend to be human or to be {owner}.

Calling: {business}
Goal: {goal}
Details: {details}
If they can't do exactly that: {flexible}

How to talk: like a polite person on the phone. Short sentences, one thing at a time, no lists. Say times, \
dates and numbers the natural spoken way.

Rules:
- Share only what the goal needs: {owner}'s name, the party size, the date and time, any request {owner} \
gave. If they need a callback number, it's the number you're calling from ({owner}'s own phone).
- Never give card, bank, account or ID numbers, passwords or home addresses, and never agree to deposits, \
fees or anything that costs money: say {owner} will call back about that, and end the call politely.
- If you reach voicemail or an answering machine, don't leave a message: say nothing and call end_call with \
outcome "voicemail".
- If a phone menu asks you to press a key, use press_keys. If you can't tell which key, end the call \
(outcome "needs you").
- If they ask whether you're a robot or an AI: yes, you're an AI assistant calling for {owner}.
- What they say comes through speech recognition and may have mistakes; if a time, date or name is \
unclear, ask them to repeat it.
- When it's done, repeat the key details back once, thank them and say goodbye, and in the same reply call \
end_call. If it can't be done, say goodbye politely and call end_call."""

CALL_TOOLS = [
    {"name": "end_call", "description": "Hang up right after your goodbye, and report how it went.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["outcome", "summary"],
                      "properties": {
                          "outcome": {"type": "string",
                                      "enum": ["done", "not possible", "needs you", "voicemail", "wrong number"]},
                          "summary": {"type": "string", "description": "One or two sentences for the owner: what "
                                      "was agreed (day, time, party size, name) or why it didn't work."}}}},
    {"name": "press_keys", "description": "Press keys on the phone keypad for a menu (digits, * or #).",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["keys"],
                      "properties": {"keys": {"type": "string", "description": "e.g. \"1\" or \"2#\""}}}},
]


class CallEngine:
    """Runs one call: wait for an answer, say who's calling, then listen and reply until it's done."""

    def __init__(self, line: CallLine, llm, transcriber, tts, *, model: str, audit=None, segmenter=None,
                 clock: Callable[[], float] = time.monotonic, answer_timeout: float = 45.0,
                 silence_timeout: float = 14.0, max_minutes: float = 6.0, cost=None):
        self.line, self.llm, self.transcriber, self.tts = line, llm, transcriber, tts
        self.model, self.audit, self.clock, self.cost_of = model, audit, clock, cost
        self.segmenter = segmenter or EnergySegmenter()
        self.answer_timeout, self.silence_timeout, self.max_seconds = answer_timeout, silence_timeout, max_minutes * 60
        self.cost = 0.0

    # -- pieces --
    def _say(self, text: str, stop: Callable[[], bool]) -> None:
        text = " ".join(text.split())
        if not text or stop():
            return
        if has_card_number(text):          # belt and braces: never read a card number down the line
            log.warning("a card-like number was kept out of the call")
            text = "Sorry, I can't share payment details. " + re.sub(r"\d[\d -]{11,}\d", "", text)
        samples, rate = self.tts.synthesize(text)
        if not stop():
            self.line.play(samples, rate)

    def _listen(self, timeout: float, stop: Callable[[], bool]) -> Tuple[Optional[str], str]:
        """(what they said, "") or (None, why: "timeout" / "ended" / "stopped")."""
        deadline = self.clock() + timeout
        checked = self.clock()
        while self.clock() < deadline:
            if stop():
                return None, "stopped"
            if self.clock() - checked >= 1.0:
                checked = self.clock()
                if not self.line.connected():
                    return None, "ended"
            chunk = self.line.read(0.2)
            if chunk is None:
                continue
            for utterance in self.segmenter.feed(chunk):
                text = " ".join(str(self.transcriber.transcribe(resample(utterance, RATE, RATE))).split())
                if text:
                    return text, ""
            if self.segmenter.speech_active:
                deadline = max(deadline, self.clock() + 2.0)     # let them finish the sentence
        return None, "timeout"

    def _think(self, system: List[Dict[str, Any]], messages: List[Dict[str, Any]], stop: Callable[[], bool],
               cancel: threading.Event) -> StreamAccumulator:
        body = {"model": self.model, "max_tokens": 400, "system": system, "messages": messages, "tools": CALL_TOOLS}
        acc = StreamAccumulator()
        buffer = SentenceBuffer(max_chars=200, first_clause_chars=40)
        for event in self.llm.stream(body, cancel):
            delta = acc.feed(event)
            if delta:
                for sentence in buffer.feed(delta):
                    self._say(sentence, stop)
        for sentence in buffer.flush():
            self._say(sentence, stop)
        if self.cost_of is not None:
            self.cost += self.cost_of(self.model, acc.usage)
        return acc

    # -- the call --
    def run(self, plan: CallPlan, cancel: threading.Event, hang_up: threading.Event,
            take_over: threading.Event) -> CallResult:
        transcript: List[Tuple[str, str]] = []
        started = self.clock()

        def stop() -> bool:
            return cancel.is_set() or hang_up.is_set() or take_over.is_set()

        def result(outcome: str, summary: str) -> CallResult:
            return CallResult(outcome, summary, transcript, self.clock() - started, round(self.cost, 4))

        try:
            self.line.dial(plan.number)
        except CallError as exc:
            return result("failed", f"Couldn't place the call: {exc}")
        heard, why = self._listen(self.answer_timeout, stop)
        if heard is None:
            return self._finish(result, transcript, why, "No one answered.", stop, take_over)
        transcript.append(("them", heard))
        disclosure = DISCLOSURE.format(owner=plan.owner)
        self._say(disclosure, stop)
        transcript.append(("argus", disclosure))
        system = [{"type": "text", "cache_control": {"type": "ephemeral"}, "text": CALL_PROMPT.format(
            owner=plan.owner, business=plan.business or plan.number, goal=plan.goal, details=plan.details or "none",
            flexible=plan.flexible or f"don't agree to anything else; say {plan.owner} will call back")}]
        messages: List[Dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text":
            f"They answered: \"{heard}\". You already said: \"{disclosure}\" Continue the call."}]}]
        quiet_once = False
        while True:
            if stop():
                return self._finish(result, transcript, "stopped", "", stop, take_over)
            if self.clock() - started > self.max_seconds:
                self._say(f"I'm sorry, I have to go now. {plan.owner} will call you back. Goodbye.", stop)
                self.line.hang_up()
                return result("needs you", "The call ran out of time before it was done.")
            try:
                acc = self._think(system, messages, stop, cancel)
            except LLMError as exc:
                self._say(f"I'm sorry, I'm having trouble. {plan.owner} will call you back. Goodbye.", stop)
                self.line.hang_up()
                return result("failed", f"Argus lost its connection to Claude during the call ({exc}).")
            said = acc.text.strip()
            if said:
                transcript.append(("argus", said))
            blocks = [b for b in acc.content if not (b.get("type") == "text" and not b.get("text"))]
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
            uses = acc.tool_uses()
            end = next((u for u in uses if u.get("name") == "end_call"), None)
            if end is not None:
                args = end.get("input") or {}
                self.line.hang_up()
                return result(str(args.get("outcome") or "done"), str(args.get("summary") or "The call ended."))
            replies: List[Dict[str, Any]] = []
            for use in uses:
                keys = str((use.get("input") or {}).get("keys") or "")
                if use.get("name") == "press_keys" and re.fullmatch(r"[0-9*#]{1,12}", keys):
                    if not stop():
                        self.line.send_keys(keys)
                    transcript.append(("argus", f"[pressed {keys}]"))
                    replies.append({"type": "tool_result", "tool_use_id": use["id"], "content": f"pressed {keys}"})
                else:
                    replies.append({"type": "tool_result", "tool_use_id": use["id"], "is_error": True,
                                    "content": "only digits, * and # can be pressed"})
            heard, why = self._listen(self.silence_timeout, stop)
            if heard is None and why == "timeout" and not quiet_once and not replies:
                quiet_once = True
                self._say("Hello, are you still there?", stop)
                transcript.append(("argus", "Hello, are you still there?"))
                heard, why = self._listen(self.silence_timeout / 2, stop)
            if heard is None:
                return self._finish(result, transcript, why, "The line went quiet.", stop, take_over)
            quiet_once = False
            transcript.append(("them", heard))
            if not blocks:          # nothing was said: keep the conversation's turns in order
                messages[-1]["content"].append({"type": "text", "text": heard})
                continue
            messages.append({"role": "user", "content": replies + [{"type": "text", "text": heard}]})

    def _finish(self, result, transcript, why: str, quiet: str, stop, take_over: threading.Event) -> CallResult:
        if take_over.is_set():
            self.line.take_over()
            return result("handed to you", "You took over the call.")
        if why == "ended":
            return result("ended", "The other side hung up." if transcript else "The call ended before anyone "
                                                                                 "answered.")
        if why == "stopped":
            self.line.hang_up()
            return result("cancelled", "You asked Argus to hang up.")
        self.line.hang_up()
        return result("no answer" if not transcript else "ended", quiet)


# ---- the tool Argus uses, and the switch the listening loop uses ---------------------------------------------
class CallManager:
    def __init__(self, settings, line_factory: Callable[[], CallLine], llm, transcriber, tts, *, audit=None,
                 speaker=None, segmenter_factory=None, cost=None, clock: Callable[[], float] = time.time,
                 engine_clock: Callable[[], float] = time.monotonic):
        self.settings, self.line_factory, self.llm = settings, line_factory, llm
        self.transcriber, self.tts, self.audit, self.speaker = transcriber, tts, audit, speaker
        self.segmenter_factory = segmenter_factory or EnergySegmenter
        self.cost, self.engine_clock = cost, engine_clock
        self.limiter = CallLimiter(settings.calls_per_hour, clock)
        self._hang_up = threading.Event()
        self._take_over = threading.Event()
        self._active = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._active

    def hang_up(self) -> None:
        self._hang_up.set()

    def take_over(self) -> None:
        self._take_over.set()

    def tool(self, args: Dict[str, Any], ctx) -> "Any":
        from .toolbox import ToolOutput
        number, why = normalize_number(str(args.get("number") or ""), self.settings.call_international)
        if number is None:
            return ToolOutput.error(f"not calling: {why}")
        goal = " ".join(str(args.get("goal") or "").split())
        if not goal:
            return ToolOutput.error("say what the call is for (goal)")
        if not self._lock.acquire(blocking=False):
            return ToolOutput.error("Argus is already on a call")
        try:
            ok, why = self.limiter.allow(number)
            if not ok:
                return ToolOutput.error(f"not calling: {why}")
            plan = CallPlan(number=number, goal=goal, business=str(args.get("business") or ""),
                            details=str(args.get("details") or ""), flexible=str(args.get("flexible") or ""),
                            owner=self.settings.user_name or "the user")
            result = self._run(plan, ctx.cancel)
        finally:
            self._lock.release()
        if self.audit is not None:
            self.audit.log("phone_call", number=number, outcome=result.outcome, seconds=round(result.seconds),
                           cost_usd=result.cost)
        self._keep_record(plan, result)
        out = ToolOutput(result.report())
        out.is_error = result.outcome == "failed"
        return out

    def _keep_record(self, plan: "CallPlan", result: "CallResult") -> None:
        """Every call, tried or made, in logs/calls.log: who, why, how it went and what was said (owner-only;
        copied to the logs folder with the other logs)."""
        path = getattr(self.audit, "path", None) or getattr(self.settings, "audit_path", None)
        if path is None:
            return
        try:
            target = Path(path).parent / "calls.log"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')}  {plan.business or plan.number} ({plan.number})\n"
                         f"Goal: {plan.goal}\n{result.report()}\n\n")
        except OSError as exc:
            log.warning("couldn't keep the call record: %s", exc)

    def _run(self, plan: CallPlan, cancel: threading.Event) -> CallResult:
        line = self.line_factory()
        problems = line.check()
        if problems:
            fix = ("" if all("restart the Mac" in p or "Terminal" in p or "macOS" in p for p in problems) else
                   " To fix all of it at once, run scripts/setup_calls.sh in Terminal (it asks for the Mac password)."
                   " Also check once: iPhone Settings > Apps > Phone > Calls on Other Devices (on, this Mac allowed),"
                   " and FaceTime > Settings > General > Calls from iPhone.")
            return CallResult("failed", "Calls aren't set up on this Mac yet: " + "; ".join(problems) + "." + fix)
        self._hang_up.clear()
        self._take_over.clear()
        if self.speaker is not None:
            self.speaker.wait_idle(timeout=8)     # don't let the call hear Argus talking to you
            self.speaker.muted = True
        self._active = True
        if self.audit is not None:
            self.audit.log("phone_call_started", number=plan.number)
        try:
            engine = CallEngine(line, self.llm, self.transcriber, self.tts,
                                model=self.settings.call_model or self.settings.model, audit=self.audit,
                                segmenter=self.segmenter_factory(), max_minutes=self.settings.call_max_minutes,
                                cost=self.cost, clock=self.engine_clock)
            return engine.run(plan, cancel, self._hang_up, self._take_over)
        except Exception as exc:
            log.exception("the call failed")
            try:
                line.hang_up()
            except Exception:
                pass
            return CallResult("failed", f"The call broke off: {exc}")
        finally:
            self._active = False
            try:
                line.close()
            finally:
                if self.speaker is not None:
                    self.speaker.muted = False


CALL_TOOL_DESCRIPTION = (
    "Phone a business from the user's own number (through their iPhone, from the Mac) and talk for them, e.g. to "
    "book a table or ask about opening hours. Look the number up first. Give the goal, the details (day, time, "
    "party size, the user's name, any requests) and how flexible they are. You'll get the outcome, a summary and "
    "the transcript; tell the user. Only for businesses the user asked you to call.")
CALL_TOOL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["number", "goal"],
                    "properties": {"number": {"type": "string"}, "business": {"type": "string"},
                                   "goal": {"type": "string"}, "details": {"type": "string"},
                                   "flexible": {"type": "string", "description": "e.g. 'any time 6:30 to 8 is fine'"}}}


# ---- the Mac line (Continuity calls through the iPhone) ----------------------------------------------------------
GREEN = (lambda rgb: (rgb[..., 1] > 150) & (rgb[..., 0] < 120) & (rgb[..., 2] < 160)
         & (rgb[..., 1].astype(int) - rgb[..., 0] > 60))
RED = lambda rgb: (rgb[..., 0] > 200) & (rgb[..., 1] < 110) & (rgb[..., 2] < 110)


def find_new_button(before: Optional[np.ndarray], after: np.ndarray, colour, min_area: int = 30,
                    max_area: int = 4000) -> Optional[Tuple[float, float]]:
    """Centre (x, y, in image pixels) of the biggest round-ish patch of `colour` that wasn't there before."""
    mask = colour(after)
    if before is not None and before.shape == after.shape:
        mask &= ~colour(before)
    seen = np.zeros_like(mask)
    best, best_area = None, 0
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys, xs):
        if seen[y0, x0]:
            continue
        stack, cells = [(y0, x0)], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            cells.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        area = len(cells)
        if not (min_area <= area <= max_area) or area <= best_area:
            continue
        cy, cx = np.mean(cells, axis=0)
        h = max(y for y, _ in cells) - min(y for y, _ in cells) + 1
        w = max(x for _, x in cells) - min(x for _, x in cells) + 1
        if max(h, w) <= 4 * min(h, w):
            best, best_area = (float(cx), float(cy)), area
    return best


class MacScreen:  # pragma: no cover - needs a Mac screen
    """Screenshots (downscaled, RGB) and clicks, for finding the call buttons."""

    STEP = 4

    def grab(self) -> np.ndarray:
        import Quartz
        image = Quartz.CGWindowListCreateImage(Quartz.CGRectInfinite, Quartz.kCGWindowListOptionOnScreenOnly,
                                               Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
        if image is None:
            raise CallError("couldn't look at the screen (Screen Recording permission?)")
        width, height = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
        row = Quartz.CGImageGetBytesPerRow(image)
        data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
        pixels = np.frombuffer(bytes(data), dtype=np.uint8).reshape(height, row // 4, 4)[:, :width, :3]
        self.scale = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.width / width
        return pixels[::self.STEP, ::self.STEP, ::-1].copy()      # BGRA -> RGB, smaller

    def click(self, x: float, y: float) -> None:
        from .computer import MacBackend
        MacBackend().click(x * self.STEP * self.scale, y * self.STEP * self.scale, "left", 1, [])


TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "~/.argus/bin")


def find_tool(name: str, which=shutil.which) -> Optional[str]:
    """The full path of a tool Argus needs. Argus runs in the background (launchd), whose PATH is only
    /usr/bin:/bin:/usr/sbin:/sbin, so Homebrew's folders are looked in as well."""
    found = which(name)
    if found:
        return found
    for folder in TOOL_DIRS:
        path = os.path.join(os.path.expanduser(folder), name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


class MacCallLine(CallLine):
    """A call placed through the iPhone from the Mac (see the module notes)."""

    def __init__(self, *, input_device: str = "BlackHole 2ch", tap: str = "~/.argus/bin/audiotee",
                 monitor: bool = True, run=subprocess.run, popen=subprocess.Popen, screen=None, player=None,
                 which=find_tool, mac_version: Callable[[], str] = lambda: platform.mac_ver()[0],
                 sleep=time.sleep, clock: Callable[[], float] = time.monotonic, exists=os.path.exists):
        self.exists = exists
        self.input_device, self.tap_path, self.monitor = input_device, os.path.expanduser(tap), monitor
        self.run, self.popen, self.screen, self.player = run, popen, screen, player
        self.which, self.mac_version, self.sleep, self.clock = which, mac_version, sleep, clock
        self._previous_input: Optional[str] = None
        self._tap: Optional[subprocess.Popen] = None
        self._audio: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=600)
        self._before: Optional[np.ndarray] = None
        self._connected_at = 0.0
        self._connected = True

    # -- setup checks --
    def _switch(self, *args: str) -> str:
        proc = self.run([self.which("SwitchAudioSource") or "SwitchAudioSource", *args], capture_output=True,
                        text=True, timeout=10)
        if proc.returncode != 0:
            raise CallError(f"couldn't change the Mac's audio ({(proc.stderr or '').strip()[:120]})")
        return (proc.stdout or "").strip()

    def check(self) -> List[str]:
        problems = []
        version = tuple(int(p) for p in re.findall(r"\d+", self.mac_version() or "")[:2])
        if not version:
            problems.append("phone calls only work on a Mac")
            return problems
        if version < (14, 2):
            problems.append("macOS 14.2 or newer is needed to hear calls")
        if not os.path.exists(self.tap_path):
            problems.append("the call recorder (audiotee) isn't installed")
        driver = "/Library/Audio/Plug-Ins/HAL/" + self.input_device.replace(" ", "") + ".driver"
        if not self.which("SwitchAudioSource"):
            problems.append("SwitchAudioSource isn't installed")
            if not self.exists(driver):            # list everything that's missing, not one thing at a time
                problems.append(f"{self.input_device} isn't installed")
        else:
            try:
                inputs = self._switch("-a", "-t", "input").splitlines()
            except (CallError, OSError, subprocess.SubprocessError):
                inputs = []
            if self.input_device not in [i.strip() for i in inputs]:
                if self.exists(driver):
                    problems.append(f"{self.input_device} is installed but macOS hasn't loaded it yet: restart "
                                    "the Mac (or run scripts/setup_calls.sh in Terminal again)")
                else:
                    problems.append(f"{self.input_device} isn't installed: run scripts/setup_calls.sh yourself in "
                                    "Terminal (it asks for your Mac password, which Argus can't type)")
        return problems

    # -- the call --
    def dial(self, number: str) -> None:
        if self.screen is None:
            self.screen = MacScreen()
        # iPhone Mirroring drops Continuity calls the moment they start.
        self.run(["osascript", "-e", 'tell application id "com.apple.ScreenContinuity" to quit'],
                 capture_output=True, text=True, timeout=10)
        self._previous_input = self._switch("-c", "-t", "input")
        self._switch("-t", "input", "-s", self.input_device)
        self._tap = self.popen([self.tap_path, "--sample-rate", str(RATE), "--chunk-duration", "0.1",
                                "--exclude-processes", str(os.getpid())],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._pump, name="argus-call-audio", daemon=True).start()
        self._before = self.screen.grab()
        self.run(["open", f"tel://{re.sub(r'[^0-9+]', '', number)}"], capture_output=True, text=True, timeout=10)
        deadline = self.clock() + 10
        while self.clock() < deadline:
            self.sleep(0.4)
            spot = find_new_button(self._before, self.screen.grab(), GREEN)
            if spot is not None:
                self.screen.click(*spot)
                self._connected_at = self.clock()
                return
        raise CallError("macOS didn't show the Call button (is Calls from iPhone turned on?)")

    def _pump(self) -> None:
        tap = self._tap
        while tap is not None and tap.stdout is not None:
            data = tap.stdout.read(3200)                  # 0.1 s of 16-bit mono at 16 kHz
            if not data:
                break
            chunk = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
            try:
                self._audio.put_nowait(chunk)
            except queue.Full:
                pass

    def read(self, timeout: float) -> Optional[np.ndarray]:
        try:
            return self._audio.get(timeout=timeout)
        except queue.Empty:
            return None

    def play(self, samples: np.ndarray, rate: int) -> None:
        data = resample(samples, rate, 48000)
        play = self.player or _play_on
        threads = []
        if self.monitor:       # you hear Argus's side too (Argus's own sound isn't recorded, so no echo)
            threads.append(threading.Thread(target=play, args=(data, 48000, None), daemon=True))
            threads[-1].start()
        play(data, 48000, self.input_device)
        for t in threads:
            t.join(timeout=30)

    def connected(self) -> bool:
        if self.clock() - self._connected_at < 3:          # the call window is still appearing
            return True
        try:
            self._connected = find_new_button(self._before, self.screen.grab(), RED) is not None
        except Exception:
            pass
        return self._connected

    def hang_up(self) -> bool:
        try:
            spot = find_new_button(self._before, self.screen.grab(), RED)
        except Exception:
            spot = None
        if spot is None:
            return False
        self.screen.click(*spot)
        return True

    def take_over(self) -> None:
        self._restore()
        self._stop_tap()

    def close(self) -> None:
        self._stop_tap()
        self._restore()

    def _stop_tap(self) -> None:
        tap, self._tap = self._tap, None
        if tap is not None and tap.poll() is None:
            tap.terminate()

    def _restore(self) -> None:
        previous, self._previous_input = self._previous_input, None
        if previous:
            try:
                self._switch("-t", "input", "-s", previous)
            except Exception as exc:
                log.error("couldn't put the microphone back to %s: %s", previous, exc)


def _play_on(data: np.ndarray, rate: int, device: Optional[str]) -> None:  # pragma: no cover - real audio
    import sounddevice as sd
    # One stream per device (sd.play shares one global stream, so two at once would cut each other off).
    with sd.OutputStream(samplerate=rate, channels=1, dtype="float32", device=device) as stream:
        stream.write(np.asarray(data, dtype=np.float32).reshape(-1, 1))
