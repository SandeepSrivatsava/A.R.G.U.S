"""Getting names right in what speech recognition heard.

Small on-device speech models garble names: people, places, businesses, films ("Jack in the Box" heard as
"jacket in the box", "Dune" as "June"). Argus knows the names that matter to you: what you asked it to
remember, places it looked up, names it said itself lately, and any you add with `argusctl names add`. Each
heard phrase of one to four words is compared to those names by how it sounds (a phonetic key, in the spirit
of Metaphone) and by spelling (Jaro-Winkler), and replaced only when it's a clearly close match. A name a
phrase could equally be, or a phrase made of everyday words that already makes sense, is left alone. The
known names also go to Claude, which fixes what's left from context.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Everyday words: a phrase made only of these is never "corrected" into a name.
COMMON = frozenset("""
a an the and or but so if then than that this these those there here where when what which who whom whose why
how is are was were be been being am do does did done have has had having i me my mine you your yours he him his
she her hers it its we us our they them their to of in on at by for with about from into over under after before
up down out off again once just only also too very can could will would shall should may might must not no yes
yeah ok okay please thanks thank time today tomorrow tonight morning evening night day week month year now later
call text send tell ask find show play stop open close set get go come take make give look see hear say know
think want need like love good great fine nice new old big small more most less some any all every each other
one two three four five six seven eight nine ten first last next best near far home work way thing things
""".split())

MAX_WORDS = 4
MIN_SCORE = 0.86


def phonetic(text: str) -> str:
    """A rough sound-key for English words: similar-sounding spellings get the same key."""
    s = re.sub(r"[^a-z]", "", str(text).lower())
    if not s:
        return ""
    for a, b in (("^kn", "n"), ("^gn", "n"), ("^pn", "n"), ("^wr", "r"), ("^ps", "s"), ("^x", "s"), ("^wh", "w"),
                 ("mb$", "m"), ("ph", "f"), ("gh(?=[^aeiou]|$)", ""), ("dge", "j"), ("dg", "j"), ("tch", "ch"),
                 ("sch", "sk"), ("ck", "k"), ("c(?=[eiy])", "s"), ("c", "k"), ("q", "k"), ("x", "ks"), ("z", "s"),
                 ("th", "0"), ("sh", "x"), ("ch", "x"), ("tio", "xo"), ("tia", "xa"), ("v", "f"), ("y(?=[^aeiou]|$)", ""),
                 ("w(?=[^aeiou]|$)", "")):
        s = re.sub(a, b, s)
    if not s:
        return ""
    first, rest = ("a" if s[0] in "aeiouy" else s[0]), re.sub(r"[aeiouyh]", "", s[1:])
    key = first + rest
    return re.sub(r"(.)\1+", r"\1", key)


def jaro_winkler(a: str, b: str) -> float:
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    reach = max(len(a), len(b)) // 2 - 1
    used = [False] * len(b)
    matches_a = []
    for i, ch in enumerate(a):
        for j in range(max(0, i - reach), min(len(b), i + reach + 1)):
            if not used[j] and b[j] == ch:
                used[j] = True
                matches_a.append(ch)
                break
    if not matches_a:
        return 0.0
    matches_b = [b[j] for j in range(len(b)) if used[j]]
    m = len(matches_a)
    transpositions = sum(x != y for x, y in zip(matches_a, matches_b)) / 2
    jaro = (m / len(a) + m / len(b) + (m - transpositions) / m) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def _ratio(a: str, b: str) -> float:
    """Similarity of two keys, 0..1, from their edit distance."""
    if not a and not b:
        return 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1 - prev[-1] / max(len(a), len(b))


def similarity(heard: str, name: str) -> float:
    """How likely `heard` is a mishearing of `name`: mostly by sound, partly by spelling."""
    sound = _ratio(phonetic(heard.replace(" ", "")), phonetic(name.replace(" ", "")))
    spelling = jaro_winkler(heard.replace(" ", ""), name.replace(" ", ""))
    return 0.65 * sound + 0.35 * spelling


_PROPER = re.compile(r"\b[A-Z][\w'&-]*(?:\s+(?:(?:in|of|the|de|la|del|and|&)\s+)*[A-Z][\w'&-]*){0,4}")
_NOT_NAMES = frozenset("i i'm i'll i'd i've a the it its this that there here he she we they you your my our "
                       "am pm ok okay yes no".split())


def names_in(text: str) -> List[str]:
    """Capitalised names in a piece of text ("Jack in the Box", "Dune Part Three", "Stevenson Blvd")."""
    found = []
    for match in _PROPER.finditer(str(text)):
        words = match.group(0).split()
        while words and (words[0].lower() in _NOT_NAMES | {"in", "of", "and", "&", "the"}
                         or len(words) > 1 and words[0].lower() in COMMON):
            words = words[1:]                  # "The" at the start of a sentence isn't part of a name
        if not words:
            continue
        name = " ".join(words)
        if len(words) == 1 and (words[0].lower() in COMMON or words[0].lower() in _NOT_NAMES or len(name) < 3
                                or name.isupper() and len(name) < 4):
            continue
        found.append(name)
    return found


class KnownNames:
    """Names Argus has reason to expect: fixed ones (its name, yours, your city, ones you added), names in
    what you asked it to remember, and the most recent ones it came across (places it looked up, names it said)."""

    def __init__(self, fixed: Iterable[str] = (), limit: int = 300, clock=time.monotonic,
                 sources: Sequence = ()):
        self._fixed = [n for n in (" ".join(str(f).split()) for f in fixed) if n]
        self.sources = list(sources)          # callables returning more names (facts, names you added)
        self._recent: "OrderedDict[str, float]" = OrderedDict()
        self.limit, self.clock = limit, clock
        self._lock = threading.Lock()

    def add(self, names: Iterable[str]) -> None:
        with self._lock:
            for name in names:
                name = " ".join(str(name).split())
                if len(name) < 3 or len(name) > 60:
                    continue
                self._recent.pop(name.lower(), None)
                self._recent[name.lower()] = self.clock()
                self._display = getattr(self, "_display", {})
                self._display[name.lower()] = name
            while len(self._recent) > self.limit:
                self._recent.popitem(last=False)

    def notice(self, text: str) -> None:
        """Pick the names out of something Argus said or read."""
        self.add(names_in(text))

    def all(self) -> List[str]:
        with self._lock:
            display = getattr(self, "_display", {})
            recent = [display.get(k, k) for k in reversed(self._recent)]
        extra: List[str] = []
        for source in self.sources:
            try:
                extra.extend(str(n) for n in source())
            except Exception:
                pass
        seen, out = set(), []
        for name in self._fixed + extra + recent:
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
        return out


def correct(text: str, names: Sequence[str], min_score: float = MIN_SCORE) -> Tuple[str, List[Tuple[str, str]]]:
    """`text` with likely-misheard names put right, and what was changed ([(heard, name)])."""
    words = str(text).split()
    if not words or not names:
        return text, []
    known = [(n, len(n.split())) for n in names if len(n.replace(" ", "")) >= 4]
    lower_names = {n.lower() for n, _ in known}
    out: List[str] = []
    changes: List[Tuple[str, str]] = []
    i = 0
    while i < len(words):
        best: Optional[Tuple[float, int, str]] = None
        for size in range(min(MAX_WORDS, len(words) - i), 0, -1):
            chunk = words[i:i + size]
            bare = [re.sub(r"[^\w'&-]", "", w) for w in chunk]
            phrase = " ".join(b for b in bare if b)
            if not phrase or phrase.lower() in lower_names:
                if phrase and phrase.lower() in lower_names:
                    best = None
                    break
                continue
            for name, n_words in known:
                if abs(n_words - size) > 1:
                    continue
                score = similarity(phrase, name)
                if score < min_score:
                    continue
                if all(b.lower() in COMMON for b in bare) and score < 0.95:
                    continue                 # everyday words that already make sense: leave them
                if best is None or score > best[0] + 1e-9:
                    best = (score, size, name)
        if best is not None:
            rivals = [n for n, _ in known if n != best[2]
                      and similarity(" ".join(words[i:i + best[1]]), n) >= best[0] - 0.02]
            if not rivals:                    # it could just as well be another name: leave it
                trail = re.sub(r"^.*?([.,!?;:]*)$", r"\1", words[i + best[1] - 1])
                changes.append((" ".join(words[i:i + best[1]]), best[2]))
                out.append(best[2] + trail)
                i += best[1]
                continue
        out.append(words[i])
        i += 1
    return " ".join(out), changes


def prompt_line(names: Sequence[str], limit: int = 60) -> str:
    """For Claude: the names speech recognition is likely to garble."""
    names = list(names)[:limit]
    if not names:
        return ""
    return ("Names you know (speech recognition may garble these; when a word sounds like one of them and fits, "
            "that's what was meant): " + "; ".join(names) + ".")


# ---- names you add yourself (argusctl names) ----------------------------------------------------------------
KV_KEY = "known_names"


def added(store) -> List[str]:
    import json
    try:
        value = json.loads(store.get(KV_KEY) or "[]")
    except ValueError:
        return []
    return [str(n) for n in value if isinstance(n, str)]


def add_name(store, name: str) -> bool:
    import json
    name = " ".join(str(name).split())[:60]
    current = added(store)
    if not name or name.lower() in {n.lower() for n in current}:
        return False
    store.set(KV_KEY, json.dumps(current + [name]))
    return True


def remove_name(store, name: str) -> bool:
    import json
    current = added(store)
    keep = [n for n in current if n.lower() != " ".join(str(name).split()).lower()]
    store.set(KV_KEY, json.dumps(keep))
    return len(keep) != len(current)
