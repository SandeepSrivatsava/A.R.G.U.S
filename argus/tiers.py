"""Which Claude model answers a request.

Everyday questions (the time, the weather, a reminder, small talk) go to the light model: quickest and about a
twentieth of the price. Using the Mac, feelings and advice, and longer requests go to the main model. Coding,
analysis and long pieces of writing go to the heavy model.

The choice is made on the Mac from the words of the request, so it costs no time. When the light model sees a
request is beyond it, it calls `hand_over` and the request moves up (see agent.py).
"""
from __future__ import annotations

import re
from typing import Any, Tuple

LIGHT, MAIN, HEAVY = "light", "main", "heavy"
STICKY_SECONDS = 180.0       # a short reply within this time stays with the model that asked
SHORT_REPLY = 6              # words
ROUTED_CHANNELS = frozenset({"voice", "phone", "cli", "android"})     # the owner asking; everything else: main

_HEAVY = re.compile(
    r"\b(cod(e|es|ed|ing)|debug(ging)?|refactor\w*|compil\w+|algorithm\w*|regex\w*|sql|python|golang|javascript|"
    r"typescript|kubernetes|docker\w*|unit tests?|pull request|repositor(y|ies)|stack ?trace|source file|"
    r"(write|build|make|create|develop) (me )?(a|an|the) (\w+ )?(app|application|website|web ?site|program|script|"
    r"tool|game|bot|report|proposal|essay|article|spec|business plan|presentation)|"
    r"in[- ]depth|deep dive|research report|business plan|analy[sz]e|analysis|step[- ]by[- ]step plan|"
    r"think (hard|carefully|deeply)|long project|big project)\b", re.I)
_MAIN = re.compile(
    r"\b(open|click|type|press|scroll|screen|screenshot|window|browser|tab|file|folder|download|install|delete|"
    r"move|rename|send|e-?mail|mail|inbox|message|text|whatsapp|call|phone|book|reserve|order|buy|pay|"
    r"look(ing)? at|glasses|photo|picture|"
    r"mood|feel(s|ing)?|felt|sad|upset|stress(ed)?|anxious|worried|lonely|depress\w*|angry|tired of|"
    r"advice|should i|what do you think|opinion|decide|decision|explain|compare|summari[sz]e|translate|"
    r"flight|trip|plan|draft|write|going to|visit(ing)?|vacation|holiday|travel(l?ing)?|restaurants?|"
    r"recommend\w*|suggest\w*|where should|reviews?|best place|ideas?)\b", re.I)


def choose(content: Any, channel: str, settings) -> Tuple[str, str]:
    """(tier, model) for a request."""
    main = settings.model
    if getattr(settings, "routing", "auto") != "auto" or channel not in ROUTED_CHANNELS:
        return MAIN, main
    if not isinstance(content, str):                 # a photo or screenshot came with it
        return MAIN, main
    text = " ".join(content.split())
    if _HEAVY.search(text) and settings.heavy_model:
        return HEAVY, settings.heavy_model
    if len(text.split()) > 40 or _MAIN.search(text) or not settings.light_model:
        return MAIN, main
    return LIGHT, settings.light_model


def model_for(tier: str, settings) -> str:
    return {LIGHT: settings.light_model, HEAVY: settings.heavy_model}.get(tier) or settings.model


HAND_OVER = {
    "name": "hand_over",
    "description": "Pass this request to a more capable model. You are the quick model for everyday questions. "
                   "Call this alone, before saying anything, when the request needs the Mac's screen, careful "
                   "judgement or advice, several steps of work, or (level 'heavy') coding, analysis or a long "
                   "piece of writing. Don't use it for simple questions you can answer well.",
    "input_schema": {"type": "object", "properties": {"level": {"type": "string", "enum": ["main", "heavy"]}},
                     "required": ["level"], "additionalProperties": False},
}
BRIDGE = ("(A quicker model handled the steps above and passed the request on to you.)",
          "Please carry on with my request above.")
