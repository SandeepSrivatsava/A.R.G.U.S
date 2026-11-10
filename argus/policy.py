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

from urllib.parse import urlsplit

from .guard import is_blocked_app, mentions_blocked_site
from .protected import MENTION, is_protected

ALLOW, ASK, BLOCK = "allow", "ask", "block"


@dataclass(frozen=True)
class Verdict:
    action: str
    reason: str = ""
    scope: str = ""        # what an "Allow for this task" answer covers
    money: bool = False    # paying or buying: only Touch ID can approve it


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
    (r"\bargus\.db\b|\bsecrets\.env\b|\bphone_token\b|\bvoiceprint\.npy\b", "reading Argus's own secrets"),
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

# A web address whose query is this long could be carrying your data out; it needs your OK.
LONG_QUERY = 300

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


# ---- paying and buying: always Touch ID ------------------------------------------------------------------
# A click looks the same whether it opens a menu or places an order, so Argus looks at what's being pressed
# (the button's label, from the Mac's accessibility interface), the page it's on (window title) and what's
# being typed (a card number). Not perfect (a button may have no label), so the model is also told to stop
# before paying, but anything that matches needs Touch ID even when screen use was allowed by voice.
MONEY_BUTTON = re.compile(
    r"\b(buy|purchase|pay|checkout|check out|place (my |your |the )?order|complete (my |your |the )?(order|purchase|"
    r"payment|booking and pay)|confirm (and |& )?pay|pay now|order now|subscribe|start (my |your )?(free )?trial|"
    r"donate|send money|add (a )?(card|payment method)|book and pay|reserve and pay|top up|add funds)\b", re.I)
MONEY_PAGE = re.compile(
    r"\b(check ?out|payment|billing|place (your )?order|order (review|summary)|purchase|buy now|subscribe|"
    r"subscription|donat\w+|wallet|top ?up|add funds)\b", re.I)
IPHONE_MIRRORING = "com.apple.ScreenContinuity"
CLICKS = frozenset({"left_click", "right_click", "middle_click", "double_click", "triple_click", "left_mouse_down",
                    "left_mouse_up"})


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def has_card_number(text: str) -> bool:
    for match in re.finditer(r"(?:\d[ -]?){13,19}", str(text)):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            return True
    return False


class Policy:
    def __init__(self, settings, *, workspace: Path, home: Optional[Path] = None,
                 desktop_state: Callable[[], Dict[str, Any]] = dict,
                 inspect_point: Callable[[Any], Dict[str, Any]] = lambda coordinate: {},
                 outside_workspace: str = "read"):
        self.settings = settings
        # "read": files anywhere may be read (except protected places) and writing elsewhere needs your OK
        # (the Mac, Android). "none": nothing outside the workspace at all (the always-on server).
        self.outside_workspace = outside_workspace
        self.workspace = Path(workspace).expanduser().resolve()
        self.home = Path(home or Path.home()).expanduser().resolve()
        self.desktop_state = desktop_state     # {"locked", "screen_private", "front_bundle", "front_name", "visible_apps",
        #                                         "front_title"}
        self.inspect_point = inspect_point     # screenshot coordinate -> {"role", "title", "description", "value"}

    # ---- paths -------------------------------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        text = str(path or ".")
        p = self.home / text[2:] if text.startswith("~/") else self.home if text == "~" else Path(os.path.expanduser(text))
        if not p.is_absolute():
            p = self.workspace / p
        return p.resolve()

    def protected(self, path: Path) -> bool:
        return is_protected(path, self.home)

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
            if args.get("cwd") and self.protected(self.resolve(str(args.get("cwd")))):
                return Verdict(BLOCK, "that folder holds passwords or keys, so Argus never works in it.")
            return self._shell(str(args.get("command") or ""))
        if tool in ("fetch_page", "open_url"):
            parts = urlsplit(str(args.get("url") or ""))
            if len(parts.query) + len(parts.fragment) > LONG_QUERY:
                return Verdict(ASK, f"open a long web address that could carry your data to {parts.hostname}: "
                                    f"{str(args.get('url'))[:160]}…", scope=f"web:{parts.hostname}")
            return Verdict(ALLOW)
        if tool in ("read_file", "list_files", "search_files"):
            path = self.resolve(args.get("path") or ".")
            if self.protected(path):
                return Verdict(BLOCK, "that location holds passwords or keys, so Argus never reads it.")
            if self.outside_workspace == "none" and not self.inside_workspace(path):
                return Verdict(BLOCK, "here Argus only works inside its own workspace folder.")
            return Verdict(ALLOW)
        if tool == "write_file":
            path = self.resolve(args.get("path") or "")
            if self.protected(path):
                return Verdict(BLOCK, "that location holds passwords or keys, so Argus never changes it.")
            if self.inside_workspace(path):
                return Verdict(ALLOW)
            if self.outside_workspace == "none":
                return Verdict(BLOCK, "here Argus only works inside its own workspace folder.")
            return Verdict(ASK, f"write to {path}", scope=f"write:{path.parent}")
        if tool == "send_text":
            digits = re.sub(r"[^\d+]", "", str(args.get("to") or ""))
            if len(re.sub(r"\D", "", digits)) < 7:
                return Verdict(BLOCK, "that isn't a full phone number (texts to short codes can cost money).")
            message = " ".join(str(args.get("message") or "").split())
            if not message:
                return Verdict(BLOCK, "the text is empty.")
            if has_card_number(message):
                return Verdict(BLOCK, "that text contains a card number, which Argus never sends.")
            return Verdict(ASK, f"text {digits}: {message[:160]}", scope=f"text:{digits}")
        if tool in ("open_app",):
            name = str(args.get("name") or "")
            if is_blocked_app(name, self.settings.blocked_apps):
                return Verdict(BLOCK, f"{name} is on your never-touch list.")
            return Verdict(ALLOW)
        if tool == "phone_call":
            from .calls import normalize_number, spoken_number
            number, why = normalize_number(str(args.get("number") or ""),
                                           bool(getattr(self.settings, "call_international", False)))
            if number is None:
                return Verdict(BLOCK, why)
            who = " ".join(str(args.get("business") or "").split())[:60]
            goal = " ".join(str(args.get("goal") or "").split())[:120]
            target = f"{who} at {spoken_number(number)}" if who else spoken_number(number)
            return Verdict(ASK, f"call {target} from your number to {goal}", scope=f"call:{number}")
        if tool == "schedule" and args.get("action") == "create" and args.get("mode") == "task":
            return Verdict(ASK, f"schedule a task to run by itself: {str(args.get('text'))[:120]}", scope="schedule:task")
        if tool == "glasses_look" and channel not in ("voice", "phone"):
            return Verdict(BLOCK, "the glasses camera is only used when you ask.")
        return Verdict(ALLOW)

    def _shell(self, command: str) -> Verdict:
        if not command.strip():
            return Verdict(BLOCK, "empty command")
        for rx, why in HARDLINE:
            if rx.search(command):
                return Verdict(BLOCK, f"not allowed: {why}.")
        if MENTION.search(command):
            return Verdict(BLOCK, "that command touches a place that holds passwords or keys.")
        for rx, why in ASK_PATTERNS:
            if rx.search(command):
                return Verdict(ASK, f"{why}: {command[:200]}", scope=f"shell:{why}")
        return Verdict(ALLOW)

    def _computer(self, member: str, args: Dict[str, Any], channel: str) -> Verdict:
        if channel not in CHANNELS_WITH_DESKTOP:
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
            money = self._money(member, args, state)
            if money:
                return Verdict(ASK, f"pay or buy something ({money})", scope="money", money=True)
            if state.get("front_bundle") == IPHONE_MIRRORING or state.get("front_name") == "iPhone Mirroring":
                # Argus can't see which iPhone app is open in there, so the iPhone gets its own OK.
                return Verdict(ASK, f"use your iPhone through iPhone Mirroring: {member.replace('_', ' ')}",
                               scope="computer:iphone")
            return Verdict(ASK, f"use your Mac: {member.replace('_', ' ')}", scope=f"computer:{member}")
        return Verdict(ALLOW)

    def _money(self, member: str, args: Dict[str, Any], state: Dict[str, Any]) -> str:
        """Why this screen action looks like paying, or ""."""
        title = " ".join(str(state.get("front_title") or "").split())
        if member in CLICKS | {"type", "key", "hold_key"} and MONEY_PAGE.search(title):
            return f"the page is {title[:60]!r}"
        if member == "type" and has_card_number(str(args.get("text") or "")):
            return "typing a card number"
        if member in CLICKS and args.get("coordinate") is not None:
            try:
                info = self.inspect_point(args.get("coordinate")) or {}
            except Exception:
                info = {}
            label = " ".join(str(info.get(k) or "") for k in ("title", "description", "value", "help"))
            label = " ".join(label.split())
            if MONEY_BUTTON.search(label):
                return f"pressing {label[:40]!r}"
        return ""
