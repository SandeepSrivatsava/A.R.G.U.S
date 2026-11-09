"""Local control socket (~/.argus/run/argus.sock, owner-only) used by `argusctl` and the menu bar."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Dict

log = logging.getLogger("argus.control")

Handler = Callable[[Dict[str, Any]], Dict[str, Any]]


class ControlServer:
    def __init__(self, path: Path, handlers: Dict[str, Handler]):
        self.path = Path(path)
        self.handlers = handlers
        self._sock: socket.socket = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            self.path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            self._sock.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        self._sock.listen(4)
        self._sock.settimeout(0.5)
        threading.Thread(target=self._serve, name="argus-control", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
            self.path.unlink()
        except (OSError, AttributeError):
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                data = b""
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data.decode() or "{}")
                handler = self.handlers.get(str(request.get("cmd")))
                reply = handler(request) if handler else {"ok": False, "error": "unknown command"}
            except Exception as exc:  # report, don't crash the daemon
                log.exception("control command failed")
                reply = {"ok": False, "error": str(exc)}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass


def send(path: Path, cmd: str, timeout: float = 10.0, **fields: Any) -> Dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall((json.dumps(dict(fields, cmd=cmd)) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode() or "{}")
