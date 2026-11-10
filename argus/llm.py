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
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

API_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int = 0, kind: str = "", retryable: bool = False,
                 partial: bool = False):
        super().__init__(message)
        self.status, self.kind, self.retryable = status, kind, retryable
        self.partial = partial      # part of the reply had already arrived (and may have been spoken)


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


def bypasses_proxy(host: str, no_proxy: str) -> bool:
    """NO_PROXY rules: "*" means everything; "example.com" covers it and its subdomains (not "notexample.com")."""
    host = (host or "").lower().rstrip(".")
    for entry in no_proxy.split(","):
        entry = entry.strip().lower().lstrip(".").split(":")[0]
        if entry and (entry == "*" or host == entry or host.endswith("." + entry)):
            return True
    return False


def connect(url: str, timeout: float) -> http.client.HTTPConnection:
    """HTTP(S) connection to `url`'s host, through HTTPS_PROXY if one is set."""
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port
    if parts.scheme == "http":
        return http.client.HTTPConnection(host, port or 80, timeout=timeout)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if proxy and not bypasses_proxy(host, no_proxy):
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
    """The Messages API, tuned for a voice assistant, where every second of waiting is heard:

    * Connections stay open and are reused, so a request doesn't pay for a new TLS handshake, and warm()
      opens one in advance while you're still talking (nothing is sent until there's a request).
    * Separate, short time limits for connecting, for the reply to start, and for silence in the middle
      of a reply, so a stuck request fails in seconds instead of minutes.
    * Anything that fails before the reply has started (no connection, a timeout, an overloaded or
      rate-limited API) is retried automatically: after 0.5, 1 and 2 seconds, or after the API's own
      Retry-After when that's short. A reply that breaks off halfway raises LLMError(partial=True), so
      the caller can pick up where it stopped instead of starting over.
    """

    MAX_IDLE = 2              # open connections kept for reuse
    IDLE_MAX_AGE = 45.0       # seconds; servers close idle connections, so older ones aren't trusted
    MAX_WAIT = 20.0           # never sit silent longer than this for a Retry-After

    def __init__(self, api_key: str, *, base_url: str = API_BASE, max_retries: int = 3,
                 connect_timeout: float = 5.0, first_byte_timeout: float = 60.0, stall_timeout: float = 30.0,
                 timeout: Optional[float] = None):
        if not api_key:
            raise ValueError("The Claude API key is missing (run ./setup.sh, or put it in ~/.argus/secrets.env)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        if timeout is not None:          # one limit for everything (handy in tests)
            connect_timeout = first_byte_timeout = stall_timeout = timeout
        self.connect_timeout, self.first_byte_timeout, self.stall_timeout = connect_timeout, first_byte_timeout, stall_timeout
        self._idle: List[Tuple[float, http.client.HTTPConnection]] = []
        self._pool_lock = threading.Lock()
        self._warming = False
        self._last_warm = float("-inf")
        self.connections_opened = 0      # for tests and `argusctl doctor`

    def _headers(self, stream: bool) -> Dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": API_VERSION, "content-type": "application/json",
                "accept": "text/event-stream" if stream else "application/json", "user-agent": "argus/1.0"}

    # ---- connections -------------------------------------------------------------------------------
    def _new(self) -> http.client.HTTPConnection:
        self.connections_opened += 1
        return connect(self.base_url, self.connect_timeout)

    def _take(self) -> Tuple[http.client.HTTPConnection, bool]:
        """A connection to send on: a recently used open one if there is one (reused=True), else a new one."""
        now = time.monotonic()
        with self._pool_lock:
            while self._idle:
                since, conn = self._idle.pop()
                if now - since <= self.IDLE_MAX_AGE:
                    return conn, True
                conn.close()
        return self._new(), False

    def _give_back(self, conn: http.client.HTTPConnection) -> None:
        with self._pool_lock:
            if conn.sock is not None and len(self._idle) < self.MAX_IDLE:
                self._idle.append((time.monotonic(), conn))
                return
        conn.close()

    def warm(self) -> None:
        """Open a connection in the background (TCP and TLS only; nothing is sent), so the next request
        starts at once. Cheap to call often: it does nothing if a fresh connection is already waiting."""
        now = time.monotonic()
        with self._pool_lock:
            fresh = any(now - since < self.IDLE_MAX_AGE - 10 for since, _ in self._idle)
            if fresh or self._warming or now - self._last_warm < 10:
                return
            self._warming, self._last_warm = True, now

        def run():
            try:
                conn = self._new()
                conn.connect()
                self._give_back(conn)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                with self._pool_lock:
                    self._warming = False
        threading.Thread(target=run, name="argus-warm", daemon=True).start()

    def close(self) -> None:
        with self._pool_lock:
            idle, self._idle = self._idle, []
        for _, conn in idle:
            conn.close()

    @staticmethod
    def _watch(conn: http.client.HTTPConnection, cancel: Optional[threading.Event]) -> threading.Event:
        """While the returned event is unset, setting `cancel` cuts the connection at once, so "Argus, stop"
        never waits for a slow server. (Shuts the socket down underneath TLS; the waiting read just ends.)"""
        done = threading.Event()
        if cancel is None:
            return done

        def run():
            while not done.is_set():
                if cancel.wait(0.1):
                    sock = conn.sock
                    if sock is not None and not done.is_set():
                        try:
                            socket.socket.shutdown(sock, socket.SHUT_RDWR)
                        except OSError:
                            pass
                    return
        threading.Thread(target=run, name="argus-cancel-watch", daemon=True).start()
        return done

    def _pause(self, attempt: int, retry_after: Optional[str] = None) -> bool:
        """Sleep before retry number `attempt` (1, 2, ...). False if the API asks for too long a pause."""
        wait = min(0.5 * 2 ** (attempt - 1), 8.0)
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                pass
        if wait > self.MAX_WAIT:
            return False
        time.sleep(wait + random.random() * 0.25)
        return True

    # ---- requests ----------------------------------------------------------------------------------
    def _open(self, body: Dict[str, Any], stream: bool, cancel: Optional[threading.Event] = None):
        """POST /v1/messages until the reply starts. Returns (conn, response, watching): status 200, and the
        cancel watch, which the caller sets once it's done with the connection."""
        data = json.dumps(body).encode()
        attempt = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise LLMError("cancelled", kind="cancelled")
            conn, reused = self._take()
            watching = self._watch(conn, cancel)
            retry_after = None
            try:
                if conn.sock is None:
                    conn.connect()
                conn.sock.settimeout(self.first_byte_timeout)
                conn.request("POST", "/v1/messages", body=data, headers=self._headers(stream))
                resp = conn.getresponse()
            except (OSError, ValueError, http.client.HTTPException) as exc:
                watching.set()
                conn.close()
                if cancel is not None and cancel.is_set():
                    raise LLMError("cancelled", kind="cancelled") from exc
                if reused:          # the server had closed the idle connection: open a new one straight away
                    continue
                error = LLMError(f"couldn't reach the Claude API ({exc or type(exc).__name__})", retryable=True)
            else:
                if resp.status == 200:
                    return conn, resp, watching
                watching.set()
                try:
                    raw_error = resp.read()
                except (OSError, http.client.HTTPException):
                    raw_error, resp.will_close = b"", True
                error = _error_from(resp.status, raw_error)
                retry_after = resp.getheader("retry-after")
                if resp.will_close:
                    conn.close()
                else:
                    self._give_back(conn)
            attempt += 1
            if not error.retryable or attempt > self.max_retries or not self._pause(attempt, retry_after):
                raise error

    def create(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """One whole reply (no streaming). Retried, like a stream that fails before it starts."""
        attempt = 0
        while True:
            conn, resp, _ = self._open(dict(body, stream=False), stream=False)
            try:
                raw = resp.read()
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                error = LLMError(f"lost the connection to the Claude API ({exc or type(exc).__name__})",
                                 retryable=True)
                attempt += 1
                if attempt > self.max_retries or not self._pause(attempt):
                    raise error from exc
                continue
            if resp.will_close:
                conn.close()
            else:
                self._give_back(conn)
            try:
                reply = json.loads(raw)
            except ValueError as exc:
                raise LLMError("the Claude API sent a reply Argus couldn't read", retryable=False) from exc
            if not isinstance(reply, dict):
                raise LLMError("the Claude API sent a reply Argus couldn't read", retryable=False)
            return reply

    def stream(self, body: Dict[str, Any], cancel: Optional[threading.Event] = None) -> Iterator[Dict[str, Any]]:
        """Yields the stream's events as dicts; stops early (without error) if `cancel` is set. A failure
        before the first event is retried here; after that it raises LLMError(partial=True)."""
        attempt = 0
        while True:
            try:
                conn, resp, watching = self._open(dict(body, stream=True), stream=True, cancel=cancel)
            except LLMError:
                if cancel is not None and cancel.is_set():
                    return
                raise
            started = reusable = False
            try:
                for event in self._events(resp, cancel):   # until the first event: first_byte_timeout
                    if not started and conn.sock is not None:
                        conn.sock.settimeout(self.stall_timeout)   # then: how long a pause may last
                    started = True
                    yield event
                    if event.get("type") == "message_stop":
                        reusable = self._drain(conn, resp)
                return
            except LLMError as exc:
                if started:
                    exc.partial = True
                    raise
                attempt += 1
                if not exc.retryable or attempt > self.max_retries or not self._pause(attempt):
                    raise
            finally:
                watching.set()
                if reusable:
                    self._give_back(conn)
                else:
                    conn.close()

    def _events(self, resp, cancel: Optional[threading.Event]) -> Iterator[Dict[str, Any]]:
        data_lines: List[str] = []
        while True:
            if cancel is not None and cancel.is_set():
                return
            try:
                raw = resp.readline()
            except (socket.timeout, TimeoutError) as exc:
                if cancel is not None and cancel.is_set():
                    return
                raise LLMError("the Claude API went quiet for too long", kind="stalled", retryable=True) from exc
            except (OSError, ValueError, http.client.HTTPException) as exc:
                if cancel is not None and cancel.is_set():
                    return
                raise LLMError(f"lost the connection to the Claude API ({exc or type(exc).__name__})",
                               retryable=True) from exc
            if cancel is not None and cancel.is_set():
                return
            if not raw:
                raise LLMError("the reply from the Claude API was cut off", kind="cut_off", retryable=True)
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
                    kind = str(err.get("type") or "")
                    raise LLMError(f"Claude API error: {err.get('message') or kind}", 0, kind,
                                   retryable=kind in ("overloaded_error", "api_error", "rate_limit_error"))
                yield event
                if event.get("type") == "message_stop":
                    return

    def _drain(self, conn: http.client.HTTPConnection, resp) -> bool:
        """Read what's left of a finished reply (the end of the body), so the connection can be reused."""
        try:
            if conn.sock is not None:
                conn.sock.settimeout(2.0)
            resp.read()
            return not resp.will_close
        except (OSError, http.client.HTTPException):
            return False


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
                if not isinstance(block.get("citations"), list):
                    block["citations"] = []
                block["citations"].append(delta.get("citation"))
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
