"""iPhone gateway: lets Siri / Back Tap Shortcuts talk to Argus over Tailscale.

* Listens only on this Mac's Tailscale address (100.x.y.z), never on Wi-Fi or the internet.
* Every request needs the secret token from ~/.argus/phone_token (compared in constant time).
* Repeated bad tokens lock that address out for 15 minutes; requests are size-limited.
* From the phone Argus can look and answer, but anything that needs approval is refused
  (approve risky things at the Mac with Touch ID, or in Telegram).

POST /ask   {"text": "..."}                                   -> {"reply": "..."}
POST /look  {"text": "...", "image": "<base64 png/jpeg>"}     -> {"reply": "..."}   (phone screen or glasses photo)
GET  /ping                                                     -> {"ok": true}
The Argus iPhone app (Meta glasses):
POST /glasses/status {"connected": true, "device": "Ray-Ban Meta"} -> {"ok": true}
GET  /glasses/next                       -> {"capture": "<request id>"} or {} (long poll, ~25 s)
POST /glasses/frame {"request_id": "...", "image": "<base64 jpeg>"} -> {"ok": true}
"""
from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .audit import Audit

log = logging.getLogger("argus.phone")

MAX_BODY = 16 * 1024 * 1024
LOCKOUT_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")   # the only addresses Tailscale hands out



def tailscale_ip() -> Optional[str]:
    for exe in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/usr/local/bin/tailscale",
                "/opt/homebrew/bin/tailscale"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            continue
        for ip in out:
            try:
                if ipaddress.ip_address(ip) in TAILSCALE_NET:
                    return ip
            except ValueError:
                continue
    return None


def load_or_create_token(path: Path) -> str:
    path = Path(path)
    if path.exists():
        token = path.read_text().strip()
        if len(token) >= 32:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)          # an older file may have been readable by others
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    return token


def image_data_url(b64: str) -> str:
    cleaned = re.sub(r"\s+", "", b64 or "")
    if cleaned.startswith("data:image/"):
        cleaned = cleaned.split(",", 1)[-1]
    try:
        raw = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image is not valid base64") from exc
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        raise ValueError("HEIC screenshots aren't supported; set the Shortcut to convert to PNG or JPEG")
    else:
        raise ValueError("image must be PNG or JPEG")
    return f"data:{mime};base64,{cleaned}"


class Gatekeeper:
    """Token check with per-address lockout."""

    def __init__(self, token: str, clock=time.monotonic):
        self.token = token.encode()
        self.clock = clock
        self._failures: Dict[str, Tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked_out(self, addr: str) -> bool:
        with self._lock:
            count, since = self._failures.get(addr, (0, 0.0))
            if count >= LOCKOUT_FAILURES and self.clock() - since < LOCKOUT_SECONDS:
                return True
            if count >= LOCKOUT_FAILURES:
                self._failures.pop(addr, None)
            return False

    def check(self, addr: str, header: Optional[str]) -> bool:
        supplied = (header or "").strip()
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        ok = bool(supplied) and hmac.compare_digest(supplied.encode(), self.token)
        with self._lock:
            if ok:
                self._failures.pop(addr, None)
            else:
                count, _ = self._failures.get(addr, (0, 0.0))
                self._failures[addr] = (count + 1, self.clock())
        return ok


class PhoneServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, ask: Callable[[Any], str], gate: Gatekeeper, audit: Audit, glasses=None):
        super().__init__(address, PhoneHandler)
        self.ask = ask              # runs one request through Argus's brain (phone channel) and returns the reply
        self.gate = gate
        self.audit = audit
        self.glasses = glasses
        self.busy = threading.Semaphore(2)


class PhoneHandler(BaseHTTPRequestHandler):
    server: PhoneServer
    server_version = "Argus"
    sys_version = ""
    timeout = 60  # seconds a client may take to send its request

    def log_message(self, fmt, *args):  # keep request details out of stderr logs
        return

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        addr = self.client_address[0]
        if self.server.gate.locked_out(addr):
            self._send(429, {"error": "too many bad tokens; try again later"})
            return False
        if not self.server.gate.check(addr, self.headers.get("Authorization")):
            self.server.audit.log("phone_bad_token", addr=addr)
            self._send(401, {"error": "unauthorised"})
            return False
        return True

    def _body(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self._send(413 if length > MAX_BODY else 400, {"error": "missing or oversized body"})
            return None
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
            return body
        except ValueError:
            self._send(400, {"error": "body must be a JSON object"})
            return None

    def do_GET(self):
        if not self._authorised():
            return
        if self.path == "/ping":
            self._send(200, {"ok": True})
        elif self.path == "/glasses/next" and self.server.glasses is not None:
            request_id = self.server.glasses.next_request(wait=25)
            self._send(200, {"capture": request_id} if request_id else {})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorised():
            return
        if self.path.startswith("/glasses/") and self.server.glasses is not None:
            body = self._body()
            if body is None:
                return
            if self.path == "/glasses/status":
                self.server.glasses.update_status(bool(body.get("connected")), str(body.get("device") or ""))
                self.server.audit.log("glasses", connected=bool(body.get("connected")), device=body.get("device"))
                self._send(200, {"ok": True})
            elif self.path == "/glasses/frame":
                try:
                    url = image_data_url(str(body.get("image") or ""))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                ok = self.server.glasses.deliver(str(body.get("request_id") or ""), base64.b64decode(url.split(",", 1)[1]))
                self._send(200 if ok else 409, {"ok": ok})
            else:
                self._send(404, {"error": "not found"})
            return
        if self.path not in ("/ask", "/look"):
            self._send(404, {"error": "not found"})
            return
        body = self._body()
        if body is None:
            return
        text = str(body.get("text") or "").strip()[:4000]
        if self.path == "/look":
            try:
                url = image_data_url(str(body.get("image") or ""))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            mime, data = url[5:].split(";base64,", 1)
            content: Any = [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
                            {"type": "text", "text": text or "What's in this picture? Describe the important part briefly."}]
        else:
            if not text:
                self._send(400, {"error": "text is empty"})
                return
            content = text
        if not self.server.busy.acquire(timeout=1):
            self._send(429, {"reply": "I'm still working on your last request."})
            return
        try:
            self.server.audit.log("phone_request", kind=self.path[1:], text=text[:500])
            reply = self.server.ask(content)
            self._send(200, {"reply": reply or "Done."})
        except Exception as exc:
            log.warning("phone request failed: %s", exc)
            self._send(502, {"reply": "Your Mac couldn't answer right now."})
        finally:
            self.server.busy.release()


def bind_address(settings) -> Optional[str]:
    """Where the gateway may listen: the Mac's Tailscale address (or 127.0.0.1 for tests)."""
    host = settings.phone_host or tailscale_ip()
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return None
    if ip == ipaddress.ip_address("127.0.0.1") or ip in TAILSCALE_NET:
        return str(ip)
    return None
