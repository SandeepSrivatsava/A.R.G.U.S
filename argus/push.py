"""Push notifications to your phone through ntfy (https://ntfy.sh, open source), for reminders and results
when Argus can't say them out loud: on the always-on server, or from the Mac while you're away from it.

The topic name works like a password (anyone who knows it can read the messages), so it's a long random
name kept in secrets.env (NTFY_TOPIC), never in the settings file. Messages pass through the ntfy server
you choose: ntfy.sh keeps them for up to 12 hours. For full privacy run your own ntfy server (reachable over
Tailscale) and set [push] server to it.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import secrets
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from .maps import MapsError, http_json

log = logging.getLogger("argus.push")
TOPIC = re.compile(r"[A-Za-z0-9_-]{20,64}")
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def new_topic() -> str:
    """A topic nobody can guess (about 190 random bits)."""
    return "argus-" + secrets.token_urlsafe(24)


def check_server(url: str) -> str:
    """Why `url` can't be used as an ntfy server, or "" if it's fine. Plain http only inside Tailscale."""
    parts = urlsplit(str(url or ""))
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        return "must be a web address like https://ntfy.sh"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return "must be just the server's address (the topic is kept in secrets.env)"
    if parts.scheme == "http":
        host = parts.hostname
        try:
            ip = ipaddress.ip_address(host)
            inside = ip.is_loopback or ip in TAILSCALE_NET
        except ValueError:
            inside = host == "localhost" or host.endswith(".ts.net")
        if not inside:
            return "plain http is only allowed inside Tailscale; use https"
    return ""


class Push:
    def __init__(self, server: str, topic: str, *, token: str = "", request: Callable[..., Any] = http_json,
                 audit=None):
        problem = check_server(server)
        if problem:
            raise ValueError(f"push server: {problem}")
        if not TOPIC.fullmatch(topic or ""):
            raise ValueError("the push topic must be 20 to 64 letters, digits, - or _")
        self.server, self.topic, self.token = server.rstrip("/"), topic, token
        self.request, self.audit = request, audit

    def send(self, text: str, *, title: str = "Argus", link: str = "", urgent: bool = False) -> bool:
        """Send one notification. Never raises: a failed push is logged, and the caller carries on."""
        message = " ".join(str(text).split())[:3500]
        if not message:
            return False
        body: Dict[str, Any] = {"topic": self.topic, "message": message, "title": title[:100],
                                "priority": 4 if urgent else 3}
        if link.startswith("https://"):
            body["click"] = link
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            status, _ = self.request("POST", self.server + "/", headers, body)
        except MapsError as exc:            # http_json's "couldn't reach" error
            log.warning("push failed: %s", exc)
            status = 0
        ok = 200 <= status < 300
        if self.audit is not None:
            self.audit.log("push", ok=ok, status=status, chars=len(message))
        return ok


def push_from(settings, secrets_map: Dict[str, str], audit=None, request: Callable[..., Any] = http_json) -> Optional[Push]:
    """The phone push set up in the settings and secrets, or None."""
    topic = secrets_map.get("NTFY_TOPIC", "")
    if not settings.push_enabled or not topic:
        return None
    try:
        return Push(settings.push_server, topic, token=secrets_map.get("NTFY_TOKEN", ""), request=request, audit=audit)
    except ValueError as exc:
        log.warning("push notifications are off: %s", exc)
        return None
