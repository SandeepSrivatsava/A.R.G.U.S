"""Remembering what was said: search across past conversations, saved facts and lessons.

"What did I say about the hotel last week?", "which restaurant did you suggest on Friday?": Argus only keeps
the last half hour of a conversation in mind, so for anything older it searches. Ranking is BM25 (the
standard way search engines score how well a passage matches a query), with light stemming ("booked",
"booking" and "book" match) and a small boost for recent passages. Everything stays on the Mac: the search
runs over Argus's own database.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

STOP = frozenset("""a an the and or but so if of to in on at by for with from about as is are was were be been am do
does did have has had i me my you your it its we our they them their this that these those what which who
when where why how can could would should will just also not no yes ok okay please argus""".split())

K1, B = 1.4, 0.75


def stem(word: str) -> str:
    for suffix in ("ingly", "edly", "ings", "ing", "ied", "ies", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)] + ("y" if suffix in ("ied", "ies") else "")
            break
    return word


def terms(text: str) -> List[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9']+", str(text).lower()) if w not in STOP and len(w) > 1]


def text_of(content: Any) -> str:
    """The words in a stored message (plain text, or Claude's content blocks), without tool plumbing."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_result":
            inner = block.get("content")
            parts.append(text_of(inner)[:600] if inner is not None else "")
    return " ".join(p for p in parts if p)


class Index:
    """BM25 over a list of passages."""

    def __init__(self, passages: Sequence[Tuple[str, Dict[str, Any]]]):
        self.passages = list(passages)
        self.docs = [Counter(terms(text)) for text, _ in self.passages]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, limit: int = 8, recency: Optional[Callable[[Dict[str, Any]], float]] = None
               ) -> List[Tuple[float, str, Dict[str, Any]]]:
        q = terms(query)
        if not q or not self.docs:
            return []
        scored = []
        for (text, meta), doc, length in zip(self.passages, self.docs, self.lengths):
            score = 0.0
            for t in set(q):
                f = doc.get(t, 0)
                if f:
                    score += self.idf.get(t, 0.0) * f * (K1 + 1) / (f + K1 * (1 - B + B * length / (self.avg or 1)))
            if score > 0:
                if recency is not None:
                    score *= recency(meta)
                scored.append((score, text, meta))
        scored.sort(key=lambda item: -item[0])
        return scored[:limit]


def passages(store, now: Optional[float] = None, days: float = 30) -> List[Tuple[str, Dict[str, Any]]]:
    now = now or time.time()
    out: List[Tuple[str, Dict[str, Any]]] = []
    for created, channel, role, content in store.all_messages(since=now - days * 86400):
        text = " ".join(text_of(content).split())
        if not text or text.startswith("(") and len(text) < 40:
            continue
        out.append((text[:900], {"kind": "you said" if role == "user" else "Argus said", "at": created,
                                 "channel": channel}))
    for _, fact in store.facts(limit=500):
        out.append((fact, {"kind": "saved fact", "at": None}))
    for _, lesson, _source in store.lessons(200):
        out.append((lesson, {"kind": "lesson", "at": None}))
    return out


def search(store, query: str, *, limit: int = 8, now: Optional[float] = None, days: float = 30) -> str:
    now = now or time.time()
    index = Index(passages(store, now, days))

    def recency(meta: Dict[str, Any]) -> float:
        at = meta.get("at")
        if at is None:
            return 1.0
        age_days = max(0.0, (now - at) / 86400)
        return 1.0 + 0.5 * math.exp(-age_days / 7)      # a little boost for the last week or so

    hits = index.search(query, limit=limit, recency=recency)
    if not hits:
        return "Nothing found about that in the last {:.0f} days of conversations, saved facts or lessons.".format(days)
    lines = []
    for _, text, meta in hits:
        when = datetime.fromtimestamp(meta["at"]).strftime("%a %d %b %H:%M") if meta.get("at") else ""
        label = f"[{meta['kind']}{', ' + when if when else ''}]"
        lines.append(f"{label} {text[:400]}")
    return "\n".join(lines)


DESCRIPTION = ("Search what was said before: past conversations (the last 30 days, every channel), facts the user "
               "asked you to remember, and lessons. Use it when the user refers to something earlier that isn't in "
               "this conversation (\"what did I say about the hotel\", \"the restaurant you suggested on Friday\", "
               "\"did I already ask you about this\"). query: the key words; try other words if nothing comes up.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
    "query": {"type": "string"}, "days": {"type": "integer", "minimum": 1, "maximum": 90}}}


def attach(store, tools: Dict[str, Any], clock: Callable[[], float] = time.time) -> None:
    from .toolbox import Tool, ToolOutput

    def run(args: Dict[str, Any], ctx) -> ToolOutput:
        query = " ".join(str(args.get("query") or "").split())
        if not query:
            return ToolOutput.error("say what to look for (query)")
        days = args.get("days") if isinstance(args.get("days"), int) else 30
        return ToolOutput(search(store, query, now=clock(), days=max(1, min(90, days))))

    tools["recall"] = Tool("recall", DESCRIPTION, SCHEMA, run, frozenset({"voice", "phone", "cli", "android"}))
