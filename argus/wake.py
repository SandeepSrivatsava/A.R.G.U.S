"""Hearing Argus's name anywhere in a sentence, and the few voice commands handled on the Mac.

Everything here runs on text that never left the Mac. Speech that doesn't contain the name
(outside a follow-up window or conversation mode) is dropped before anything is sent anywhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Tuple

_WORD = re.compile(r"[a-z0-9']+")

# How speech recognisers commonly mishear "Argus". Kept short on purpose: every extra spelling is
# another way for a TV or a conversation in the room to wake Argus by accident.
DEFAULT_VARIANTS: Tuple[str, ...] = ("argos", "argas", "argis", "argous", "arguss", "argush")

# Real words the recogniser also produces for "Argus" (British "Argus" often comes out as "August").
# They only count when used like a name: "August, stop." / "Hey August what's new" / "thanks August",
# never "in August", "August is hot" or "August 5th".
DEFAULT_SOFT_VARIANTS: Tuple[str, ...] = ("august", "angus")

# Ordinary words and common names one letter away from "argus". Hearing these must not wake Argus.
NOT_THE_NAME = frozenset({"argue", "angus", "august", "agus", "args", "fergus", "argo", "argon", "arcus"})

_GREETINGS = frozenset({"hey", "hi", "hello", "ok", "okay", "yo", "oh", "thanks", "thank", "you", "so", "well"})
# Words that, right after a soft variant at the start, mean it's being used as an ordinary word.
_NOT_VOCATIVE_NEXT = frozenset({
    "is", "was", "will", "has", "had", "and", "or", "of", "in", "on", "at", "to", "the", "a", "this", "that",
    "'s", "s", "rush", "heat", "sun", "issue", "edition", "bank", "holiday", "break", "first", "second", "third",
    "fourth", "fifth", "tenth", "twentieth", "thirtieth",
})
# Words that, right before a soft variant at the end, mean it's a month ("in August", "last August").
_NOT_VOCATIVE_PREV = frozenset({
    "in", "of", "since", "until", "till", "by", "for", "from", "last", "next", "this", "early", "late", "mid",
    "during", "through", "throughout", "before", "after", "every", "each", "since", "the", "to", "around",
})

# Small words a recogniser may split the name into ("are gus", "r gus").
_SPLIT_PREFIXES = frozenset({"are", "ar", "r", "arr", "a", "our"})

_FILLER = frozenset({
    "hey", "hi", "hello", "ok", "okay", "please", "so", "um", "uh", "umm", "erm", "now", "just", "right",
    "yo", "oh", "well", "alright", "thanks", "thank", "you",
})

# Things Whisper-style recognisers "hear" in silence, music or noise.
_HALLUCINATIONS = frozenset({
    "", "you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye", "uh", "um",
    "hmm", "mm", "oh", "ah", "so", "okay", "the", "i", "a", "music", "applause", "laughter", "silence",
})


def words(text: str) -> List[str]:
    """Lower-case word tokens, keeping apostrophes (so "that's" stays one word)."""
    return _WORD.findall(text.lower().replace("’", "'"))


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def misheard_name(prompt: str, heard: str, name: str) -> Optional[str]:
    """When someone read `prompt` aloud and the recogniser wrote `heard`, the word it wrote for the name,
    if that word plausibly *is* the name misheard ("Markus" for "Argus"). Looks one word either side, since
    recognisers drop or merge little words, and never returns a word that merely happened to be there
    ("what's", "play"): learning one of those would make Argus answer ordinary speech."""
    name = name.lower()
    said, got = words(prompt), words(heard)
    if name not in said:
        return None
    at = said.index(name)
    best = None
    for word in got[max(0, at - 1):at + 2]:
        if word in NOT_THE_NAME or len(word) < 3 or word == name:
            continue
        distance = levenshtein(word, name)
        if distance <= max(2, len(name) // 2) and (best is None or distance < best[0]):
            best = (distance, word)
    return best[1] if best else None


def is_hallucination(text: str) -> bool:
    return " ".join(words(text)) in _HALLUCINATIONS


@dataclass(frozen=True)
class Heard:
    addressed: bool          # the name was said somewhere in the sentence
    rest: str                # the sentence without the name and filler words (lower case)


class NameMatcher:
    """Finds the assistant's name in a transcript, tolerating the usual mishearings."""

    def __init__(self, name: str = "argus", variants: Iterable[str] = DEFAULT_VARIANTS,
                 soft_variants: Iterable[str] = DEFAULT_SOFT_VARIANTS):
        self.name = name.strip().lower()
        if not re.fullmatch(r"[a-z]{3,20}", self.name):
            raise ValueError("The name must be a single word of 3-20 letters")
        self.variants = frozenset(v.strip().lower() for v in variants if v.strip()) | {self.name}
        self.soft_variants = frozenset(v.strip().lower() for v in soft_variants if v.strip()) - self.variants
        self._allow_one_typo = len(self.name) >= 5

    def _is_name(self, token: str) -> bool:
        if token in self.variants:
            return True
        if token in NOT_THE_NAME or token in self.soft_variants or not self._allow_one_typo:
            return False
        return len(token) >= len(self.name) - 1 and levenshtein(token, self.name) == 1

    @staticmethod
    def _vocative(tokens: Sequence[str], gaps: Sequence[str], i: int) -> bool:
        """Is tokens[i] used like a name? gaps[k] is the text between token k-1 and token k."""
        before = gaps[i]
        after = gaps[i + 1] if i + 1 < len(gaps) else ""
        punct_before = any(c in before for c in ",.!?;:")
        punct_after = any(c in after for c in ",.!?;:")
        at_start = all(t in _GREETINGS for t in tokens[:i])
        at_end = i == len(tokens) - 1
        if punct_before and (punct_after or at_end):
            return True
        if at_start and (at_end or punct_after):
            return True
        if at_start:
            nxt = tokens[i + 1]
            return nxt not in _NOT_VOCATIVE_NEXT and not nxt[0].isdigit()
        if at_end:
            return tokens[i - 1] not in _NOT_VOCATIVE_PREV
        return False

    def find(self, text: str) -> Tuple[List[str], List[Tuple[int, int]]]:
        """Tokens of `text` and the spans (start, end) of tokens that are the name."""
        text = text.lower().replace("\u2019", "'")
        tokens, gaps, last = [], [], 0
        for m in _WORD.finditer(text):
            gaps.append(text[last:m.start()])
            tokens.append(m.group().strip("'"))
            last = m.end()
        gaps.append(text[last:])
        spans: List[Tuple[int, int]] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i][:-2] if tokens[i].endswith("'s") else tokens[i]   # "Argus's" -> "argus"
            if self._is_name(tok) or (tok in self.soft_variants and self._vocative(tokens, gaps, i)):
                spans.append((i, i + 1))
                i += 1
                continue
            if i + 1 < len(tokens) and tok in _SPLIT_PREFIXES and tokens[i + 1].startswith(self.name[2:4]):
                joined = tok + tokens[i + 1]
                if joined in self.variants or levenshtein(joined, self.name) <= 1:
                    spans.append((i, i + 2))
                    i += 2
                    continue
            i += 1
        return tokens, spans

    def hear(self, text: str) -> Heard:
        tokens, spans = self.find(text)
        drop = {k for start, end in spans for k in range(start, end)}
        rest = [t for k, t in enumerate(tokens) if k not in drop and t]
        while rest and rest[0] in _FILLER:
            rest.pop(0)
        while rest and rest[-1] in _FILLER:
            rest.pop()
        return Heard(addressed=bool(spans), rest=" ".join(rest))


class Command(str, Enum):
    STOP = "stop"                          # stop talking / cancel the current task
    SLEEP = "sleep"                        # stop sending anything until "Argus, wake up"
    WAKE = "wake"
    MIC_OFF = "mic_off"                    # close the microphone completely (menu bar to reopen)
    CONVERSATION_ON = "conversation_on"    # no need to say the name for a while
    CONVERSATION_OFF = "conversation_off"
    NEW_SESSION = "new_session"            # forget the current conversation thread
    GUESTS_ON = "guests_on"                # answer other people too, for a while
    GUESTS_OFF = "guests_off"              # only the owner again


_COMMANDS: Tuple[Tuple[Command, re.Pattern], ...] = tuple((c, re.compile(p)) for c, p in (
    (Command.GUESTS_ON, r"(you can )?(talk|speak) to (everyone|everybody|anyone|my friends|my guests|them|others|"
                        r"other people)( too| as well)?|answer (everyone|everybody|anyone|my friends|my guests|them|"
                        r"others)( too| as well)?|guest mode( on)?"),
    (Command.GUESTS_OFF, r"(only|just) (me|listen to me|answer me|my voice)( from now on| now)?|only listen to me|"
                         r"only answer me|stop (talking to|answering) (everyone|everybody|them|others|guests|my friends)|"
                         r"guest mode off"),
    (Command.STOP, r"(stop|cancel|never ?mind|nevermind|quiet|be quiet|shut up|enough|that'?s enough|hold on|"
                   r"stop talking|stop it|cancel that|cancel it|forget it|stop that)( now)?"),
    (Command.SLEEP, r"(go to sleep|sleep|stop listening|pause listening|take a break|don'?t listen|"
                    r"go to sleep now|stop listening to me|pause|i'?m (on|taking) (a|the|my) (phone )?call|"
                    r"i'?m on the phone|(i'?ve got|i have|i got) a call)"),
    (Command.WAKE, r"(wake up|start listening|resume listening|resume|i'?m back|you can listen( now)?|"
                   r"i'?m (off|done with) the (phone|call)|the call('s| is) (over|done)|call('s| is) over)"),
    (Command.MIC_OFF, r"((turn|switch) off|disable|mute) (the |your )?(mic|microphone)|(mic|microphone) off|"
                      r"mute yourself"),
    (Command.CONVERSATION_ON, r"((turn on|start|enable) )?(conversation|chat|talk) mode( on)?|let'?s (talk|chat)|"
                              r"let'?s have a (conversation|chat)"),
    (Command.CONVERSATION_OFF, r"that'?s all|that is all|end (the |this )?conversation|(stop|end|exit|leave) "
                               r"(conversation|chat|talk) mode|(conversation|chat|talk) mode off|"
                               r"turn off (conversation|chat|talk) mode|we'?re done|good ?bye|bye|see you"),
    (Command.NEW_SESSION, r"(start a )?new (conversation|chat|topic)|start over|fresh start|"
                          r"forget (this|our|the) conversation|clear (the |our )?(conversation|chat)"),
))


def command_of(rest: str) -> Optional[Command]:
    """A local command if the whole (name-stripped) sentence is one, else None."""
    rest = " ".join(words(rest))
    if not rest or len(rest) > 60:
        return None
    for command, pattern in _COMMANDS:
        if pattern.fullmatch(rest):
            return command
    return None


def looks_like_echo(heard: str, spoken_recently: Iterable[str], threshold: float = 0.6) -> bool:
    """True when what the microphone heard is mostly Argus's own recent speech."""
    heard_tokens = [t for t in words(heard) if t not in _FILLER]
    if len(heard_tokens) < 3:
        return False
    for sentence in spoken_recently:
        spoken = set(words(sentence))
        if spoken and sum(t in spoken for t in heard_tokens) / len(heard_tokens) >= threshold:
            return True
    return False
