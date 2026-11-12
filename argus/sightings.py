"""What Argus has seen through the glasses: "where did I put my keys?"

Whenever Argus looks through the glasses, it notes the everyday things in view and where they are ("keys: on
the kitchen counter by the fruit bowl"). Only those few words are kept (14 days, on this Mac); the photo
itself is never stored. Later, find_seen answers where something was last seen.

Argus only sees when it looks. So there are two ways it gets to know where things are:
* each time you ask it to look at something, and
* "glances" (off unless you switch them on: `[glasses] glances_minutes = 3`): while the Argus Eye app is
  open and the glasses are on, the app sends a photo every few minutes; the quick model notes what's in
  view and the photo is dropped. Each glance sends a photo of what's in front of you to Claude, which is
  why it's your choice. "Argus, stop watching" / "start watching again" pauses and resumes them.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

KEEP_DAYS = 14
MAX_ROWS = 2000
CHANNEL = "glance"
GLANCE_PROMPT = ("A glance through the user's glasses, to remember where their things are. If you can see everyday "
                 "belongings someone might look for later (keys, wallet, phone, glasses case, bag, headphones, "
                 "remote, charger, passport, bottle, jacket), call note_seen once with each thing and exactly where "
                 "it is. Never note people, screens, documents or anything written. Then answer with just: ok")
_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> List[str]:
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _WORD.findall(text.lower())]


def add(store, things: List[Tuple[str, str]], now: Optional[float] = None) -> int:
    now = now or time.time()
    kept = 0
    for thing, where in things[:12]:
        thing, where = " ".join(str(thing).split())[:60], " ".join(str(where).split())[:160]
        if thing and where:
            store.add_sighting(thing, where, now)
            kept += 1
    store.prune_sightings(now - KEEP_DAYS * 86400, MAX_ROWS)
    return kept


def find(store, query: str, limit: int = 4) -> List[Tuple[float, str, str]]:
    wanted = set(_words(query)) - {"my", "the", "a", "did", "i", "put", "where", "is", "are"}
    if not wanted:
        return []
    hits = [(at, thing, where) for at, thing, where in store.sightings() if wanted & set(_words(thing))]
    return hits[:limit]


def ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} hours ago"
    return f"{round(seconds / 86400)} days ago"


NOTE_DESC = ("After looking through the glasses, quietly note everyday belongings in view and exactly where they are, "
             "so you can answer 'where did I put my ...' later. Things only: never people, screens or documents.")
NOTE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["things"], "properties": {
    "things": {"type": "array", "maxItems": 12, "items": {
        "type": "object", "additionalProperties": False, "required": ["thing", "where"],
        "properties": {"thing": {"type": "string"}, "where": {"type": "string"}}}}}}
FIND_DESC = ("Where something was last seen through the glasses (\"where did I put my keys?\"). Only knows what the "
             "glasses saw in the last two weeks; if nothing is found, say so and offer to take a look now.")
FIND_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["thing"],
               "properties": {"thing": {"type": "string"}}}


def attach(store, tools: Dict[str, Any], *, clock: Callable[[], float] = time.time,
           glancer: Optional["Glancer"] = None) -> None:
    from .toolbox import Tool, ToolOutput

    if glancer is not None:
        def glances(args: Dict[str, Any], ctx) -> ToolOutput:
            if args.get("action") in ("pause", "resume"):
                glancer.paused = args["action"] == "pause"
            return ToolOutput("Glances are paused." if glancer.paused else
                              f"Glancing about every {round(glancer.every / 60)} minutes while the glasses are on.")
        tools["glances"] = Tool(
            "glances", "Pause or resume the glasses' automatic glances (\"stop watching\", \"start watching again\"), "
            "or check whether they're on.",
            {"type": "object", "additionalProperties": False, "required": ["action"],
             "properties": {"action": {"type": "string", "enum": ["pause", "resume", "status"]}}},
            glances, frozenset({"voice", "phone"}))

    def note(args: Dict[str, Any], ctx) -> ToolOutput:
        things = [(t.get("thing", ""), t.get("where", "")) for t in args.get("things") or [] if isinstance(t, dict)]
        return ToolOutput(f"Noted {add(store, things, clock())}.")

    def look_up(args: Dict[str, Any], ctx) -> ToolOutput:
        hits = find(store, str(args.get("thing") or ""))
        if not hits:
            return ToolOutput("The glasses haven't seen that in the last two weeks.")
        now = clock()
        return ToolOutput("Last seen through the glasses (newest first):\n" +
                          "\n".join(f"- {thing}: {where} ({ago(now - at)})" for at, thing, where in hits))

    tools["note_seen"] = Tool("note_seen", NOTE_DESC, NOTE_SCHEMA, note, frozenset({"voice", "phone", CHANNEL}))
    tools["find_seen"] = Tool("find_seen", FIND_DESC, FIND_SCHEMA, look_up, frozenset({"voice", "phone", "cli"}))


class Glancer:
    """Takes the photos the iPhone app sends by itself, one at a time and not too often."""

    def __init__(self, agent, store, audit, *, minutes: float, clock: Callable[[], float] = time.monotonic):
        self.agent, self.store, self.audit, self.clock = agent, store, audit, clock
        self.every = max(1.0, float(minutes)) * 60
        self.paused = False
        self._last = -1e12
        self._lock = threading.Lock()

    @property
    def interval(self) -> int:
        """Seconds between glances the app should send (0: none now)."""
        return 0 if self.paused else int(self.every)

    def glance(self, jpeg_b64: str) -> bool:
        if self.paused or self.clock() - self._last < self.every * 0.8 or not self._lock.acquire(blocking=False):
            return False
        try:
            from .agent import Events
            self._last = self.clock()
            session = self.store.new_session(CHANNEL, "glasses")
            content = [{"type": "text", "text": GLANCE_PROMPT},
                       {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpeg_b64}}]
            try:
                turn = self.agent.run(session_id=session, channel=CHANNEL, content=content, events=Events())
            finally:
                self.store.replace_history(session, [])            # the photo isn't kept
            self.audit.log("glance", noted=sum(1 for s in turn.steps if "note_seen" in str(s)), error=turn.error)
            return True
        finally:
            self._lock.release()
