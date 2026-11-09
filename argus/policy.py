"""What Argus may do without asking, what needs your OK, and what is never allowed.

The shell rules start from Hermes Agent's dangerous-command patterns (tools/approval_detection.py,
MIT, Nous Research), trimmed to macOS/POSIX and extended for Argus: protecting its own secrets,
data leaving the Mac, macOS system changes, and installing software.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .guard import is_blocked_app, mentions_blocked_site

ALLOW, ASK, BLOCK = "allow", "ask", "block"


@dataclass(frozen=True)
class Verdict:
    action: str
    reason: str = ""
    scope: str = ""        # what an "Allow for this task" answer covers


def _rx(patterns: Iterable[Tuple[str, str]]) -> List[Tuple[re.Pattern, str]]:
    return [(re.compile(p, re.IGNORECASE), why) for p, why in patterns]


# Never, even with approval: unrecoverable or self-defeating.
HARDLINE = _rx([
    (r"\brm\s+(-[a-z]*\s+)*(-[a-z]*r[a-z]*|--recursive)\s+(-[a-z]*\s+)*(/|~|\$home|/users/?|/system|/library|/applications)(\s|/?\*?$|$)",
     "deleting the whole disk, home folder or system folders"),
    (r"\bmkfs\b|\bnewfs\b|\bdiskutil\s+(erase|zerodisk|partitiondisk|apfs\s+delete)", "erasing a disk"),
    (r"\bdd\b[^\n]*\bof=/dev/", "writing raw data to a disk"),
    (r">\s*/dev/(r?disk|sd|nvme)", "writing raw data to a disk"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bsudo\b|\bsu\s+-?\s*(root)?\s*$|\bdoas\b", "running as administrator (Argus never uses sudo)"),
    (r"\bcsrutil\b|\bspctl\s+--master-disable|\bnvram\b", "turning off macOS security protections"),
    (r"\btccutil\b", "changing macOS privacy permissions"),
    (r"\bsecurity\s+(dump-keychain|find-(generic|internet)-password\b[^\n]*-[wg]\b|export)", "reading passwords from the keychain"),
    (r"\.argus/(secrets\.env|phone_token|voiceprint)|argus\.db\b", "reading Argus's own secrets"),
    (r"\.ssh/(id_|.*_key)|\.gnupg/|\.aws/credentials|\.config/gh/hosts", "reading private keys or credentials"),
    (r"\b(shutdown|halt|reboot)\b|\bpmset\b[^\n]*\bsleepnow\b|\bkillall\s+(-[a-z0-9]+\s+)*(loginwindow|WindowServer|Finder|Dock)\b",
     "shutting down or breaking the session"),
    (r"(curl|wget)\b[^\n|]*\|\s*(ba|z|fi)?sh\b", "running a script straight from the internet"),
])

# Allowed only with your OK.
ASK_PATTERNS = _rx([
    (r"\brm\b", "deleting files"),
    (r"\bfind\b[^\n]*(-delete\b|-exec\s+rm\b)|\bxargs\b[^\n]*\brm\b", "deleting files"),
    (r"\bmv\b|\bshred\b|\btruncate\b", "moving or overwriting files"),
    (r"\bchmod\b|\bchown\b|\bchflags\b|\bxattr\s+-[dc]", "changing file permissions"),
    (r"\bkill(all)?\b|\bpkill\b|\blaunchctl\b", "stopping or changing running programs"),
    (r"\bdefaults\s+(write|delete)\b|\bnetworksetup\b|\bscutil\b|\bsystemsetup\b|\bpmset\b|\bsoftwareupdate\b",
     "changing macOS settings"),
    (r"\bosascript\b|\bautomator\b|\bshortcuts\s+run\b", "controlling other apps with scripts"),
    (r"\b(brew|pip3?|pipx|npm|pnpm|yarn|gem|cargo|go)\s+(install|uninstall|add|remove|upgrade|update)\b|\buv\s+(pip|tool)\b",
     "installing or removing software"),
    (r"\bgit\s+(push|reset\s+--hard|clean\b|checkout\s+--|branch\s+-D|rebase)\b", "changing or publishing a git repository"),
    (r"\b(curl|wget|http|xh)\b[^\n]*(\s-d\b|--data|\s-F\b|--form|--upload-file|\s-T\b|-X\s*(POST|PUT|PATCH|DELETE))",
     "sending data to the internet"),
    (r"\b(scp|rsync|sftp|ftp|nc|ncat|netcat|ssh|telnet)\b", "connecting to or copying to another computer"),
    (r"\b(mail|sendmail|mailx|msmtp)\b", "sending email"),
    (r"\bcrontab\b|\bat\s+-?[0-9a-z]", "scheduling commands to run later"),
    (r"\bDROP\s+(TABLE|DATABASE)\b|\bDELETE\s+FROM\b|\bTRUNCATE\b", "deleting database data"),
    (r"\bdiskutil\b|\bhdiutil\b|\bmount\b|\bumount\b", "changing disks"),
    (r"\bopen\s+(-a\s+)?[^\n]*\.(pkg|dmg|mpkg)\b|\binstaller\b", "installing software"),
    (r"\bpython3?\s+-c\b|\bperl\s+-e\b|\bruby\s+-e\b|\bnode\s+-e\b", "running an inline script"),
    (r"(^|[;&|]\s*)(ba|z|fi)?sh\s+-c\b|\beval\b", "running a nested shell command"),
    (r">\s*~|>\s*/(?!dev/null)|>>\s*~|>>\s*/(?!dev/null)", "writing a file outside the workspace"),
])

# Paths Argus may never read or write.
PROTECTED_PATHS = (".argus/secrets.env", ".argus/phone_token", ".argus/voiceprint.npy", ".argus/argus.db",
                   ".ssh/", ".gnupg/", ".aws/", "Library/Keychains/", ".config/gh/", "Library/Cookies/",
                   "Library/Application Support/Google/Chrome/", "Library/Safari/")

# Desktop actions (computer toolset members) and what they need.
COMPUTER_READ_ONLY = frozenset({"screenshot", "zoom", "cursor_position", "wait", "mouse_move"})
COMPUTER_INPUT = frozenset({"left_click", "right_click", "middle_click", "double_click", "triple_click",
                            "left_click_drag", "left_mouse_down", "left_mouse_up", "scroll", "type", "key",
                            "hold_key"})
# Key combinations that are never sent (ported from Hermes computer_use, MIT): log out, lock, empty trash...
BLOCKED_KEY_COMBOS = [frozenset(c) for c in (
    {"cmd", "shift", "q"}, {"cmd", "option", "shift", "q"}, {"cmd", "ctrl", "q"}, {"cmd", "shift", "backspace"},
    {"cmd", "option", "backspace"}, {"cmd", "option", "esc"}, {"cmd", "ctrl", "power"}, {"ctrl", "option", "cmd", "power"},
)]
_KEY_ALIASES = {"command": "cmd", "super": "cmd", "meta": "cmd", "control": "ctrl", "alt": "option", "opt": "option",
                "delete": "backspace", "escape": "esc"}
BLOCKED_TYPED_TEXT = _rx([
    (r"curl\s+[^|]*\|\s*(ba|z)?sh", "typing a command that runs a script from the internet"),
    (r"\bsudo\b", "typing an administrator command"),
    (r"\brm\s+-[a-z]*r", "typing a recursive delete"),
])

CHANNELS_WITH_DESKTOP = frozenset({"voice"})


def canon_keys(combo: str) -> frozenset:
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*|(?<=\w)-(?=\w)", combo) if p.strip()]
    return frozenset(_KEY_ALIASES.get(p, p) for p in parts)


class Policy:
    def __init__(self, settings, *, workspace: Path, home: Optional[Path] = None,
                 desktop_state: Callable[[], Dict[str, Any]] = dict):
        self.settings = settings
        self.workspace = Path(workspace).expanduser().resolve()
        self.home = Path(home or Path.home()).expanduser().resolve()
        self.desktop_state = desktop_state     # {"locked", "screen_private", "front_bundle", "front_name", "visible_apps"}

    # ---- paths -------------------------------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        p = Path(os.path.expanduser(str(path or ".")))
        if not p.is_absolute():
            p = self.workspace / p
        return p.resolve()

    def protected(self, path: Path) -> bool:
        text = str(path)
        rel = text[len(str(self.home)) + 1:] if text.startswith(str(self.home) + os.sep) else text
        return any(rel.startswith(p) or f"/{p}" in text for p in PROTECTED_PATHS)

    def inside_workspace(self, path: Path) -> bool:
        return path == self.workspace or self.workspace in path.parents

    # ---- the decision ------------------------------------------------------------------------------
    def check(self, tool: str, args: Dict[str, Any], channel: str, toolset: str = "") -> Verdict:
        args = args if isinstance(args, dict) else {}
        site = mentions_blocked_site(args, self.settings.blocked_sites)
        if site:
            return Verdict(BLOCK, f"{site} is on your never-touch list.")
        if toolset == "computer":
            return self._computer(tool, args, channel)
        if tool == "run_command":
            return self._shell(str(args.get("command") or ""))
        if tool in ("read_file", "list_files", "search_files"):
            path = self.resolve(args.get("path") or ".")
            if self.protected(path):
                return Verdict(BLOCK, "that location holds passwords or keys, so Argus never reads it.")
            return Verdict(ALLOW)
        if tool == "write_file":
            path = self.resolve(args.get("path") or "")
            if self.protected(path):
                return Verdict(BLOCK, "that location holds passwords or keys, so Argus never changes it.")
            if self.inside_workspace(path):
                return Verdict(ALLOW)
            return Verdict(ASK, f"write to {path}", scope=f"write:{path.parent}")
        if tool in ("open_app",):
            name = str(args.get("name") or "")
            if is_blocked_app(name, self.settings.blocked_apps):
                return Verdict(BLOCK, f"{name} is on your never-touch list.")
            return Verdict(ALLOW)
        if tool == "schedule" and args.get("action") == "create" and args.get("mode") == "task":
            return Verdict(ASK, f"schedule a task to run by itself: {str(args.get('text'))[:120]}", scope="schedule:task")
        if tool == "glasses_look" and channel not in ("voice", "phone", "telegram"):
            return Verdict(BLOCK, "the glasses camera is only used when you ask.")
        return Verdict(ALLOW)

    def _shell(self, command: str) -> Verdict:
        if not command.strip():
            return Verdict(BLOCK, "empty command")
        for rx, why in HARDLINE:
            if rx.search(command):
                return Verdict(BLOCK, f"not allowed: {why}.")
        for rx, why in ASK_PATTERNS:
            if rx.search(command):
                return Verdict(ASK, f"{why}: {command[:200]}", scope=f"shell:{why}")
        return Verdict(ALLOW)

    def _computer(self, member: str, args: Dict[str, Any], channel: str) -> Verdict:
        if channel not in CHANNELS_WITH_DESKTOP and not (channel == "telegram" and self.settings.telegram_desktop):
            if not (channel == "phone" and member == "screenshot"):
                return Verdict(BLOCK, "the screen can only be used from your voice at the Mac.")
        state = self.desktop_state() or {}
        if state.get("locked"):
            return Verdict(BLOCK, "the Mac is locked, so Argus won't look at or use the screen.")
        if state.get("screen_private"):
            return Verdict(BLOCK, "screen access is switched off in the Argus menu.")
        if member not in COMPUTER_READ_ONLY | COMPUTER_INPUT:
            return Verdict(BLOCK, f"unknown desktop action {member}")
        apps = [state.get("front_bundle"), state.get("front_name")] + list(state.get("visible_apps") or [])
        if state.get("front_bundle") is None and state.get("front_name") is None:
            return Verdict(BLOCK, "couldn't check which apps are on screen, so Argus won't look at it.")
        for app in apps:
            if app and is_blocked_app(str(app), self.settings.blocked_apps):
                return Verdict(BLOCK, f"{app} is on screen and it's on your never-touch list. Hide it first.")
        if member in ("key", "hold_key"):
            combo = canon_keys(str(args.get("text") or ""))
            if any(b <= combo for b in BLOCKED_KEY_COMBOS):
                return Verdict(BLOCK, "that key combination (log out, lock, force quit, empty trash) is never sent.")
        if member == "type":
            for rx, why in BLOCKED_TYPED_TEXT:
                if rx.search(str(args.get("text") or "")):
                    return Verdict(BLOCK, f"not allowed: {why}.")
        if member in COMPUTER_INPUT:
            return Verdict(ASK, f"use your Mac: {member.replace('_', ' ')}", scope=f"computer:{member}")
        return Verdict(ALLOW)
