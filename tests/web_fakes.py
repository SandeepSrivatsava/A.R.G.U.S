"""Stand-ins for the outside services the new features talk to: a JSON web service (Google Routes, Apple Maps,
ntfy) on 127.0.0.1, and the phone's Termux:API commands."""
from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit


class JsonService(ThreadingHTTPServer):
    """Answers every request with handler(request) -> (status, payload). Keeps every request it saw."""
    daemon_threads = True

    def __init__(self, handler: Callable[[Dict[str, Any]], Tuple[int, Any]]):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.handler = handler
        self.requests: List[Dict[str, Any]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    server: JsonService

    def log_message(self, *args):
        return

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        parts = urlsplit(self.path)
        request = {"method": self.command, "path": parts.path, "query": {k: v[0] for k, v in parse_qs(parts.query).items()},
                   "headers": {k.lower(): v for k, v in self.headers.items()},
                   "body": json.loads(raw) if raw.strip() else None}
        self.server.requests.append(request)
        status, payload = self.server.handler(request)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _serve


class FakeTermux:
    """Termux:API commands, scripted: answers[name] is a value (printed as JSON), a callable(args, stdin)
    returning that, or an exception to raise. Every call is recorded as (args, stdin)."""

    def __init__(self, **answers: Any):
        self.answers: Dict[str, Any] = {k.replace("_", "-"): v for k, v in answers.items()}
        self.calls: List[Tuple[List[str], Optional[str]]] = []

    def names(self) -> List[str]:
        return [args[0] for args, _ in self.calls]

    def __call__(self, args, input=None, capture_output=True, text=True, timeout=None, **kw):
        self.calls.append((list(args), input))
        answer = self.answers.get(args[0], "")
        if callable(answer) and not isinstance(answer, type):
            answer = answer(list(args), input)
        if isinstance(answer, BaseException) or (isinstance(answer, type) and issubclass(answer, BaseException)):
            raise answer
        if isinstance(answer, subprocess.CompletedProcess):
            return answer
        out = answer if isinstance(answer, str) else json.dumps(answer)
        return subprocess.CompletedProcess(args, 0, out, "")


def routed_to(service: JsonService, path: str = ""):
    """An http_json stand-in that sends every request to `service` (keeping the path when `path` is "")."""
    from argus.maps import http_json

    def request(method, url, headers, body=None, timeout=15):
        rest = path or "/" + (url.split("/", 3)[3] if url.count("/") >= 3 else "")
        return http_json(method, service.url + rest, headers, body, timeout)
    return request
