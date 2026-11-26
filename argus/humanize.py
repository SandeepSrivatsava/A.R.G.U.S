"""A last clean-up of text that goes out under your name (a text message Argus sends for you), so it doesn't
read as machine-written. The humanizer skill tells Claude how to write; this catches what slips through.
It only removes stock phrases and punctuation habits; it never adds words or changes facts."""
from __future__ import annotations

import re

_DASH = re.compile(r"\s*[—–]\s*|\s+--\s+")
_OPENERS = re.compile(r"^(i hope (this|you'?re|you are)[^.!?\n]*[.!?]\s*|great question[.!]\s*|"
                      r"certainly[.!,]\s*|absolutely[.!,]\s*)", re.I)
_CLOSERS = re.compile(r"\s*(i hope this helps[.!]?|feel free to reach out[^.!?]*[.!]?|"
                      r"let me know if you have any (other |more )?questions[.!]?|"
                      r"please don'?t hesitate to [^.!?]*[.!]?)\s*$", re.I)
_SMART = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "…": "..."})


def humanize(text: str) -> str:
    out = str(text or "").translate(_SMART)
    out = _DASH.sub(", ", out)
    for _ in range(2):
        out = _OPENERS.sub("", out.strip())
        out = _CLOSERS.sub("", out.strip())
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"\s+([,.!?])", r"\1", out)
    out = out.strip()
    if out and out[0].islower() and str(text or "").strip()[:1].isupper():
        out = out[0].upper() + out[1:]
    return out or str(text or "").strip()
