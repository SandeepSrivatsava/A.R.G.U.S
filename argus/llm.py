"""Claude Messages API client, written for Argus (no SDK): streaming over SSE, retries, TLS that
trusts the macOS keychain, and an accumulator that rebuilds the assistant message from the stream.
"""
from __future__ import annotations

import http.client
import json
import os
import random
import socket
import ssl
import threading
import time
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlsplit

API_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int = 0, kind: str = "", retryable: bool = False):
        super().__init__(message)
        self.status, self.kind, self.retryable = status, kind, retryable


def ssl_context() -> ssl.SSLContext:
    """The OS trust store when possible (truststore), else certifi's bundle, else Python's default."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def connect(url: str, timeout: float) -> http.client.HTTPConnection:
    """HTTP(S) connection to `url`'s host, through HTTPS_PROXY if one is set."""
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port
    if parts.scheme == "http":
        return http.client.HTTPConnection(host, port or 80, timeout=timeout)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if proxy and not any(host.endswith(d.strip().lstrip(".")) for d in no_proxy.split(",") if d.strip()):
        p = urlsplit(proxy if "//" in proxy else "http://" + proxy)
        conn = http.client.HTTPSConnection(p.hostname, p.port or 8080, timeout=timeout, context=ssl_context())
        conn.set_tunnel(host, port or 443)
        return conn
    return http.client.HTTPSConnection(host, port or 443, timeout=timeout, context=ssl_context())


def _error_from(status: int, raw: bytes) -> LLMError:
    try:
        err = json.loads(raw).get("error") or {}
        kind, message = str(err.get("type") or ""), str(err.get("message") or "")
    except (ValueError, AttributeError):
        kind, message = "", raw[:300].decode("utf-8", "replace")
    if status == 401:
        message = "the Claude API key was rejected (check it at platform.claude.com)"
    elif status == 402 or "credit" in message.lower():
        message = "the Claude API account is out of credit"
    return LLMError(f"Claude API error {status}: {message or kind}", status, kind,
                    retryable=status in RETRYABLE or kind == "overloaded_error")


class Claude:
    def __init__(self, api_key: str, *, base_url: str = API_BASE, timeout: float = 600.0, max_retries: int = 3):
        if not api_key:
            raise ValueError("The Claude API key is missing (run ./setup.sh, or put it in ~/.argus/secrets.env)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def _headers(self, stream: bool) -> Dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": API_VERSION, "content-type": "application/json",
                "accept": "text/event-stream" if stream else "application/json", "user-agent": "argus/1.0"}

    def _open(self, body: Dict[str, Any], stream: bool):
        """POST /v1/messages with retries for overloads and network errors. Returns (conn, response)."""
        data = json.dumps(body).encode()
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            conn = connect(self.base_url, self.timeout)
            try:
                conn.request("POST", "/v1/messages", body=data, headers=self._headers(stream))
                resp = conn.getresponse()
                if resp.status == 200:
                    return conn, resp
                error = _error_from(resp.status, resp.read())
                retry_after = resp.getheader("retry-after")
                conn.close()
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                error = LLMError(f"couldn't reach the Claude API ({exc})", retryable=True)
                retry_after = None
            if not error.retryable or attempt == self.max_retries:
                raise error
            try:
                wait = float(retry_after) if retry_after else delay
            except ValueError:
                wait = delay
            time.sleep(min(wait, 30) + random.random() * 0.25)
            delay *= 2
        raise LLMError("unreachable")  # pragma: no cover

    def create(self, body: Dict[str, Any]) -> Dict[str, Any]:
        conn, resp = self._open(dict(body, stream=False), stream=False)
        try:
            return json.loads(resp.read())
        finally:
            conn.close()

    def stream(self, body: Dict[str, Any], cancel: Optional[threading.Event] = None) -> Iterator[Dict[str, Any]]:
        """Yields the stream's events as dicts. Stops early (without error) if `cancel` is set."""
        conn, resp = self._open(dict(body, stream=True), stream=True)
        try:
            data_lines: List[str] = []
            while True:
                if cancel is not None and cancel.is_set():
                    return
                try:
                    raw = resp.readline()
                except socket.timeout as exc:
                    raise LLMError("the Claude API went quiet for too long", retryable=False) from exc
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif line == "" and data_lines:
                    payload, data_lines = "\n".join(data_lines), []
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    if event.get("type") == "error":
                        err = event.get("error") or {}
                        raise LLMError(f"Claude API error: {err.get('message') or err.get('type')}", 0,
                                       str(err.get("type") or ""), retryable=err.get("type") == "overloaded_error")
                    yield event
                    if event.get("type") == "message_stop":
                        return
        except (OSError, http.client.HTTPException) as exc:
            raise LLMError(f"lost the connection to the Claude API ({exc})") from exc
        finally:
            conn.close()


class StreamAccumulator:
    """Rebuilds the assistant message from stream events, block by block, exactly as the API sent it
    (tool_use with toolset_name, server_tool_use, web_search_tool_result with encrypted_content, ...)."""

    def __init__(self):
        self.blocks: Dict[int, Dict[str, Any]] = {}
        self._json: Dict[int, str] = {}
        self.stop_reason: Optional[str] = None
        self.usage: Dict[str, int] = {}
        self.message_id = ""

    def feed(self, event: Dict[str, Any]) -> Optional[str]:
        """Apply one event; returns new visible text, if any."""
        kind = event.get("type")
        if kind == "message_start":
            msg = event.get("message") or {}
            self.message_id = msg.get("id", "")
            self._add_usage(msg.get("usage") or {})
        elif kind == "content_block_start":
            index = int(event["index"])
            block = dict(event.get("content_block") or {})
            if block.get("type") in ("tool_use", "server_tool_use"):
                self._json[index] = ""
            self.blocks[index] = block
        elif kind == "content_block_delta":
            index = int(event["index"])
            delta = event.get("delta") or {}
            block = self.blocks.setdefault(index, {"type": "text", "text": ""})
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
                return delta.get("text", "")
            if dtype == "input_json_delta":
                self._json[index] = self._json.get(index, "") + delta.get("partial_json", "")
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif dtype == "signature_delta":
                block["signature"] = delta.get("signature", "")
            elif dtype == "citations_delta":
                block.setdefault("citations", []).append(delta.get("citation"))
        elif kind == "content_block_stop":
            index = int(event["index"])
            if index in self._json:
                raw = self._json.pop(index)
                try:
                    self.blocks[index]["input"] = json.loads(raw) if raw.strip() else {}
                except ValueError:  # malformed arguments: the tool reports a clear error instead
                    self.blocks[index]["input"] = {}
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                self.stop_reason = delta["stop_reason"]
            self._add_usage(event.get("usage") or {})
        return None

    def _add_usage(self, usage: Dict[str, Any]) -> None:
        flat = dict(usage)
        searches = (usage.get("server_tool_use") or {}).get("web_search_requests")
        if isinstance(searches, int):
            flat["web_search_requests"] = searches
        for key, value in flat.items():
            if isinstance(value, int) and not isinstance(value, bool):
                self.usage[key] = max(self.usage.get(key, 0), value)  # stream usage counts are cumulative

    @property
    def content(self) -> List[Dict[str, Any]]:
        return [self.blocks[i] for i in sorted(self.blocks)]

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    def tool_uses(self) -> List[Dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]
