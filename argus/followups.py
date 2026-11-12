"""Loose ends: things left open that Argus keeps an eye on and brings up by itself at the right moment.

"I still need a hotel for New York" -> Argus notes the loose end, and every few days quietly looks into it
(prices, dates getting close, a reply that hasn't come). When there's something worth hearing it says so
("Hotel prices for your dates went up; want me to look now?"); when there isn't, it says nothing.

* A check can only look things up (web and Google search, places, weather, flights, the calendar): it runs on
  its own restricted channel, like trip checks, so nothing can be sent, bought or changed.
* Quiet from 9 at night to 8 in the morning. At most 12 loose ends, each for at most 90 days.
* "What's still open?" lists them; "forget the hotel one" or sorting it out closes it.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_OPEN = 12
MAX_DAYS = 90
QUIET_FROM, QUIET_UNTIL = 21, 8
NOTHING = "NOTHING"
MODE = "followup"


def read(text: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("what"):
        return None
    return data


def open_ends(store) -> List[Tuple[int, Dict[str, Any], float]]:
    out = []
    for task in store.tasks():
        if task["mode"] == MODE:
            end = read(task["text"])
            if end is not None:
                out.append((task["id"], end, task["next_run"] or 0.0))
    return out


def in_prompt(store, limit: int = 6) -> List[Tuple[int, str]]:
    return [(i, str(end["what"])[:140]) for i, end, _ in open_ends(store)[:limit]]


def _zone(tz: Optional[str]):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz) if tz else None
    except Exception:
        return None


def out_of_quiet_hours(at: float, tz: Optional[str]) -> float:
    """`at`, or 8 the next morning when it falls at night."""
    local = datetime.fromtimestamp(at, _zone(tz))
    if QUIET_UNTIL <= local.hour < QUIET_FROM:
        return at
    morning = local.replace(hour=QUIET_UNTIL, minute=0, second=0, microsecond=0)
    if local.hour >= QUIET_FROM:
        morning += timedelta(days=1)
    return morning.timestamp()


def request(end: Dict[str, Any], user: str) -> str:
    return (f"Follow-up check for {user}. Loose end: {end['what']}. What to look into: {end.get('check') or end['what']}. "
            "Look it up now with the tools you have. If there's something new or timely that " + user +
            " would want to hear about it (a price or availability change, a date getting close while it's still "
            "open, an answer that arrived), say it in one or two spoken sentences and offer the next step as a "
            f"question. If there's nothing worth interrupting them for, answer with exactly: {NOTHING}")


def make_check(agent, store, user: str) -> Callable[[Dict[str, Any]], str]:
    from .agent import Events

    def check(end: Dict[str, Any]) -> str:
        session = store.new_session(MODE, "ends")
        turn = agent.run(session_id=session, channel=MODE, content=request(end, user or "the user"), events=Events())
        text = turn.text.strip()
        return "" if not text or NOTHING in text.upper()[:40] else text
    return check


def due(task: Dict[str, Any], now: float, check: Optional[Callable[[Dict[str, Any]], str]],
        deliver: Callable[[str], None], tz: Optional[str]) -> Optional[float]:
    """Run one due loose end; returns when to look again (None: it's over)."""
    end = read(task["text"])
    if end is None or check is None or now > float(end.get("until") or 0):
        return None
    allowed = out_of_quiet_hours(now, tz)
    if allowed > now:
        return allowed
    said = check(end)
    if said:
        deliver(said)
    nxt = out_of_quiet_hours(now + max(1.0, float(end.get("every") or 3)) * 86400, tz)
    return nxt if nxt <= float(end["until"]) else None


DESCRIPTION = (
    "Keep track of a loose end: something the user left open that is worth coming back to (\"no hotel yet for New "
    "York\", \"waiting to hear back from the landlord\", \"decide on the car insurance before the 30th\"). Argus then "
    "looks into it by itself every few days and speaks up only when there's something worth hearing. action 'add' "
    "(what, and check: what to look up each time, self-contained, with dates and places), 'list', or 'done' (id) when "
    "it's settled or the user doesn't care any more. Add one without asking when a plan leaves something open, and "
    "say so in a few words (\"I'll keep an eye on hotel prices\").")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
    "action": {"type": "string", "enum": ["add", "list", "done"]},
    "what": {"type": "string", "description": "The loose end in a few words."},
    "check": {"type": "string", "description": "What to look up at each check, e.g. 'hotel prices in Midtown Manhattan for Dec 12-15, 2026'."},
    "first_in_days": {"type": "number", "minimum": 0.02, "maximum": 60, "description": "When to look first (default 2)."},
    "every_days": {"type": "number", "minimum": 1, "maximum": 30, "description": "How often after that (default 3)."},
    "for_days": {"type": "number", "minimum": 1, "maximum": MAX_DAYS, "description": "Give up after (default 30)."},
    "id": {"type": "integer"}}}


def attach(store, tools: Dict[str, Any], *, tz: Optional[str] = None, clock: Callable[[], float] = time.time) -> None:
    from .toolbox import Tool, ToolOutput

    def number(args, key, default, low, high) -> float:
        value = args.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return default
        return max(low, min(high, float(value)))

    def run(args: Dict[str, Any], ctx) -> ToolOutput:
        action = args.get("action")
        ends = open_ends(store)
        if action == "list":
            if not ends:
                return ToolOutput("No loose ends are being watched.")
            return ToolOutput("\n".join(
                f"#{i} {end['what']} (next look: {datetime.fromtimestamp(at, _zone(tz)):%a %d %b %H:%M})"
                for i, end, at in ends))
        if action == "done":
            if isinstance(args.get("id"), int) and any(i == args["id"] for i, _, _ in ends) \
                    and store.cancel_task(args["id"]):
                return ToolOutput("Closed.")
            return ToolOutput.error("No open loose end with that id; use action 'list'.")
        if action != "add":
            return ToolOutput.error("action must be add, list or done")
        what = " ".join(str(args.get("what") or "").split())[:200]
        if not what:
            return ToolOutput.error("say what is still open (what)")
        if any(end["what"].lower() == what.lower() for _, end, _ in ends):
            return ToolOutput("That loose end is already being watched.")
        if len(ends) >= MAX_OPEN:
            return ToolOutput.error(f"{MAX_OPEN} loose ends are open already; close one first (list, then done).")
        now = clock()
        end = {"what": what, "check": " ".join(str(args.get("check") or "").split())[:400],
               "every": number(args, "every_days", 3, 1, 30),
               "until": now + number(args, "for_days", 30, 1, MAX_DAYS) * 86400}
        first = out_of_quiet_hours(now + number(args, "first_in_days", 2, 0.02, 60) * 86400, tz)
        task_id = store.add_task(json.dumps(end), MODE, due=first, origin=getattr(ctx, "channel", "voice"))
        return ToolOutput(f"Watching it (#{task_id}); first look {datetime.fromtimestamp(first, _zone(tz)):%A %d %B}.")

    tools["follow_up"] = Tool("follow_up", DESCRIPTION, SCHEMA, run, frozenset({"voice", "phone", "cli", "android"}))
