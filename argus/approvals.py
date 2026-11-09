"""Approving risky actions: a click on the Mac plus Touch ID (or your Mac password).

Never by voice: a TV, a video call or a recording could say "yes". Argus only ever answers
"once" (this action), "session" (this one task) or "deny". It never grants "always".
"""
from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("argus.approvals")

ONCE, TASK, DENY = "once", "session", "deny"
ALLOWED_ANSWERS = frozenset({ONCE, TASK, DENY})

_DIALOG_SCRIPT = Path(__file__).with_name("approve.applescript")


@dataclass
class ApprovalRequest:
    run_id: str
    command: str = ""
    description: str = ""
    choices: List[str] = field(default_factory=list)
    pattern_key: str = ""
    origin: str = "voice"

    @classmethod
    def from_event(cls, run_id: str, event: Dict[str, Any], origin: str = "voice") -> "ApprovalRequest":
        return cls(run_id=run_id, command=str(event.get("command") or "")[:600],
                   description=str(event.get("description") or "")[:300],
                   choices=[str(c) for c in (event.get("choices") or [])],
                   pattern_key=str(event.get("pattern_key") or ""), origin=origin)

    @property
    def task_scope_offered(self) -> bool:
        return TASK in self.choices

    def summary(self) -> str:
        return self.command or self.description or "an action"

    def spoken(self) -> str:
        if self.pattern_key.startswith("computer:"):
            return "I need your OK to use your Mac. Please check the screen."
        return "I need your OK for that. Please check the screen."


class Approver:
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
    """For requests from the phone: nothing risky without being at the Mac (or using Telegram)."""

    def decide(self, request: ApprovalRequest) -> str:
        return DENY


class ScriptedApprover(Approver):
    def __init__(self, answers: List[str]):
        self.answers = list(answers)
        self.seen: List[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> str:
        self.seen.append(request)
        return self.answers.pop(0) if self.answers else DENY


def touch_id(reason: str, timeout: float = 90.0) -> bool:
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
    if not done.wait(timeout):
        context.invalidate()
        return False
    return result["ok"]


class MacApprover(Approver):
    """Dialog on the Mac's screen, then Touch ID / password for anything other than Deny."""

    def __init__(self, require_owner_auth: bool = True, timeout: int = 120, audit=None):
        self.require_owner_auth = require_owner_auth
        self.timeout = timeout
        self.audit = audit
        self._lock = threading.Lock()  # one prompt at a time

    def decide(self, request: ApprovalRequest) -> str:
        message = f"Argus wants to:\n\n{request.summary()}"
        if request.description and request.description not in message:
            message += f"\n\nWhy this needs approval: {request.description}"
        message += "\n\n\"Allow\" will ask for Touch ID." if self.require_owner_auth else ""
        with self._lock:
            proc = subprocess.run(
                ["osascript", str(_DIALOG_SCRIPT), message, "task" if request.task_scope_offered else "once",
                 str(self.timeout)], capture_output=True, text=True, timeout=self.timeout + 30)
            button = proc.stdout.strip()
            answer = {"Allow once": ONCE, "Allow for this task": TASK}.get(button, DENY)
            if answer != DENY and self.require_owner_auth:
                if not touch_id("approve an action Argus wants to take"):
                    if self.audit:
                        self.audit.log("approval_auth_failed", run_id=request.run_id)
                    return DENY
            return answer
