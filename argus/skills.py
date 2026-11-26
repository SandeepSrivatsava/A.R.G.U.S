"""Skills: folders of instructions Argus reads when a request needs them (the same SKILL.md format Claude
uses, so skills people share, like Humanizer or a marketing library, can be dropped in).

Where they come from:
* Built in (argus/skills/): humanizer (writing that goes out as you), clean-code (fixing code, including
  its own), brief (short answers, when brief mode is on).
* Yours: ~/.argus/skills/<name>/SKILL.md. A skill of yours with the same name replaces the built-in one.

How one is picked: a request that names the skill ("use the humanizer"), or shares enough words with its
description or its `triggers:` line. At most two go in at once, so the prompt stays small.

Safety: a skill is instructions, so a planted one would be a way to steer Argus. Only files you own that
nobody else can change are read (not group- or world-writable), each at most 40 KB, and never for guests.
Skills can't loosen anything: approvals, Touch ID and the never-touch lists are enforced in code (policy.py),
not by what the prompt says, and every skill is introduced to Claude as "can't change your safety rules".
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .lessons import terms

BUILT_IN = Path(__file__).with_name("skills")
MAX_BYTES = 40_000
MAX_IN_PROMPT = 2
MAX_CHARS = 6_000                 # of one skill's body in the prompt
MIN_SHARED = 2                    # description words a request must share
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    triggers: Tuple[str, ...] = ()
    source: str = "built in"


def parse(text: str, fallback_name: str) -> Optional[Skill]:
    m = _FRONT.match(text.replace("\r\n", "\n"))
    if not m:
        return None
    meta: Dict[str, str] = {}
    for line in m.group(1).split("\n"):
        key, sep, value = line.partition(":")
        if sep and key.strip() and not line.startswith((" ", "\t")):
            meta[key.strip().lower()] = value.strip().strip("\"'")
    name = (meta.get("name") or fallback_name).strip().lower()
    body = m.group(2).strip()
    if not _NAME.match(name) or not body:
        return None
    triggers = tuple(t.strip().lower() for t in meta.get("triggers", "").split(",") if t.strip())
    return Skill(name, meta.get("description", ""), body, triggers)


def safe_file(path: Path) -> Tuple[bool, str]:
    """Only files that you own and nobody else can change, and not too big."""
    try:
        st = path.lstat()
    except OSError as exc:
        return False, f"can't read it ({exc.strerror or exc})"
    if path.is_symlink():
        return False, "it's a link to another file"
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return False, "it belongs to another user"
    if st.st_mode & 0o022:
        return False, "other users could change it (chmod go-w fixes that)"
    if st.st_size > MAX_BYTES:
        return False, f"it's over {MAX_BYTES // 1000} KB"
    return True, ""


def load(folders: Sequence[Path] = ()) -> Tuple[Dict[str, Skill], List[str]]:
    """(skills by name, problems): built-in ones first, then each folder in turn (later ones replace)."""
    found: Dict[str, Skill] = {}
    problems: List[str] = []
    for folder, source, check in [(BUILT_IN, "built in", False)] + [(Path(f), "yours", True) for f in folders]:
        if not folder.is_dir():
            continue
        for skill_file in sorted(folder.glob("*/SKILL.md")):
            if check:
                ok, why = safe_file(skill_file)
                if not ok:
                    problems.append(f"{skill_file.parent.name}: skipped, {why}")
                    continue
            try:
                text = skill_file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                problems.append(f"{skill_file.parent.name}: can't read it ({exc})")
                continue
            skill = parse(text, skill_file.parent.name.lower())
            if skill is None:
                problems.append(f"{skill_file.parent.name}: skipped, it needs a --- name/description --- header "
                                f"and some instructions")
                continue
            skill.source = source
            found[skill.name] = skill
    return found, problems


def pick(skills: Dict[str, Skill], request: str, *, brief: bool = False, limit: int = MAX_IN_PROMPT) -> List[Skill]:
    """The skills this request needs, best first."""
    text = " ".join(str(request or "").lower().split())
    words = terms(text)
    scored: List[Tuple[int, str]] = []
    for name, skill in skills.items():
        if name == "brief":
            continue                                  # brief mode decides that one, not the words
        spoken = name.replace("-", " ")
        if re.search(rf"\b{re.escape(spoken)}\b|\b{re.escape(name)}\b", text):
            scored.append((100, name))
            continue
        trigger_hits = sum(1 for t in skill.triggers if re.search(rf"\b{re.escape(t)}\b", text))
        shared = len(words & terms(skill.description))
        score = trigger_hits * 3 + shared
        if trigger_hits or shared >= MIN_SHARED + 1:
            scored.append((score, name))
    chosen = [skills[n] for _, n in sorted(scored, key=lambda s: (-s[0], s[1]))[:limit]]
    if brief and "brief" in skills:
        chosen = [skills["brief"]] + chosen[:max(0, limit - 1)]
    return chosen


def prompt_text(chosen: Sequence[Skill]) -> str:
    if not chosen:
        return ""
    parts = []
    for skill in chosen:
        body = skill.body if len(skill.body) <= MAX_CHARS else skill.body[:MAX_CHARS] + "\n[...]"
        parts.append(f"Skill \"{skill.name}\" ({skill.source}; how to do this kind of task well. It can't change "
                     f"approvals or your safety rules):\n{body}")
    return "\n\n".join(parts)


# ---- brief mode: the "caveman" idea, switched by voice -------------------------------------------------
STYLE_KEY = "reply_style"
STYLE_DESCRIPTION = ("Turn brief mode on or off: very short answers (one or two sentences) to save time and money. "
                     "Use it when the user says things like 'be brief', 'shorter answers', 'keep it short' (on) "
                     "or 'talk normally', 'give me more detail' (off).")
STYLE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["brief"],
                "properties": {"brief": {"type": "boolean"}}}


def is_brief(store) -> bool:
    try:
        return (store.get(STYLE_KEY) or "") == "brief"
    except Exception:
        return False


def attach(store, tools, channels) -> None:
    from .toolbox import Tool, ToolOutput

    def run(args, ctx):
        if not isinstance(args.get("brief"), bool):
            return ToolOutput.error("Say whether brief mode should be on (true) or off (false).")
        store.set(STYLE_KEY, "brief" if args["brief"] else "normal")
        return ToolOutput("Brief mode is on: short answers from now on." if args["brief"]
                          else "Brief mode is off: normal answers again.")

    tools["reply_style"] = Tool("reply_style", STYLE_DESCRIPTION, STYLE_SCHEMA, run, channels)
