"""Shared test helpers: a scripted Claude, a fake Telegram, and a quick way to build Argus's brain."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

from mock_claude import ClaudeMock


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def say(text: str) -> Tuple[List[Dict[str, Any]], str]:
    return [{"type": "text", "text": text}], "end_turn"


def call(name: str, args: Dict[str, Any], text: str = "", toolset: str = "") -> Tuple[List[Dict[str, Any]], str]:
    blocks: List[Dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    block: Dict[str, Any] = {"type": "tool_use", "name": name, "input": args}
    if toolset:
        block["toolset_name"] = toolset
    blocks.append(block)
    return blocks, "tool_use"


class Script:
    """Answers each request with the next scripted reply (then 'Done.')."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies: List[Dict[str, Any]] = []

    def __call__(self, body):
        self.bodies.append(body)
        return self.replies.pop(0) if self.replies else say("Done.")


def make_settings(tmp_path: Path, **overrides):
    from argus import config
    s = config.Settings()
    s.workspace_dir = str(tmp_path / "workspace")
    s.user_name, s.city, s.timezone = "Sandeep", "Portland, OR", "America/Los_Angeles"
    s.web_search = False
    for k, v in overrides.items():
        setattr(s, k, v)
    Path(s.workspace_dir).mkdir(parents=True, exist_ok=True)
    return s


def make_agent(tmp_path: Path, responder, *, settings=None, computer=None, desktop=None, notify=None, glasses=None):
    from argus.agent import Agent
    from argus.audit import Audit
    from argus.llm import Claude
    from argus.policy import Policy
    from argus.store import Store
    from argus.toolbox import build_tools
    mock = ClaudeMock(responder).start()
    s = settings or make_settings(tmp_path)
    state = desktop if desktop is not None else {"locked": False, "front_bundle": "com.apple.Safari",
                                                 "front_name": "Safari", "visible_apps": []}
    agent = Agent(Claude("sk-ant-test", base_url=mock.url, max_retries=1), Store(tmp_path / "argus.db"),
                  Policy(s, workspace=s.workspace, desktop_state=lambda: dict(state)), build_tools(),
                  settings=s, audit=Audit(tmp_path / "audit.jsonl"), computer=computer,
                  notify=notify or (lambda t: False), glasses=glasses)
    return agent, mock


class FakeTelegram(ThreadingHTTPServer):
    """Enough of the Telegram Bot API for Argus: updates, messages, buttons, files."""

    daemon_threads = True

    def __init__(self, token: str = "123:abc"):
        super().__init__(("127.0.0.1", 0), _TgHandler)
        self.token = token
        self.updates: List[Dict[str, Any]] = []
        self.sent: List[Dict[str, Any]] = []
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.files: Dict[str, bytes] = {}
        self.fail_methods: set = set()      # Bot API methods that answer {"ok": false} (error tests)
        self._next = 1
        self._lock = threading.Lock()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def handle_error(self, request, client_address):
        pass

    def push(self, update: Dict[str, Any]) -> None:
        with self._lock:
            update = dict(update, update_id=self._next)
            self._next += 1
            self.updates.append(update)

    def message(self, text: str, user: int = 987654321, **extra) -> None:
        self.push({"message": dict({"message_id": self._next, "from": {"id": user}, "chat": {"id": user, "type": "private"},
                                    "text": text}, **extra)})

    def press(self, data: str, user: int = 987654321) -> None:
        self.push({"callback_query": {"id": f"cb{self._next}", "from": {"id": user}, "data": data,
                                      "message": {"chat": {"id": user, "type": "private"}}}})


class _TgHandler(BaseHTTPRequestHandler):
    server: FakeTelegram

    def log_message(self, *a):
        pass

    def _ok(self, result: Any) -> None:
        data = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        prefix = f"/file/bot{self.server.token}/"
        if self.path.startswith(prefix) and self.path[len(prefix):] in self.server.files:
            data = self.server.files[self.path[len(prefix):]]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        prefix = f"/bot{self.server.token}/"
        if not self.path.startswith(prefix):
            self.send_response(401)
            self.end_headers()
            return
        method = self.path[len(prefix):]
        self.server.calls.append((method, body))
        if method in self.server.fail_methods:
            data = json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: refused in this test"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if method == "getUpdates":
            deadline = time.time() + min(float(body.get("timeout") or 0), 1.0)
            while time.time() < deadline:
                with self.server._lock:
                    pending = [u for u in self.server.updates if u["update_id"] >= int(body.get("offset") or 0)]
                if pending:
                    break
                time.sleep(0.02)
            with self.server._lock:
                pending = [u for u in self.server.updates if u["update_id"] >= int(body.get("offset") or 0)]
            return self._ok(pending)
        if method == "sendMessage":
            self.server.sent.append(body)
            return self._ok({"message_id": 1000 + len(self.server.sent)})
        if method == "getFile":
            return self._ok({"file_path": f"photos/{body.get('file_id')}.jpg"})
        if method == "getMe":
            return self._ok({"username": "argus_test_bot"})
        return self._ok(True)


# ---- the listening loop with scripted transcripts and a fake microphone ---------------------------------
class FakeSource:
    def __init__(self):
        self.is_open = False

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def read(self, timeout=0.25):
        return None


class FakeSegmenter:
    speech_active = False

    def feed(self, chunk):
        return []

    def flush(self):
        return []

    def reset(self):
        pass

    def recent_audio(self):
        return np.zeros(0, dtype=np.float32)


def make_argus(tmp_path, transcripts, script=None, answers=()):
    from argus.approvals import ScriptedApprover
    from argus.audit import Audit
    from argus.brain import Brain, Sessions
    from argus.daemon import Argus
    from argus.hearing import ScriptedTranscriber
    from argus.listener import Listener
    from argus.voice import RecordingVoice, Speaker
    from argus.wake import NameMatcher
    script = script or Script()
    agent, _ = make_agent(tmp_path, script)
    voice = RecordingVoice()
    speaker = Speaker(voice)
    audit = Audit(tmp_path / "audit.jsonl")
    brain = Brain(agent, speaker, ScriptedApprover(list(answers)), audit, Sessions(agent.store))
    argus = Argus(source=FakeSource(), segmenter=FakeSegmenter(), transcriber=ScriptedTranscriber(transcripts),
                  listener=Listener(NameMatcher("argus")), speaker=speaker, brain=brain, audit=audit)
    argus.set_mic(True)
    return argus, voice, script


SPEECH = np.ones(16000, dtype=np.float32) * 0.1
