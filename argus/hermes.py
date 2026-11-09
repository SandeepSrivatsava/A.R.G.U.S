"""Small client for the Hermes Agent API on this Mac (127.0.0.1 only, key-authenticated).

Voice requests use the Runs API so Argus can stream the reply, see tool activity, and answer
approval requests. The phone gateway uses chat completions (one request, one answer).
"""
from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union
from urllib.parse import urlsplit

TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled", "run.interrupted"})
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class HermesError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def read_env_file(path: Union[str, Path]) -> Dict[str, str]:
    """KEY=VALUE lines of ~/.hermes/.env (comments and blank lines skipped)."""
    values: Dict[str, str] = {}
    p = Path(path).expanduser()
    if not p.exists():
        return values
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


class Hermes:
    def __init__(self, base_url: str = "http://127.0.0.1:8642", api_key: str = "", *, model: str = "",
                 timeout: float = 30.0, allow_remote: bool = False):
        parts = urlsplit(base_url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError("Hermes URL must look like http://127.0.0.1:8642")
        if parts.hostname not in LOOPBACK and not allow_remote:
            raise ValueError("Argus only talks to the Hermes API on this Mac (127.0.0.1)")
        if not api_key:
            raise ValueError("API_SERVER_KEY is missing from ~/.hermes/.env")
        self.host, self.port = parts.hostname, parts.port or 80
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls, env_file: str = "~/.hermes/.env") -> "Hermes":
        env = read_env_file(env_file)
        port = env.get("API_SERVER_PORT") or "8642"
        return cls(f"http://127.0.0.1:{port}", env.get("API_SERVER_KEY", ""),
                   model=env.get("API_SERVER_MODEL_NAME", ""))

    # ---- plumbing --------------------------------------------------------------------------------
    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        h.update(extra or {})
        return h

    def _request(self, method: str, path: str, body: Optional[dict] = None, *, timeout: Optional[float] = None,
                 headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        try:
            conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                         headers=self._headers(headers))
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise HermesError(f"Hermes isn't answering on port {self.port} ({exc})") from exc
        finally:
            conn.close()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {"raw": raw[:500].decode("utf-8", "replace")}
        return resp.status, data

    @staticmethod
    def _error_text(data: Any) -> str:
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            if err:
                return str(err)
        return str(data)[:300]

    # ---- API ---------------------------------------------------------------------------------------
    def healthy(self) -> bool:
        try:
            status, _ = self._request("GET", "/health", timeout=5)
            return status == 200
        except HermesError:
            return False

    def start_run(self, text: str, *, session_id: Optional[str] = None, instructions: Optional[str] = None) -> str:
        body: Dict[str, Any] = {"input": text}
        if session_id:
            body["session_id"] = session_id
        if instructions:
            body["instructions"] = instructions
        status, data = self._request("POST", "/v1/runs", body)
        if status not in (200, 201, 202) or not isinstance(data, dict) or not data.get("run_id"):
            raise HermesError(f"Hermes refused the request: {self._error_text(data)}", status)
        return str(data["run_id"])

    def events(self, run_id: str, *, idle_timeout: float = 900.0) -> Iterator[Dict[str, Any]]:
        """Server-sent events for one run, ending after its terminal event."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=idle_timeout)
        try:
            conn.request("GET", f"/v1/runs/{run_id}/events", headers=self._headers({"Accept": "text/event-stream"}))
            resp = conn.getresponse()
            if resp.status != 200:
                raise HermesError(f"event stream refused: {self._error_text(resp.read()[:500])}", resp.status)
            data_lines: List[str] = []
            while True:
                try:
                    raw = resp.readline()
                except socket.timeout as exc:
                    raise HermesError("Hermes went quiet for too long") from exc
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(":"):
                    continue                        # keepalive / comment
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                if line == "" and data_lines:
                    payload, data_lines = "\n".join(data_lines), []
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        yield event
                        if event.get("event") in TERMINAL_EVENTS:
                            return
        except (OSError, http.client.HTTPException) as exc:
            raise HermesError(f"lost the event stream ({exc})") from exc
        finally:
            conn.close()

    def approve(self, run_id: str, choice: str) -> Dict[str, Any]:
        if choice not in ("once", "session", "deny"):
            raise ValueError("Argus never grants permanent approvals")
        status, data = self._request("POST", f"/v1/runs/{run_id}/approval", {"choice": choice})
        if status != 200:
            raise HermesError(f"approval not accepted: {self._error_text(data)}", status)
        return data

    def stop(self, run_id: str) -> None:
        try:
            self._request("POST", f"/v1/runs/{run_id}/stop", {}, timeout=10)
        except HermesError:
            pass

    def chat(self, content: Union[str, List[Dict[str, Any]]], *, system: Optional[str] = None,
             session_id: Optional[str] = None, timeout: float = 600.0) -> str:
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        body: Dict[str, Any] = {"messages": messages, "stream": False}
        if self.model:
            body["model"] = self.model
        headers = {"X-Hermes-Session-Id": session_id} if session_id else None
        status, data = self._request("POST", "/v1/chat/completions", body, timeout=timeout, headers=headers)
        if status != 200:
            raise HermesError(f"Hermes refused the request: {self._error_text(data)}", status)
        try:
            return str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise HermesError("Hermes sent an answer Argus couldn't read") from exc


