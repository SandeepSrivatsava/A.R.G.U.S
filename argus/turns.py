"""Knowing when you've finished talking (end of turn).

A pause alone is a poor signal: "book a table for... um... four" has pauses in the middle, and waiting longer
after every sentence makes every answer slower. So Argus listens to what you said, too, the way recent voice
agents do ("semantic end-of-turn"): after the usual short pause it looks at the words. If the sentence is
clearly unfinished (it ends on "and", "the", "to", "um", "because", a comma...), it waits a little longer for
the rest and joins the pieces into one request. A finished sentence goes straight through, so answers aren't
slowed down.
"""
from __future__ import annotations

import re

# A sentence that ends on one of these words isn't finished.
TRAILING = frozenset("""
and or but so because cause cuz the a an to of for with without in on at from by into onto about like than then
my your his her their our its this that these those which who whom whose what when where whether if as
is are was were be been am will would could should can can't don't do does did have has had
um uh er erm hmm uhm mm well also plus maybe i i'm we we're you you're it's there's let's please
""".split())

HOLD_SECONDS = 1.2          # how much longer to wait for the rest of an unfinished sentence
MAX_HELD_SECONDS = 25.0     # never hold more speech than this


def unfinished(text: str) -> bool:
    """Whether `text` sounds like you stopped mid-sentence."""
    text = str(text or "").strip()
    if not text:
        return False
    if re.search(r"(,|\.\.\.|…|-|—)\s*$", text):
        return True
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return False
    if re.search(r"[.?!]\s*$", text) and words[-1] not in ("um", "uh", "er", "erm", "hmm", "uhm", "mm"):
        return False                       # the speech model ended it with a full stop: trust that
    return words[-1] in TRAILING
