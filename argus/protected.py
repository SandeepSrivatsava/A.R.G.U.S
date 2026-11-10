"""Places on the Mac that hold passwords, keys and login cookies. Argus never reads or changes them:
the file tools refuse them (policy.py), searches skip them (toolbox.py), shell commands that name them
are blocked (policy.py), and on macOS every shell command runs in a sandbox that the kernel enforces,
so even a disguised command (a wildcard, an encoding trick) can't read them."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List

# Relative to the home folder. Folders cover everything inside them.
PROTECTED_DIRS = (
    ".argus",                                    # Argus's own keys, phone token, voiceprint, database
    ".ssh", ".gnupg", ".aws", ".docker", ".kube",
    ".config/gh", ".config/gcloud",
    "Library/Keychains", "Library/Cookies", "Library/Safari", "Library/Containers/com.apple.Safari",
    "Library/Application Support/Google/Chrome", "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge", "Library/Application Support/Firefox",
)
PROTECTED_FILES = (".netrc", ".git-credentials", ".npmrc", ".pypirc")

# Shell commands that so much as name one of these places are blocked.
MENTION = re.compile(
    r"(?<![\w.-])\.(argus|ssh|gnupg|aws|docker|kube|netrc|git-credentials|npmrc|pypirc)(?![\w-])"
    r"|\.config/(gh|gcloud)\b"
    r"|Library/(Keychains|Cookies|Safari)\b|com\.apple\.Safari\b"
    r"|Google/Chrome\b|BraveSoftware\b|Microsoft(\\ |\s)+Edge\b|Firefox/Profiles\b",
    re.IGNORECASE)


def protected_roots(home: Path) -> List[Path]:
    return [home / p for p in PROTECTED_DIRS]


def _key(path: Path) -> str:
    # macOS disks ignore letter case by default (~/.ARGUS is ~/.argus), so comparisons do too.
    return str(path).rstrip("/").casefold()


def is_protected(path: Path, home: Path) -> bool:
    """True if `path` (already resolved) is, or is inside, a protected place."""
    s = _key(path)
    for root in protected_roots(Path(home)):
        r = _key(root)
        if s == r or s.startswith(r + "/"):
            return True
    return any(s == _key(Path(home) / f) for f in PROTECTED_FILES)



def _sb_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(home: Path, extra: Iterable[Path] = ()) -> str:
    """macOS sandbox (Seatbelt) profile for shell commands: everything is allowed except reading or
    writing the protected places."""
    rules = [f"(subpath {_sb_quote(str(p))})" for p in list(protected_roots(Path(home))) + list(extra)]
    rules += [f"(literal {_sb_quote(str(Path(home) / f))})" for f in PROTECTED_FILES]
    return "(version 1)\n(allow default)\n(deny file-read* file-write*\n  " + "\n  ".join(rules) + ")\n"
