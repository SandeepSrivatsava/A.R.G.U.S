"""The daily review: how Argus gets a little better every day, and tells you how.

Every morning Argus reads yesterday's activity log (what it was asked, what failed, how long answers took,
what it cost; never what you said) and compares it with the week before. It keeps the numbers, and tells
you only when there's something worth hearing:

* something kept failing (the same tool with the same error, again and again),
* answers got noticeably slower, or Claude was busy and another model had to answer,
* your voice wasn't recognised several times (time to run `argusctl enroll` again),
* and what it would fix, with the exact words to say ("Argus, fix the weather crash from yesterday").

It never changes its own code on its own: a fix starts only when you ask (code_fix), and a merge needs your
Touch ID. `argusctl today` (or `argusctl review 2026-11-24`) shows the same report any time.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .diagnose import read_entries

KEEP_DAYS = 60
TREND_DAYS = 7
REPEATED = 2              # the same failure this many times in a day is worth a look
SLOWER = 1.3              # first words 30% slower than the week before
_NUMBERS = re.compile(r"\d+")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_CRASH = re.compile(r"^\w+ failed: \w*(Error|Exception)\b|Traceback|\b(KeyError|TypeError|AttributeError|"
                    r"ValueError|IndexError|NameError)\b")


@dataclass
class Day:
    day: str
    requests: int = 0
    cost: float = 0.0
    first_words: Optional[float] = None          # median seconds to Claude's first words
    errors: int = 0                              # requests that ended in an error
    failures: Dict[str, int] = field(default_factory=dict)       # "tool: error" -> times
    slow: Dict[str, int] = field(default_factory=dict)           # tool -> times over 5 s
    fallbacks: int = 0
    offline: int = 0
    not_recognised: int = 0
    barge_ins: int = 0
    shortened: int = 0


def signature(tool: str, error: str) -> str:
    """The same failure with different numbers or names counts as one."""
    text = _QUOTED.sub("'…'", _NUMBERS.sub("N", " ".join(str(error).split())))
    return f"{tool or 'tool'}: {text[:90]}"


def review(entries: Iterable[Dict[str, Any]], day: str) -> Day:
    d = Day(day)
    firsts: List[float] = []
    for e in entries:
        if not str(e.get("ts", "")).startswith(day):
            continue
        kind = e.get("event")
        if kind == "turn":
            d.requests += 1
            d.cost += float(e.get("cost_usd") or 0)
            if e.get("error"):
                d.errors += 1
            first = ((e.get("timings") or {}).get("claude_first_s") or [None])[0]
            if isinstance(first, (int, float)):
                firsts.append(float(first))
        elif kind == "tool_failed":
            key = signature(str(e.get("tool") or ""), str(e.get("error") or ""))
            d.failures[key] = d.failures.get(key, 0) + 1
        elif kind == "tool_slow":
            tool = str(e.get("tool") or "tool")
            d.slow[tool] = d.slow.get(tool, 0) + 1
        elif kind == "model_fallback":
            d.fallbacks += 1
        elif kind == "llm_error" and "reach the Claude API" in str(e.get("error") or ""):
            d.offline += 1
        elif kind == "voice_not_recognised":
            d.not_recognised += 1
        elif kind == "barge_in":
            d.barge_ins += 1
        elif kind == "result_shortened":
            d.shortened += 1
    d.cost = round(d.cost, 4)
    d.first_words = round(statistics.median(firsts), 2) if firsts else None
    return d


def findings(today: Day, before: Sequence[Day] = ()) -> List[str]:
    """What's worth telling you, most useful first. Each ends with what to do, when there's something to do."""
    out: List[str] = []
    for key, times in sorted(today.failures.items(), key=lambda kv: -kv[1]):
        if times < REPEATED:
            continue
        tool, _, error = key.partition(": ")
        if "free requests" in error:
            out.append(f"{tool} ran out of its free requests ({times} times). Argus now skips it for the rest of "
                       f"that day and searches the web instead.")
        elif _CRASH.search(error):
            out.append(f"{tool} crashed {times} times ({error[:70]}). That's a bug in Argus: say \"Argus, fix the "
                       f"{tool} crash from yesterday\" and it'll look into it (you approve the change with Touch ID).")
        else:
            out.append(f"{tool} failed {times} times: {error[:80]}.")
    for tool, times in sorted(today.slow.items(), key=lambda kv: -kv[1]):
        if times >= 3:
            out.append(f"{tool} was slow {times} times (over 5 seconds each).")
    week = [d.first_words for d in before if d.first_words]
    if today.first_words and week:
        usual = statistics.mean(week)
        if today.first_words > usual * SLOWER and today.first_words - usual > 0.3:
            out.append(f"Answers started slower: {today.first_words:.1f} s to the first words (usually "
                       f"{usual:.1f} s).")
        elif today.first_words < usual / SLOWER and usual - today.first_words > 0.3:
            out.append(f"Answers started faster: {today.first_words:.1f} s to the first words (was {usual:.1f} s).")
    if today.fallbacks:
        out.append(f"Claude was busy {today.fallbacks} time{'s' if today.fallbacks > 1 else ''}, so another model "
                   f"answered instead.")
    if today.not_recognised >= 3:
        out.append(f"Your voice wasn't recognised {today.not_recognised} times. If that's new (a cold, a new mic), "
                   f"run argusctl enroll to refresh it.")
    if today.offline >= 3:
        out.append(f"The internet dropped {today.offline} times while you were asking things.")
    if today.requests and today.errors / today.requests > 0.25 and today.errors >= 3:
        out.append(f"{today.errors} of {today.requests} requests ended in an error; argusctl errors shows them.")
    return out


def summary(today: Day, before: Sequence[Day] = ()) -> str:
    """The morning note: empty when there's nothing worth saying."""
    found = findings(today, before)
    if not found:
        return ""
    head = f"Yesterday's review: {today.requests} request{'s' if today.requests != 1 else ''}, about ${today.cost:.2f}."
    return " ".join([head] + found)


def report(today: Day, before: Sequence[Day] = ()) -> str:
    """The long form, for argusctl today."""
    lines = [f"{today.day}: {today.requests} requests, about ${today.cost:.2f}"]
    if today.first_words is not None:
        lines.append(f"First words after {today.first_words:.2f} s (median)")
    week = [d for d in before if d.requests]
    if week:
        lines.append(f"The {len(week)} days before: {statistics.mean(d.requests for d in week):.0f} requests a day, "
                     f"about ${statistics.mean(d.cost for d in week):.2f} a day")
    if today.shortened:
        lines.append(f"{today.shortened} long results were shortened before going to Claude")
    found = findings(today, before)
    lines += ["", "Worth knowing:"] + [f"- {f}" for f in found] if found else ["", "Nothing went wrong more than once."]
    return "\n".join(lines)


class Reviewer:
    """Keeps a short history of daily numbers in Argus's database, for the trend."""

    def __init__(self, store, audit_path: Path):
        self.store, self.audit_path = store, Path(audit_path)

    def _entries(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for path in (self.audit_path.with_name(self.audit_path.name + ".1"), self.audit_path):
            entries += read_entries(path, last=200_000)
        return entries

    def saved(self, day: str) -> Optional[Day]:
        raw = self.store.get(f"daily:{day}")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return Day(**{k: v for k, v in data.items() if k in Day.__dataclass_fields__})
        except (ValueError, TypeError):
            return None

    def history(self, day: str, days: int = TREND_DAYS) -> List[Day]:
        start = date.fromisoformat(day)
        out = []
        for back in range(1, days + 1):
            got = self.saved((start - timedelta(days=back)).isoformat())
            if got is not None:
                out.append(got)
        return out

    def run(self, day: str, entries: Optional[List[Dict[str, Any]]] = None) -> Day:
        d = review(self._entries() if entries is None else entries, day)
        self.store.set(f"daily:{day}", json.dumps(asdict(d)))
        old = (date.fromisoformat(day) - timedelta(days=KEEP_DAYS)).isoformat()
        self.store.set(f"daily:{old}", "")
        return d

    def morning(self, today: date) -> str:
        """Review yesterday and return the note to deliver ("" = nothing worth saying)."""
        day = (today - timedelta(days=1)).isoformat()
        return summary(self.run(day), self.history(day))
