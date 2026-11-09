"""Hermes `pre_tool_call` hook: Argus's privacy rules, enforced before any desktop or browser action.

Hermes runs this for every computer_use / browser / web_extract call (configured by setup.sh with
fail_closed: true, so if this script ever crashes the action is blocked, not allowed).
Reads the call as JSON on stdin; prints {"decision": "block", "reason": ...} or {}.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from . import config as config_mod
from .audit import Audit
from .guard import frontmost_app, is_blocked_app, mentions_blocked_site, read_state, screen_locked

# computer_use actions that look at or act on whatever window is in front when no app is named
_WHOLE_SCREEN_TARGETS = ("", "screen", "desktop")


def _host_of(args: Dict[str, Any]) -> Optional[str]:
    for key in ("url", "href"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return urlsplit(value if "//" in value else "//" + value).hostname
    return None


def evaluate(payload: Dict[str, Any], settings: config_mod.Settings,
             state: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The reason to block this tool call, or None to let it through."""
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") or {}
    if not isinstance(args, dict):
        args = {}

    site = mentions_blocked_site(args, settings.blocked_sites)
    if site:
        return f"Argus privacy rule: {site} is on the never-touch list."

    if tool != "computer_use":
        return None

    if state is None:
        state = read_state(settings.state_path) or {}
    locked = state.get("locked")
    if locked is None:
        locked = screen_locked()
    if locked:
        return "Argus privacy rule: the Mac is locked, so Argus won't look at or use the screen."
    if state.get("screen_private"):
        return "Argus privacy rule: screen access is switched off in the Argus menu."

    app = str(args.get("app") or "").strip()
    if app and app.lower() not in _WHOLE_SCREEN_TARGETS:
        if is_blocked_app(app, settings.blocked_apps):
            return f"Argus privacy rule: {app} is on the never-touch list."
        return None

    action = str(args.get("action") or "")
    if action in ("list_apps", "list_windows", "wait"):
        return None
    front_bundle, front_name = state.get("front_bundle"), state.get("front_name")
    if front_bundle is None and front_name is None:
        front = frontmost_app()
        if front is None:
            return ("Argus privacy rule: couldn't check which app is in front, so it won't look at or act on "
                    "the screen. Name the app you want instead.")
        front_bundle, front_name = front
    for candidate in (front_bundle, front_name):
        if candidate and is_blocked_app(candidate, settings.blocked_apps):
            return (f"Argus privacy rule: {front_name or front_bundle} is in front and it's on the never-touch "
                    f"list. Switch to another app first.")
    return None


def main(stdin=sys.stdin, stdout=sys.stdout) -> int:
    try:
        payload = json.load(stdin)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        settings = config_mod.load()
    except Exception as exc:  # unreadable settings: refuse rather than guess
        stdout.write(json.dumps({"decision": "block", "reason": f"Argus settings unreadable: {exc}"}))
        return 0
    reason = evaluate(payload, settings)
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    Audit(settings.audit_path).log(
        "tool_blocked" if reason else "tool_allowed", tool=tool, action=args.get("action"),
        app=args.get("app"), host=_host_of(args), session=payload.get("session_id"), reason=reason)
    stdout.write(json.dumps({"decision": "block", "reason": reason} if reason else {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
