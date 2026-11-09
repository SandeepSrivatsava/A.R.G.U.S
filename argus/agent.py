"""Argus's brain: the loop that talks to Claude, runs tools, asks for approval and remembers.

Ideas borrowed (and simplified) from Hermes Agent (MIT, Nous Research): per-task approval scopes,
pruning old screenshots, compacting long conversations with a small model. The code is Argus's own.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from .approvals import DENY, TASK, ApprovalRequest
from .audit import Audit
from .computer import NOT_EXECUTED, TOOLSET, Computer
from .llm import Claude, LLMError, StreamAccumulator
from .policy import ASK, BLOCK, Policy
from .prompts import system_blocks
from .store import Store
from .toolbox import Context, Tool, ToolOutput, describe_call, web_search_spec

log = logging.getLogger("argus.agent")

# $ per million tokens: (input, output). Cache writes cost 1.25x input, cache reads 0.1x.
# Estimated cost, USD per million tokens: (input, 5-minute cache write, cache read, output).
# From the Claude API pricing page (platform.claude.com/docs/en/about-claude/pricing), November 2026.
PRICES = {
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
}
LONG_PROMPT = 100_000                                         # Haiku 5.5 costs more above this prompt size
LONG_PROMPT_PRICES = {"claude-haiku-5-5": (0.50, 0.625, 0.05, 2.50)}
WEB_SEARCH_USD = 10.0 / 1000                                  # $10 per 1,000 searches
IMAGE_TOKENS = 1600          # rough cost of one 1280x800 screenshot
KEEP_IMAGES = 3              # older screenshots are replaced by a note
READ_ONLY_SCREEN = ("screenshot", "zoom", "cursor_position", "wait")
SCREEN_MEMBERS = ("screenshot", "zoom", "left_click", "right_click", "middle_click", "double_click", "triple_click",
                  "left_click_drag", "mouse_move", "left_mouse_down", "left_mouse_up", "cursor_position", "scroll",
                  "type", "key", "hold_key", "wait")


class Events:
    """How a channel (voice, Telegram, phone, scheduler) hears about a turn as it happens."""

    def text(self, delta: str) -> None: ...
    def tool_started(self, name: str, description: str) -> None: ...
    def tool_finished(self, name: str, ok: bool) -> None: ...
    def approve(self, request: ApprovalRequest) -> str:
        return DENY


@dataclass
class Turn:
    text: str = ""
    stopped: bool = False
    error: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)
    cost: float = 0.0


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


class Agent:
    def __init__(self, llm: Claude, store: Store, policy: Policy, tools: Dict[str, Tool], *, settings,
                 audit: Audit, computer: Optional[Computer] = None, notify: Callable[[str], bool] = lambda t: False,
                 glasses=None, glasses_status: Callable[[], Optional[str]] = lambda: None):
        self.llm = llm
        self.store = store
        self.policy = policy
        self.tools = tools
        self.settings = settings
        self.audit = audit
        self.computer = computer
        self.notify = notify
        self.glasses = glasses
        self.glasses_status = glasses_status
        self._lock = threading.Lock()

    # ---- request building --------------------------------------------------------------------------
    def _tools_for(self, channel: str) -> List[Dict[str, Any]]:
        specs = [t.spec() for t in self.tools.values() if channel in t.channels]
        if self.settings.web_search:
            specs.append(web_search_spec(self.settings.blocked_sites, self.settings.city, self.settings.timezone))
        desktop = channel == "voice" or (channel == "telegram" and self.settings.telegram_desktop)
        if self.computer is not None and (desktop or channel == "phone"):
            toolset = dict(TOOLSET)
            if not desktop:   # from the phone: look, never touch
                toolset["configs"] = {m: {"enabled": False} for m in SCREEN_MEMBERS if m not in READ_ONLY_SCREEN}
            specs.append(toolset)
        if specs:
            specs[-1] = dict(specs[-1], cache_control={"type": "ephemeral"})
        return specs

    def _body(self, channel: str, system: List[Dict[str, Any]], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        prepared = prune_images(messages)
        last = prepared[-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        for block in reversed(last["content"]):
            if isinstance(block, dict) and block.get("type") in ("text", "tool_result", "image"):
                block["cache_control"] = {"type": "ephemeral"}
                break
        return {"model": self.settings.model, "max_tokens": 8192, "system": system, "messages": prepared,
                "tools": self._tools_for(channel)}

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
        compacted = [{"role": "user", "content": f"(Summary of our conversation so far)\n{summary}"},
                     {"role": "assistant", "content": [{"type": "text", "text": "Understood."}]}] + messages[split:]
        self.store.replace_history(session_id, compacted)
        self.audit.log("conversation_compacted", session=session_id, before=len(messages), after=len(compacted),
                       cost_usd=round(cost, 4))
        return compacted

    # ---- one tool call -----------------------------------------------------------------------------
    def _call(self, block: Dict[str, Any], *, channel: str, session_id: str, events: Events, ctx: Context,
              grants: Set[str], earlier_screen_failure: bool) -> Dict[str, Any]:
        name, args = str(block.get("name")), block.get("input") or {}
        toolset = str(block.get("toolset_name") or "")
        result: Dict[str, Any] = {"type": "tool_result", "tool_use_id": block["id"]}
        if toolset:
            result["toolset_name"] = toolset

        def fail(text: str) -> Dict[str, Any]:
            result.update(content=text, is_error=True)
            return result

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
                                      choices=["once", "session", "deny"], pattern_key=verdict.scope, origin=channel)
            choice = events.approve(request)
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
        if time.monotonic() - started > 5:
            self.audit.log("tool_slow", tool=name, seconds=round(time.monotonic() - started, 1))
        result["content"] = out.content
        if out.is_error:
            result["is_error"] = True
        return result

    # ---- a whole turn -------------------------------------------------------------------------------
    def run(self, *, session_id: str, channel: str, content: Any, events: Events,
            cancel: Optional[threading.Event] = None) -> Turn:
        cancel = cancel or threading.Event()
        turn = Turn()
        messages = repair(self.store.history(session_id, limit=self.settings.history_messages))
        self.store.append(session_id, "user", content)
        messages.append({"role": "user", "content": content})
        system = system_blocks(user=self.settings.user_name, city=self.settings.city, timezone=self.settings.timezone,
                               workspace=str(self.settings.workspace), channel=channel,
                               facts=self.store.facts(limit=40), glasses=self.glasses_status())
        ctx = Context(channel=channel, workspace=self.settings.workspace, store=self.store, cancel=cancel,
                      notify=self.notify, glasses=self.glasses, timezone=self.settings.timezone or None)
        grants: Set[str] = set()
        spent = 0
        for _ in range(self.settings.max_steps):
            if cancel.is_set():
                turn.stopped = True
                break
            messages = self._compact(session_id, messages)
            acc = StreamAccumulator()
            try:
                for event in self.llm.stream(self._body(channel, system, messages), cancel):
                    delta = acc.feed(event)
                    if delta:
                        events.text(delta)
                    elif event.get("type") == "content_block_start" and \
                            (event.get("content_block") or {}).get("type") == "server_tool_use":
                        events.tool_started(str(event["content_block"].get("name")), "search the web")
            except LLMError as exc:
                turn.error = str(exc)
                self.audit.log("llm_error", channel=channel, error=str(exc)[:300])
                break
            for key, value in acc.usage.items():
                turn.usage[key] = turn.usage.get(key, 0) + value
            turn.cost += request_cost(self.settings.model, acc.usage)
            spent += budget_tokens(acc.usage)
            blocks = [b for b in acc.content if not (b.get("type") == "text" and not b.get("text"))]
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
            turn.text = acc.text or turn.text
            if acc.stop_reason == "pause_turn":
                continue
            uses = acc.tool_uses()
            if acc.stop_reason != "tool_use" or not uses:
                break
            results: List[Dict[str, Any]] = []
            screen_failed = False
            for block in uses:
                res = self._call(block, channel=channel, session_id=session_id, events=events, ctx=ctx,
                                 grants=grants, earlier_screen_failure=screen_failed)
                if block.get("toolset_name") == "computer" and res.get("is_error"):
                    screen_failed = True
                results.append(res)
            messages.append({"role": "user", "content": results})
            self.store.append(session_id, "user", results)
            if spent > self.settings.max_turn_tokens:
                turn.error = "this request used its token budget; stopped to protect your API credit"
                break
        else:
            turn.error = "stopped after too many steps"
        self.audit.log("turn", channel=channel, session=session_id, stopped=turn.stopped or None, error=turn.error,
                       input_tokens=turn.usage.get("input_tokens"), output_tokens=turn.usage.get("output_tokens"),
                       cache_read=turn.usage.get("cache_read_input_tokens"),
                       web_searches=turn.usage.get("web_search_requests"), cost_usd=round(turn.cost, 4))
        return turn
