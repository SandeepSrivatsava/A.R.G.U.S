"""Test doubles shared by the Argus tests: a fake Hermes Runs API server and helpers."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List


class FakeHermes(ThreadingHTTPServer):
    """Speaks just enough of the Hermes API: runs, SSE events, approvals, stop, chat completions."""

    daemon_threads = True

    def __init__(self, key: str = "test-key"):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.key = key
        self.script: List[List[Dict[str, Any]]] = []   # one list of events per run
        self.runs: List[Dict[str, Any]] = []
        self.approvals: List[Dict[str, Any]] = []
        self.stopped: List[str] = []
        self.chats: List[Dict[str, Any]] = []
        self.chat_reply = "Sure thing."
        self.approval_answered = threading.Event()
        self.event_delay = 0.0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    server: FakeHermes

    def log_message(self, *a):
        pass

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.server.key}":
            self._json(401, {"error": {"message": "bad key"}})
            return False
        return True

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok"})
        if not self._authed():
            return
        if self.path.endswith("/events"):
            run_id = self.path.split("/")[3]
            index = int(run_id.split("_")[1])
            events = self.server.script[index] if index < len(self.server.script) else []
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": keepalive\n\n")
            for ev in events:
                if ev.get("event") == "_wait_for_approval":
                    self.server.approval_answered.wait(10)
                    continue
                if ev.get("event") == "_wait_for_stop":
                    deadline = time.time() + 10
                    while run_id not in self.server.stopped and time.time() < deadline:
                        time.sleep(0.02)
                    ev = {"event": "run.cancelled"}
                time.sleep(self.server.event_delay)
                data = dict(ev, run_id=run_id)
                self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
            return
        self._json(404, {})

    def do_POST(self):
        if not self._authed():
            return
        body = self._body()
        if self.path == "/v1/runs":
            run_id = f"run_{len(self.server.runs)}"
            self.server.runs.append(dict(body, run_id=run_id))
            return self._json(202, {"run_id": run_id, "status": "queued"})
        if self.path.endswith("/approval"):
            self.server.approvals.append(dict(body, run_id=self.path.split("/")[3]))
            self.server.approval_answered.set()
            return self._json(200, {"object": "hermes.run.approval_response", "choice": body.get("choice")})
        if self.path.endswith("/stop"):
            self.server.stopped.append(self.path.split("/")[3])
            return self._json(200, {"status": "stopping"})
        if self.path == "/v1/chat/completions":
            self.server.chats.append(dict(body, session=self.headers.get("X-Hermes-Session-Id")))
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": self.server.chat_reply}}]})
        self._json(404, {})


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
