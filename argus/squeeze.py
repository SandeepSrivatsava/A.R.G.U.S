"""Squeezing bulky tool results before they reach Claude (the Headroom idea, rebuilt small for Argus).

Every step of a request sends the whole conversation again, so one 40,000-character web page or log dump is
paid for on every later step, and makes the reply slower to start. Before a long result goes in:

1. JSON is rewritten without spaces, and empty fields (null, "", [], {}) are dropped.
2. Runs of the same line are folded into one ("... (same line 37 more times)"), and long runs of blank
   lines or spaces are shortened.
3. If it's still long, the start and the end are kept and the middle is put aside, with a note saying how
   to read it: the `expand_result` tool hands back any part of the original. Nothing is thrown away
   ("reversible", like Headroom's CCR), but it stays on the Mac and only what's asked for is sent.

Kept results live in memory for this run of Argus (the newest 40); after a restart the tool is simply run again.
"""
from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

SQUEEZE_OVER = 12_000       # characters; shorter results go in as they are
KEEP_HEAD = 8_000
KEEP_TAIL = 2_000
PART = 8_000                # characters per expand_result part
KEEP_RESULTS = 40
NEVER = frozenset({"expand_result", "code_fix", "write_file"})   # exact text matters there

_BLANKS = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t]{4,}")


def _drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        out = {k: _drop_empty(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_drop_empty(v) for v in value]
    return value


def tidy(text: str) -> str:
    """Lossless-enough clean-up: compact JSON, folded repeats, shorter whitespace."""
    stripped = text.strip()
    if stripped[:1] in "[{":
        try:
            return json.dumps(_drop_empty(json.loads(stripped)), ensure_ascii=False, separators=(",", ":"))
        except ValueError:
            pass
    lines, out, last, repeats = text.split("\n"), [], None, 0
    for line in lines + [None]:
        if line is not None and line == last and line.strip():
            repeats += 1
            continue
        if repeats:
            out.append(f"... (same line {repeats} more time{'s' if repeats > 1 else ''})")
            repeats = 0
        if line is not None:
            out.append(line)
        last = line
    text = "\n".join(out)
    return _SPACES.sub("   ", _BLANKS.sub("\n\n", text))


class Squeezer:
    def __init__(self, over: int = SQUEEZE_OVER, head: int = KEEP_HEAD, tail: int = KEEP_TAIL,
                 part: int = PART, keep: int = KEEP_RESULTS):
        self.over, self.head, self.tail, self.part, self.keep = over, head, tail, part, keep
        self._kept: "OrderedDict[str, str]" = OrderedDict()
        self._n = 0
        self._lock = threading.Lock()
        self.saved_chars = 0          # for the daily report

    def squeeze(self, tool: str, text: Any) -> Any:
        """The result to send to Claude: unchanged unless it's long text."""
        if tool in NEVER or not isinstance(text, str) or len(text) <= self.over:
            return text
        tidied = tidy(text)
        if len(tidied) <= self.over:
            self.saved_chars += len(text) - len(tidied)
            return tidied
        with self._lock:
            self._n += 1
            key = f"r{self._n}"
            self._kept[key] = text                     # the original, not the tidied copy
            while len(self._kept) > self.keep:
                self._kept.popitem(last=False)
        parts = (len(text) + self.part - 1) // self.part
        cut = len(tidied) - self.head - self.tail
        self.saved_chars += cut
        note = (f"\n\n[... {cut:,} characters left out here to save time. The full result is kept on the Mac as "
                f"{key}: call expand_result with id {key} and part 1-{parts} (or with find, a word to look "
                f"for) if the answer may be in the part left out.]\n\n")
        return tidied[:self.head] + note + tidied[-self.tail:]

    def kept(self) -> int:
        with self._lock:
            return len(self._kept)

    def expand(self, key: str, part: int = 1, find: str = "") -> Tuple[Optional[str], str]:
        """(text, "") or (None, why not)."""
        with self._lock:
            text = self._kept.get(str(key or "").strip().lower())
        if text is None:
            return None, "That result isn't kept any more (Argus restarted or it's old); run the tool again."
        parts = max(1, (len(text) + self.part - 1) // self.part)
        if find:
            needle = find.strip().lower()
            if not needle:
                return None, "find needs a word or phrase."
            spots = [m.start() for m in re.finditer(re.escape(needle), text.lower())][:5]
            if not spots:
                return f"'{find}' doesn't appear in {key}.", ""
            out = []
            for at in spots:
                lo, hi = max(0, at - 600), min(len(text), at + 600)
                out.append(f"[around character {at:,}]\n{text[lo:hi]}")
            return "\n\n---\n\n".join(out), ""
        if not 1 <= part <= parts:
            return None, f"{key} has parts 1 to {parts}."
        chunk = text[(part - 1) * self.part: part * self.part]
        return f"{key}, part {part} of {parts}:\n{chunk}", ""


EXPAND_DESCRIPTION = ("Read part of a long tool result that was shortened (the note in it names an id like r3). "
                      "Give the part number, or `find` with a word to see the passages around it. Use it only "
                      "when the answer may be in the part left out.")
EXPAND_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {
    "id": {"type": "string"}, "part": {"type": "integer", "minimum": 1}, "find": {"type": "string"}}}


def attach(squeezer: Squeezer, tools: Dict[str, Any], channels) -> None:
    from .toolbox import Tool, ToolOutput

    def run(args: Dict[str, Any], ctx) -> "ToolOutput":
        part = args.get("part") if isinstance(args.get("part"), int) else 1
        text, why = squeezer.expand(str(args.get("id") or ""), part, str(args.get("find") or ""))
        return ToolOutput(text) if text is not None else ToolOutput.error(why)

    tools["expand_result"] = Tool("expand_result", EXPAND_DESCRIPTION, EXPAND_SCHEMA, run, channels)
