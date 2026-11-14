"""Argus's brain: the loop that talks to Claude, runs tools, asks for approval and remembers.

Ideas borrowed (and simplified) from Hermes Agent (MIT, Nous Research): per-task approval scopes,
pruning old screenshots, compacting long conversations with a small model. The code is Argus's own.
"""
from __future__ import annotations

import copy
import json
import re
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .approvals import DENY, ONCE, TASK, ApprovalRequest
from .audit import Audit
from .computer import NOT_EXECUTED, TOOLSET, Computer
from .lessons import IN_PROMPT, MAX_LESSONS, pick, request_text
from .llm import Claude, LLMError, StreamAccumulator
from .policy import ASK, BLOCK, Policy
from .prompts import system_blocks
from .store import Store
from . import tiers
from .toolbox import Context, Tool, ToolOutput, describe_call, web_search_spec

log = logging.getLogger("argus.agent")

# $ per million tokens: (input, output). Cache writes cost 1.25x input, cache reads 0.1x.
# Estimated cost, USD per million tokens: (input, 5-minute cache write, cache read, output).
# From the Claude API pricing page (platform.claude.com/docs/en/about-claude/pricing), November 2026.
PRICES = {
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    "claude-fable-5-2": (8.00, 10.00, 0.20, 40.00),
}
MAX_RESUMES = 2       # how often a reply that broke off halfway is picked up again
RESUME_NOTE = ("(Your reply above was cut off by a network problem. Continue exactly where it stopped: don't repeat "
               "anything you already said and don't mention the interruption.)")
LONG_PROMPT = 100_000                                         # Haiku 5.5 costs more above this prompt size
LONG_PROMPT_PRICES = {"claude-haiku-5-5": (0.50, 0.625, 0.05, 2.50)}
WEB_SEARCH_USD = 10.0 / 1000                                  # $10 per 1,000 searches
IMAGE_TOKENS = 1600          # rough cost of one 1280x800 screenshot
KEEP_IMAGES = 3              # older screenshots are replaced by a note
READ_ONLY_SCREEN = ("screenshot", "zoom", "cursor_position", "wait")
# Tools that only look things up. When Claude asks for several of these at once ("weather in Austin and in
# Chicago", "search mail and the calendar"), they run at the same time instead of one after another.
CODE_EXECUTION = {"type": "code_execution_20250825", "name": "code_execution"}
CODE_CHANNELS = frozenset({"voice", "phone", "cli", "android"})      # you asking, not guests or scheduled jobs
PARALLEL_SAFE = frozenset({"weather", "route", "get_news", "fetch_page", "recall", "read_file", "list_files",
                           "search_files", "mail_search", "mail_read", "drive_search", "drive_read", "calendar",
                           "flight_history", "place_search", "google_search", "ask_council"})
MAX_PARALLEL = 6
SCREEN_MEMBERS = ("screenshot", "zoom", "left_click", "right_click", "middle_click", "double_click", "triple_click",
                  "left_click_drag", "mouse_move", "left_mouse_down", "left_mouse_up", "cursor_position", "scroll",
                  "type", "key", "hold_key", "wait")


class Events:
    """How a channel (voice, phone, scheduler) hears about a turn as it happens."""

    def text(self, delta: str) -> None: ...
    def tool_started(self, name: str, description: str) -> None: ...
    def tool_finished(self, name: str, ok: bool) -> None: ...
    def working(self, tier: str) -> None: ...          # which kind of model took the request (tiers.py)
    def approve(self, request: ApprovalRequest) -> str:
        return DENY


class _OneAtATime(Events):
    """Passes events on from tools running at the same time, one at a time."""

    def __init__(self, events: Events):
        self.events, self.lock = events, threading.Lock()

    def text(self, delta: str) -> None:
        with self.lock:
            self.events.text(delta)

    def tool_started(self, name: str, description: str) -> None:
        with self.lock:
            self.events.tool_started(name, description)

    def tool_finished(self, name: str, ok: bool) -> None:
        with self.lock:
            self.events.tool_finished(name, ok)

    def approve(self, request):              # never reached: only tools that need no approval run together
        with self.lock:
            return self.events.approve(request)


@dataclass
class Turn:
    text: str = ""
    stopped: bool = False
    error: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    steps: List[Dict[str, Any]] = field(default_factory=list)    # what each tool call did (for lessons)
    links: List[str] = field(default_factory=list)               # for the phone to open (e.g. navigation)
    timings: Dict[str, Any] = field(default_factory=dict)        # seconds: each Claude request, each tool
    model: str = ""                                              # the Claude model that answered


# How much Claude thinks before answering. Spoken answers should start quickly: low effort, and on Sonnet 5.5 no
# thinking up front ("between_tools": it may still think between tool calls). The API docs recommend low or
# medium for chat and other latency-sensitive work. Scheduled tasks and the command line get more effort.
SPOKEN_CHANNELS = frozenset({"voice", "phone", "android", "guest", "trip", "followup", "glance"})
_EFFORT_MODELS = re.compile(r"^claude-(fable|mythos|opus|sonnet|haiku)-5|^claude-opus-4-[5-8]|^claude-sonnet-4-6")
THINKING_TYPES = ("thinking", "redacted_thinking")


def reasoning(model: str, channel: str, settings, tier: str = tiers.MAIN) -> Dict[str, Any]:
    """The output_config / thinking fields for a request on this channel."""
    spoken = channel in SPOKEN_CHANNELS
    effort = getattr(settings, "voice_effort" if spoken else "effort", "medium")
    if tier == tiers.HEAVY:                  # hard work: worth the wait, also when spoken
        effort = getattr(settings, "heavy_effort", "high")
    found: Dict[str, Any] = {}
    if _EFFORT_MODELS.match(model):
        found["output_config"] = {"effort": effort}
    if spoken and effort in ("low", "medium", "high"):
        if model.startswith("claude-sonnet-5-5"):
            found["thinking"] = {"type": "between_tools"}        # Sonnet 5.5's "no thinking up front"
        elif model.startswith("claude-haiku-5-5"):
            found["thinking"] = {"type": "disabled"}
    return found


def without_old_thinking(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Thinking blocks from earlier requests are left out: the API refuses to replay them once anything before
    them changed (the system prompt's clock line changes every minute), and earlier answers don't need them.
    The current request's own blocks (after its question) are kept, as tool use requires."""
    start = max((i for i, m in enumerate(messages) if _is_plain_user(m)), default=0)
    out = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if i < start and m.get("role") == "assistant" and isinstance(content, list):
            kept = [b for b in content if not (isinstance(b, dict) and b.get("type") in THINKING_TYPES)]
            if kept and len(kept) != len(content):
                m = dict(m, content=kept)
        out.append(m)
    return out


def request_cost(model: str, usage: Dict[str, int]) -> float:
    """Estimated USD for one API request, from the usage numbers the API returned."""
    prompt = sum(usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    prices = LONG_PROMPT_PRICES.get(model) if prompt > LONG_PROMPT else None
    p_in, p_write, p_read, p_out = prices or PRICES.get(model, PRICES["claude-sonnet-5-5"])
    tokens = (usage.get("input_tokens", 0) * p_in + usage.get("cache_creation_input_tokens", 0) * p_write +
              usage.get("cache_read_input_tokens", 0) * p_read + usage.get("output_tokens", 0) * p_out) / 1e6
    return tokens + usage.get("web_search_requests", 0) * WEB_SEARCH_USD


def budget_tokens(usage: Dict[str, int]) -> int:
    """How much of the per-request token budget one API call used (cache reads count a tenth)."""
    return (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) +
            usage.get("cache_read_input_tokens", 0) // 10 + usage.get("output_tokens", 0))


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        stack = [m["content"]]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                total += len(item) // 4
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if item.get("type") == "image":
                    total += IMAGE_TOKENS
                    continue
                stack.extend(v for k, v in item.items() if k not in ("encrypted_content", "signature"))
    return total


def _is_plain_user(message: Dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    return isinstance(content, str) or not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def repair(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Start at a plain user message and make sure every tool call has a result (after a crash or stop)."""
    while messages and not _is_plain_user(messages[0]):
        messages = messages[1:]
    fixed: List[Dict[str, Any]] = []
    for i, m in enumerate(messages):
        fixed.append(m)
        if m["role"] != "assistant" or not isinstance(m["content"], list):
            continue
        ids = [b["id"] for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
        if not ids:
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        answered = {b.get("tool_use_id") for b in (nxt or {}).get("content", []) if isinstance(b, dict)} if nxt and \
            isinstance(nxt.get("content"), list) else set()
        missing = [b for b in m["content"] if b.get("type") == "tool_use" and b["id"] not in answered]
        if missing and (nxt is None or not answered):
            results = []
            for b in missing:
                r = {"type": "tool_result", "tool_use_id": b["id"], "content": "Interrupted.", "is_error": True}
                if b.get("toolset_name"):
                    r["toolset_name"] = b["toolset_name"]
                results.append(r)
            fixed.append({"role": "user", "content": results})
    return fixed


def prune_images(messages: List[Dict[str, Any]], keep: int = KEEP_IMAGES) -> List[Dict[str, Any]]:
    """Copy of `messages` where only the newest `keep` images remain (screenshots are expensive)."""
    out = copy.deepcopy(messages)
    seen = 0
    for m in reversed(out):
        if not isinstance(m.get("content"), list):
            continue
        for block in reversed(m["content"]):
            if not isinstance(block, dict):
                continue
            holders = [block] if block.get("type") == "image" else [
                b for b in (block.get("content") if isinstance(block.get("content"), list) else []) if
                isinstance(b, dict) and b.get("type") == "image"]
            for img in holders:
                seen += 1
                if seen > keep:
                    img.clear()
                    img.update({"type": "text", "text": "[older image removed to save space]"})
    return out


def _step(block: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """One tool call, summarised for the lessons Argus learns from its own mistakes."""
    name, args = str(block.get("name")), block.get("input")
    if block.get("toolset_name") == "computer":
        what = f"screen: {name.replace('_', ' ')}"
    else:
        what = describe_call(name, args if isinstance(args, dict) else {})
    content = result.get("content")
    error = content if isinstance(content, str) else "error"
    return {"tool": name, "what": what, "ok": not result.get("is_error"),
            "error": error[:300] if result.get("is_error") else ""}


class Agent:
    def __init__(self, llm: Claude, store: Store, policy: Policy, tools: Dict[str, Tool], *, settings,
                 audit: Audit, computer: Optional[Computer] = None,
                 glasses=None, glasses_status: Callable[[], Optional[str]] = lambda: None,
                 secrets: Sequence[str] = (), reflector=None, device: str = "mac"):
        self.llm = llm
        self.device = device                  # where this Argus runs: "mac", "server" or "android"
        self.reflector = reflector            # learns a lesson after a request that hit errors (lessons.py)
        self.store = store
        self.policy = policy
        self.tools = tools
        self.settings = settings
        self.audit = audit
        self.computer = computer
        self.glasses = glasses
        self.glasses_status = glasses_status
        self.loose_ends: Callable[[], Sequence[Tuple[int, str]]] = lambda: ()     # set by followups (app.py)
        # Argus's own keys and tokens: never shown to Claude, never sent anywhere by a tool.
        self.secrets = sorted({str(v) for v in secrets if v and len(str(v)) >= 8}, key=len, reverse=True)
        self._lock = threading.Lock()
        self._last_tier: Dict[str, Tuple[str, float]] = {}     # per conversation: which kind of model answered last

    def add_secret(self, value: str) -> None:
        if value and len(value) >= 8 and value not in self.secrets:
            self.secrets = sorted(set(self.secrets) | {value}, key=len, reverse=True)

    def _redact(self, content: Any) -> Any:
        if not self.secrets:
            return content
        if isinstance(content, str):
            for value in self.secrets:
                content = content.replace(value, "[secret removed]")
            return content
        if isinstance(content, list):
            return [dict(b, text=self._redact(b["text"])) if isinstance(b, dict) and isinstance(b.get("text"), str)
                    else b for b in content]
        return content

    def _mentions_secret(self, args: Any) -> bool:
        text = json.dumps(args, ensure_ascii=False)
        return any(value in text for value in self.secrets)

    # ---- request building --------------------------------------------------------------------------
    def _tools_for(self, channel: str, model: str = "", tier: str = tiers.MAIN) -> List[Dict[str, Any]]:
        specs = [t.spec() for t in self.tools.values() if channel in t.channels]
        if tier == tiers.LIGHT:
            specs.append(dict(tiers.HAND_OVER))
        if self.settings.web_search and channel != "glance":
            specs.append(web_search_spec(self.settings.blocked_sites, self.settings.city, self.settings.timezone))
        if getattr(self.settings, "code_execution", False) and channel in CODE_CHANNELS and tier != tiers.LIGHT:
            specs.append(dict(CODE_EXECUTION))   # Anthropic's sandbox: no internet, nothing of the Mac
        desktop = channel == "voice"
        if self.computer is not None and (desktop or channel == "phone") and tier != tiers.LIGHT:
            toolset = dict(TOOLSET)
            if not desktop:   # from the phone: look, never touch
                toolset["configs"] = {m: {"enabled": False} for m in SCREEN_MEMBERS if m not in READ_ONLY_SCREEN}
            specs.append(toolset)
        if specs:
            specs[-1] = dict(specs[-1], cache_control={"type": "ephemeral"})
        return specs

    def _body(self, channel: str, system: List[Dict[str, Any]], messages: List[Dict[str, Any]],
              model: str = "", tier: str = tiers.MAIN) -> Dict[str, Any]:
        model = model or self.settings.model
        prepared = without_old_thinking(prune_images(messages))
        last = prepared[-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        for block in reversed(last["content"]):
            if isinstance(block, dict) and block.get("type") in ("text", "tool_result", "image"):
                block["cache_control"] = {"type": "ephemeral"}
                break
        return dict({"model": model, "max_tokens": 8192, "system": system, "messages": prepared,
                     "tools": self._tools_for(channel, model, tier)},
                    **reasoning(model, channel, self.settings, tier))

    # ---- conversation upkeep -----------------------------------------------------------------------
    def _compact(self, session_id: str, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if estimate_tokens(messages) <= self.settings.context_tokens or len(messages) < 8:
            return messages
        split = None
        for i in range(len(messages) - 6, 0, -1):   # keep at least the last few exchanges verbatim
            if _is_plain_user(messages[i]):
                split = i
                break
        if not split:
            return messages
        transcript = json.dumps(prune_images(messages[:split], keep=0), ensure_ascii=False)[-120_000:]
        try:
            reply = self.llm.create({"model": self.settings.small_model, "max_tokens": 800, "messages": [{
                "role": "user", "content": "Summarise this conversation between a user and their assistant so "
                "the assistant can continue it: the user's goals, facts learned, decisions, results of actions, "
                "and anything still pending. Under 250 words, plain text.\n\n" + transcript}]})
            summary = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text").strip()
            cost = request_cost(self.settings.small_model, reply.get("usage") or {})
        except LLMError as exc:
            log.warning("couldn't summarise the conversation: %s", exc)
            return messages
        if not summary:   # keep the full conversation rather than replace it with nothing
            return messages
        compacted = [{"role": "user", "content": f"(Summary of our conversation so far)\n{summary}"},
                     {"role": "assistant", "content": [{"type": "text", "text": "Understood."}]}] + messages[split:]
        self.store.replace_history(session_id, compacted)
        self.audit.log("conversation_compacted", session=session_id, before=len(messages), after=len(compacted),
                       cost_usd=round(cost, 4))
        return compacted

    # ---- one tool call -----------------------------------------------------------------------------
    def _call(self, block: Dict[str, Any], *, channel: str, session_id: str, events: Events, ctx: Context,
              grants: Set[str], earlier_screen_failure: bool) -> Dict[str, Any]:
        name, args = str(block.get("name")), block.get("input")
        args = {} if args is None else args
        toolset = str(block.get("toolset_name") or "")
        result: Dict[str, Any] = {"type": "tool_result", "tool_use_id": block["id"]}
        if toolset:
            result["toolset_name"] = toolset

        def fail(text: str) -> Dict[str, Any]:
            result.update(content=text, is_error=True)
            return result

        if not isinstance(args, dict):
            return fail("The tool input must be a JSON object.")
        if self._mentions_secret(args):
            self.audit.log("tool_blocked", channel=channel, tool=name, reason="contains one of Argus's keys")
            return fail("Blocked by Argus rules: that would use one of Argus's private keys.")

        if toolset == "computer" and earlier_screen_failure:
            return fail(NOT_EXECUTED)
        if ctx.cancel.is_set():
            return fail("Cancelled by the user.")
        tool = None
        if toolset == "computer":
            if self.computer is None:
                return fail("Screen control isn't available on this computer.")
        else:
            tool = self.tools.get(name)
            if tool is None or channel not in tool.channels:
                return fail(f"The tool {name} isn't available here.")
        verdict = self.policy.check(name, args, channel, toolset)
        what = f"use the screen: {name.replace('_', ' ')}" if toolset == "computer" else describe_call(name, args)
        if verdict.action == BLOCK:
            self.audit.log("tool_blocked", channel=channel, tool=name, reason=verdict.reason)
            return fail(f"Blocked by Argus rules: {verdict.reason}")
        if verdict.action == ASK and verdict.scope not in grants:
            request = ApprovalRequest(run_id=session_id, command=what, description=verdict.reason,
                                      choices=["once", "session", "deny"], pattern_key=verdict.scope, origin=channel,
                                      money=verdict.money, short=verdict.short,
                                      touch_only=verdict.touch_only)
            try:
                choice = events.approve(request)
            except Exception as exc:   # a broken dialog must never count as a yes
                log.exception("asking for approval failed")
                self.audit.log("approval_failed", channel=channel, tool=name, error=str(exc)[:200])
                choice = DENY
            if choice not in (ONCE, TASK):
                choice = DENY
            self.audit.log("approval", channel=channel, tool=name, action=what[:200], choice=choice)
            if choice == DENY:
                return fail("The user declined this action. Don't retry it; ask what they'd prefer instead.")
            if choice == TASK:
                grants.add(verdict.scope)
        events.tool_started(name, what)
        self.audit.log("tool", channel=channel, tool=name, action=what[:200])
        started = time.monotonic()
        try:
            out = self.computer.run(name, args) if toolset == "computer" else tool.run(args, ctx)
        except Exception as exc:
            log.exception("tool %s crashed", name)
            out = ToolOutput.error(f"{name} failed: {exc}")
        events.tool_finished(name, not out.is_error)
        if out.is_error:
            detail = out.content if isinstance(out.content, str) else "error"
            self.audit.log("tool_failed", channel=channel, tool=name, error=self._redact(detail)[:300])
        if time.monotonic() - started > 5:
            self.audit.log("tool_slow", tool=name, seconds=round(time.monotonic() - started, 1))
        result["content"] = self._redact(out.content)
        if out.is_error:
            result["is_error"] = True
        return result

    def _can_run_together(self, uses: List[Dict[str, Any]], channel: str) -> bool:
        """Several lookups that need no approval can run at the same time."""
        if len(uses) < 2:
            return False
        for block in uses:
            name = str(block.get("name"))
            if block.get("toolset_name") or name not in PARALLEL_SAFE or name not in self.tools:
                return False
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            if self.policy.check(name, args, channel, "").action != "allow":
                return False
        return True

    # ---- a whole turn -------------------------------------------------------------------------------
    def _models_note(self, model: str, tier: str) -> str:
        """Which model is answering and how the others are used, so Argus can say so when asked."""
        s = self.settings
        kind = {tiers.LIGHT: "the quick model", tiers.HEAVY: "the heavy model"}.get(tier, "the main model")
        parts = [f"Models: this answer comes from {model} ({kind})."]
        if getattr(s, "routing", "auto") == "auto":
            parts.append(f"Argus picks by the request: everyday questions {s.light_model or s.model}; tasks, "
                         f"advice, plans and recommendations {s.model}; code, analysis and long writing "
                         f"{s.heavy_model or s.model}.")
        if getattr(s, "gemini_model", ""):
            parts.append(f"google_search runs on Google's {s.gemini_model}.")
        if getattr(s, "council", None):
            parts.append("ask_council gets second opinions from the other AI models set up with argusctl brains.")
        parts.append(f"Phone calls use {getattr(s, 'call_model', '') or s.model}.")
        code = Path(__file__).resolve().parent.parent
        parts.append(f"Your own code is in {code} (setup scripts in {code}/scripts; the user runs them in Terminal).")
        return " ".join(parts)

    def run(self, *, session_id: str, channel: str, content: Any, events: Events,
            cancel: Optional[threading.Event] = None, location: Optional[Dict[str, float]] = None,
            locate: Optional[Callable[[], Optional[Dict[str, float]]]] = None) -> Turn:
        cancel = cancel or threading.Event()
        turn = Turn()
        messages = repair(self.store.history(session_id, limit=self.settings.history_messages))
        self.store.append(session_id, "user", content)
        messages.append({"role": "user", "content": content})
        guest = channel == "guest"           # someone other than the owner: nothing personal in the prompt
        asked = request_text(content)
        lessons = [] if guest else pick(self.store.lessons(MAX_LESSONS), asked, IN_PROMPT)
        self.store.used_lessons([i for i, _, _ in lessons])
        system = system_blocks(user=self.settings.user_name, city=self.settings.city, timezone=self.settings.timezone,
                               workspace=str(self.settings.workspace), channel=channel,
                               facts=() if guest else self.store.facts(limit=40),
                               lessons=[(i, text) for i, text, _ in lessons],
                               glasses=None if guest else self.glasses_status(), device=self.device,
                               loose_ends=() if guest or channel in ("glance", "followup") else self.loose_ends(),
                               phone=getattr(self.settings, "phone_type", "iphone"),
                               location_known=bool(location or locate) and not guest)
        ctx = Context(channel=channel, workspace=self.settings.workspace, store=self.store, cancel=cancel,
                      glasses=self.glasses, timezone=self.settings.timezone or None,
                      location=None if guest else location, locate=None if guest else locate)
        grants: Set[str] = set()
        spent = 0
        tier, model = tiers.choose(content, channel, self.settings)
        before = self._last_tier.get(session_id)
        if (tier == tiers.LIGHT and before and time.monotonic() - before[1] < tiers.STICKY_SECONDS
                and len(str(content).split()) <= tiers.SHORT_REPLY):
            tier = before[0]          # "yes", "do it", "and tomorrow?": the answer to what a bigger model just said
            model = tiers.model_for(tier, self.settings)
        turn.model = model
        system = system[:-1] + [dict(system[-1], text=system[-1]["text"] + "\n\n" + self._models_note(model, tier))]
        getattr(events, "working", lambda _tier: None)(tier)
        resumes, said_before = 0, ""
        for _ in range(self.settings.max_steps):
            if cancel.is_set():
                turn.stopped = True
                break
            messages = self._compact(session_id, messages)
            acc = StreamAccumulator()
            asked_at, first_at = time.monotonic(), None
            try:
                for event in self.llm.stream(self._body(channel, system, messages, model, tier), cancel):
                    if first_at is None and event.get("type") == "content_block_delta":
                        first_at = time.monotonic()
                        turn.timings.setdefault("claude_first_s", []).append(round(first_at - asked_at, 2))
                    delta = acc.feed(event)
                    if delta:
                        events.text(delta)
                    elif event.get("type") == "content_block_start" and \
                            (event.get("content_block") or {}).get("type") == "server_tool_use":
                        events.tool_started(str(event["content_block"].get("name")), "search the web")
            except LLMError as exc:
                self.audit.log("llm_error", channel=channel, error=str(exc)[:300], partial=exc.partial or None)
                if exc.partial and exc.retryable and resumes < MAX_RESUMES and not cancel.is_set():
                    # The reply broke off halfway (a dropped connection or a stall). Ask Claude to carry on
                    # from where it stopped, so what was already said isn't said twice.
                    resumes += 1
                    for key, value in acc.usage.items():          # the broken request still cost something
                        turn.usage[key] = turn.usage.get(key, 0) + value
                    turn.cost += request_cost(model, acc.usage)
                    spent += budget_tokens(acc.usage)
                    said = "".join(b.get("text", "") for b in acc.content if b.get("type") == "text").strip()
                    if said:
                        for role, text in (("assistant", said), ("user", RESUME_NOTE)):
                            block = [{"type": "text", "text": text}]
                            messages.append({"role": role, "content": block})
                            self.store.append(session_id, role, block)
                        said_before = (said_before + " " + said).strip()
                    self.audit.log("reply_resumed", channel=channel, after_chars=len(said))
                    continue
                turn.error = str(exc)
                break
            turn.timings.setdefault("claude_s", []).append(round(time.monotonic() - asked_at, 2))
            for key, value in acc.usage.items():
                turn.usage[key] = turn.usage.get(key, 0) + value
            turn.cost += request_cost(model, acc.usage)
            spent += budget_tokens(acc.usage)
            blocks = [b for b in acc.content if not (b.get("type") == "text" and not b.get("text"))]
            wanted = next((b for b in blocks if b.get("type") == "tool_use" and b.get("name") == "hand_over"), None)
            if wanted is not None and tier == tiers.LIGHT and not cancel.is_set():
                # The quick model says this is beyond it. Its message is dropped and a stronger model takes over.
                level = (wanted.get("input") or {}).get("level")
                tier = tiers.HEAVY if level == tiers.HEAVY and self.settings.heavy_model else tiers.MAIN
                model = tiers.model_for(tier, self.settings)
                turn.model = model
                self.audit.log("handed_over", channel=channel, to=model)
                if not _is_plain_user(messages[-1]):          # it had already used tools: start a clean exchange
                    for role, text in zip(("assistant", "user"), tiers.BRIDGE):
                        block = [{"type": "text", "text": text}]
                        messages.append({"role": role, "content": block})
                        self.store.append(session_id, role, block)
                getattr(events, "working", lambda _tier: None)(tier)
                continue
            if cancel.is_set():
                turn.stopped = True
                text_only = [b for b in blocks if b.get("type") == "text"]
                if text_only:
                    self.store.append(session_id, "assistant", text_only)
                break
            if not blocks:
                break
            messages.append({"role": "assistant", "content": blocks})
            self.store.append(session_id, "assistant", blocks)
            if acc.text:
                glue = "" if not said_before or acc.text[:1].isspace() or acc.text[:1] in ".,;:!?" else " "
                turn.text = (said_before + glue + acc.text).strip() if said_before else acc.text
                said_before = ""
            if acc.stop_reason == "pause_turn":
                continue
            uses = acc.tool_uses()
            if acc.stop_reason != "tool_use" or not uses:
                break
            results: List[Dict[str, Any]] = []
            screen_failed = False
            if self._can_run_together(uses, channel):
                shared = _OneAtATime(events)

                def one(block):
                    began = time.monotonic()
                    res = self._call(block, channel=channel, session_id=session_id, events=shared, ctx=ctx,
                                     grants=grants, earlier_screen_failure=False)
                    return res, round(time.monotonic() - began, 2)
                with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(uses))) as pool:
                    done = list(pool.map(one, uses))
                for block, (res, took) in zip(uses, done):
                    turn.timings.setdefault("tools", []).append([str(block.get("name") or "tool"), took])
                    results.append(res)
                    turn.steps.append(_step(block, res))
                self.audit.log("tools_together", count=len(uses))
            for block in ([] if results else uses):
                began = time.monotonic()
                res = self._call(block, channel=channel, session_id=session_id, events=events, ctx=ctx,
                                 grants=grants, earlier_screen_failure=screen_failed)
                took = round(time.monotonic() - began, 2)
                turn.timings.setdefault("tools", []).append([str(block.get("name") or "tool"), took])
                if block.get("toolset_name") == "computer" and res.get("is_error"):
                    screen_failed = True
                results.append(res)
                turn.steps.append(_step(block, res))
            messages.append({"role": "user", "content": results})
            self.store.append(session_id, "user", results)
            if spent > self.settings.max_turn_tokens:
                turn.error = "this request used its token budget; stopped to protect your API credit"
                break
        else:
            turn.error = "stopped after too many steps"
        self.audit.log("turn", channel=channel, session=session_id, stopped=turn.stopped or None, error=turn.error,
                       model=turn.model,
                       input_tokens=turn.usage.get("input_tokens"), output_tokens=turn.usage.get("output_tokens"),
                       cache_read=turn.usage.get("cache_read_input_tokens"),
                       web_searches=turn.usage.get("web_search_requests"), cost_usd=round(turn.cost, 4),
                       timings=turn.timings or None)
        self._last_tier[session_id] = (tier, time.monotonic())
        turn.links = list(dict.fromkeys(ctx.links))
        if self.reflector is not None and channel in ("voice", "phone", "cli", "android") and not turn.stopped:
            self.reflector.learn_later(asked, turn.steps, turn.text)
        return turn
