"""Approving risky actions: on the Mac's screen with Touch ID, or by your own voice with a code word.

Anything that pays or buys always needs Touch ID. For everything else (deleting, sending, using the
screen, booking, calling someone), you can also answer out loud: Argus reads out a random code word, and
only your enrolled voice saying "yes" plus that word counts. The code word stops a TV, a video call or a
recording from approving ("yes" alone is easy to fake; a fresh random word is not), and the voice check
stops other people in the room. A good imitation of your voice could still pass, which is why money stays
on Touch ID. Argus only ever answers "once" (this action), "session" (this one task) or "deny"; never "always".
"""
from __future__ import annotations

import logging
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("argus.approvals")

ONCE, TASK, DENY = "once", "session", "deny"
ALLOWED_ANSWERS = frozenset({ONCE, TASK, DENY})

_DIALOG_SCRIPT = Path(__file__).with_name("approve.applescript")

# Easy to say, hard to mishear, and unlikely to come up by chance. One is picked at random for each request.
CODE_WORDS = (
    "maple", "river", "tiger", "rocket", "silver", "orange", "falcon", "guitar", "island", "lemon", "meadow",
    "pepper", "marble", "candle", "copper", "harbor", "jungle", "lantern", "mango", "olive", "panda", "pickle",
    "planet", "pumpkin", "raven", "salmon", "sunset", "thunder", "tulip", "velvet", "violet", "walnut", "willow",
    "zebra", "anchor", "bamboo", "blossom", "canyon", "cactus", "coconut", "comet", "crystal", "dolphin", "eagle",
    "garnet", "ginger", "glacier", "kettle", "koala", "lobster", "magnet", "meteor", "noodle", "parrot", "pebble",
    "puzzle", "robin", "saffron", "squirrel", "tornado", "trumpet", "turtle", "waffle", "volcano", "penguin")

_VERBS = frozenset({"call", "use", "write", "open", "schedule", "pay", "run", "send", "install", "delete", "change",
                    "move", "copy", "book", "buy", "post", "do"})
_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|allow|allowed|approve|approved|go ahead|do it|confirm|confirmed)\b")
_NO = re.compile(r"\b(no|nope|don'?t|do not|deny|denied|cancel|stop|never ?mind|not now|wait)\b")


@dataclass
class ApprovalRequest:
    run_id: str
    command: str = ""
    description: str = ""
    choices: List[str] = field(default_factory=list)
    pattern_key: str = ""
    origin: str = "voice"
    money: bool = False            # paying or buying: Touch ID only
    code: str = ""                 # set when a spoken answer is allowed

    @property
    def task_scope_offered(self) -> bool:
        return TASK in self.choices

    def summary(self) -> str:
        return self.command or self.description or "an action"

    def spoken_action(self) -> str:
        """What Argus asks to do, worded to follow "May I ..."."""
        if self.pattern_key == "computer:iphone":
            return "use your iPhone through iPhone Mirroring for this task"
        if self.pattern_key.startswith("computer:"):
            return "use your screen for this task"
        text = " ".join((self.description or self.command or "do that").split())[:140]
        first = text.split(" ", 1)[0].lower()
        return text if first in _VERBS else f"go ahead with {text}"

    def spoken(self) -> str:
        if self.money:
            return "That involves paying, so please approve it on the screen with Touch ID."
        if self.code:
            return f"May I {self.spoken_action()}? To allow, say yes, {self.code}. Or use the screen."
        if self.pattern_key.startswith("computer:"):
            return "I need your OK to use your Mac. Please check the screen."
        return "I need your OK for that. Please check the screen."


def _close(a: str, b: str) -> bool:
    """Same word, allowing one letter of speech-recognition slop for longer words."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    rows = [list(range(len(b) + 1))]                       # edit distance, counting a swap of two letters as one
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            best = min(rows[-1][j] + 1, row[j - 1] + 1, rows[-1][j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                best = min(best, rows[-2][j - 2] + 1)
            row.append(best)
        rows.append(row)
    return rows[-1][-1] <= 1


def parse_voice_answer(text: str, code: str) -> Optional[str]:
    """ONCE / TASK / DENY from what the owner said, or None if it wasn't an answer.
    Yes needs the code word; no doesn't (saying no can never do harm)."""
    said = " ".join(re.findall(r"[a-z']+", str(text).lower()))
    words = said.split()
    has_code = bool(code) and any(_close(w, code) for w in words)
    if _YES.search(said) and has_code and not re.search(r"\b(no|don'?t|do not|deny|cancel)\b", said):
        return ONCE if re.search(r"\b(once|just this|only this)\b", said) else TASK
    if _NO.search(said):
        return DENY
    return None


class VoiceGate:
    """The one spoken approval Argus is waiting for, if any. The listening loop hands it what the owner says."""

    def __init__(self, words=CODE_WORDS, pick: Callable[[tuple], str] = secrets.choice):
        self.words, self.pick = tuple(words), pick
        self._lock = threading.Lock()
        self._code = ""
        self._answer: Optional[str] = None
        self._event = threading.Event()

    @property
    def pending(self) -> bool:
        return bool(self._code)

    @property
    def code(self) -> str:
        return self._code

    def open(self) -> str:
        with self._lock:
            self._code = self.pick(self.words)
            self._answer = None
            self._event.clear()
            return self._code

    def offer(self, text: str) -> Optional[str]:
        """What the owner just said. Returns the decision if it was one."""
        with self._lock:
            if not self._code or self._answer is not None:
                return None
            answer = parse_voice_answer(text, self._code)
            if answer is not None:
                self._answer = answer
                self._event.set()
            return answer

    def wait(self, timeout: float) -> Optional[str]:
        self._event.wait(timeout)
        return self._answer

    def close(self) -> None:
        with self._lock:
            self._code = ""
            self._event.set()


class Approver:
    def prepare(self, request: ApprovalRequest) -> None:
        """Called before Argus speaks the request (a spoken answer needs its code word by then)."""

    def withdraw(self, request: ApprovalRequest) -> None:
        """The request was cancelled before it was asked: stop waiting for a spoken answer."""

    def decide(self, request: ApprovalRequest) -> str:
        raise NotImplementedError

    def ask(self, request: ApprovalRequest) -> str:
        """The decision, clamped to what Argus is allowed to answer."""
        try:
            answer = self.decide(request)
        except Exception:
            log.exception("approval prompt failed; denying")
            return DENY
        if answer not in ALLOWED_ANSWERS:
            return DENY
        if answer == TASK and not request.task_scope_offered:
            return ONCE
        return answer


class DenyAll(Approver):
    """For requests from the phone and for guests: nothing risky without the owner at the Mac."""

    def decide(self, request: ApprovalRequest) -> str:
        return DENY


class ScriptedApprover(Approver):
    def __init__(self, answers: List[str]):
        self.answers = list(answers)
        self.seen: List[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> str:
        self.seen.append(request)
        return self.answers.pop(0) if self.answers else DENY


def touch_id(reason: str, timeout: float = 90.0, cancel: Optional[threading.Event] = None) -> bool:
    """Touch ID, or the Mac login password when Touch ID isn't available. False if cancelled/failed."""
    try:
        from LocalAuthentication import LAContext  # pyobjc-framework-LocalAuthentication
    except ImportError:
        log.error("LocalAuthentication isn't installed; can't confirm with Touch ID")
        return False
    policy = 2  # LAPolicyDeviceOwnerAuthentication: biometrics, falling back to the account password
    context = LAContext.alloc().init()
    ok, _ = context.canEvaluatePolicy_error_(policy, None)
    if not ok:
        log.error("this Mac can't do owner authentication right now")
        return False
    done, result = threading.Event(), {"ok": False}

    def reply(success, error):
        result["ok"] = bool(success)
        done.set()

    context.evaluatePolicy_localizedReason_reply_(policy, reason, reply)
    deadline = time.monotonic() + timeout
    while not done.wait(0.2):
        if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
            context.invalidate()
            return False
    return result["ok"]


class MacApprover(Approver):
    """Dialog on the Mac's screen, then Touch ID / password for anything other than Deny."""

    def __init__(self, require_owner_auth: bool = True, timeout: int = 120, audit=None, popen=subprocess.Popen,
                 auth: Optional[Callable[..., bool]] = None):
        self.require_owner_auth = require_owner_auth
        self.timeout = timeout
        self.audit = audit
        self.popen = popen
        self.auth = auth
        self._lock = threading.Lock()  # one prompt at a time

    def message(self, request: ApprovalRequest) -> str:
        message = f"Argus wants to:\n\n{request.summary()}"
        if request.description and request.description not in message:
            message += f"\n\nWhy this needs approval: {request.description}"
        if request.money:
            message += "\n\nThis involves paying. \"Allow\" will ask for Touch ID."
        elif self.require_owner_auth:
            message += "\n\n\"Allow\" will ask for Touch ID."
        return message

    def decide(self, request: ApprovalRequest, cancel: Optional[threading.Event] = None) -> str:
        with self._lock:
            try:
                proc = self.popen(["osascript", str(_DIALOG_SCRIPT), self.message(request),
                                   "task" if request.task_scope_offered else "once", str(self.timeout)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except (OSError, subprocess.SubprocessError) as exc:
                log.error("couldn't show the approval dialog: %s", exc)
                return DENY
            deadline = time.monotonic() + self.timeout + 30
            while proc.poll() is None:
                if (cancel is not None and cancel.is_set()) or time.monotonic() > deadline:
                    proc.terminate()       # answered by voice (or nobody answered): take the dialog away
                    return DENY
                time.sleep(0.1)
            button = (proc.stdout.read() if proc.stdout else "").strip()
            answer = {"Allow once": ONCE, "Allow for this task": TASK}.get(button, DENY)
            if answer != DENY and (self.require_owner_auth or request.money):
                auth = self.auth or touch_id
                if not auth("approve an action Argus wants to take", cancel=cancel):
                    if self.audit:
                        self.audit.log("approval_auth_failed", run_id=request.run_id)
                    return DENY
            return answer


class VoiceOrScreenApprover(Approver):
    """The screen dialog (Touch ID), plus a spoken answer for anything that isn't about money.
    Whichever comes first wins; the other one is taken away."""

    def __init__(self, screen: MacApprover, gate: VoiceGate, *, voice_ok: Callable[[], bool], timeout: float = 120,
                 audit=None):
        self.screen, self.gate, self.voice_ok, self.timeout, self.audit = screen, gate, voice_ok, timeout, audit

    def _spoken_allowed(self, request: ApprovalRequest) -> bool:
        return request.origin == "voice" and not request.money and self.voice_ok()

    def prepare(self, request: ApprovalRequest) -> None:
        request.code = self.gate.open() if self._spoken_allowed(request) else ""

    def withdraw(self, request: ApprovalRequest) -> None:
        self.gate.close()

    def decide(self, request: ApprovalRequest) -> str:
        if not request.code:
            return self.screen.decide(request)
        stop, done, result = threading.Event(), threading.Event(), {}

        def screen() -> None:
            result["screen"] = self.screen.decide(request, cancel=stop)
            done.set()

        threading.Thread(target=screen, name="argus-approval-dialog", daemon=True).start()
        deadline = time.monotonic() + self.timeout
        try:
            while time.monotonic() < deadline:
                spoken = self.gate.wait(0.1)
                if spoken is not None:
                    if self.audit:
                        self.audit.log("approval_by_voice", run_id=request.run_id, choice=spoken)
                    return spoken
                if done.is_set():
                    return result.get("screen", DENY)
            return DENY
        finally:
            self.gate.close()
            stop.set()
