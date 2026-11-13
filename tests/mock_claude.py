#!/usr/bin/env python3
"""A stand-in for the Claude Messages API (streaming SSE and plain JSON), for Argus's tests.

In-process: ClaudeMock(responder) where responder(body) returns (blocks, stop_reason).
As a script (used by tests/e2e.sh): python3 mock_claude.py PORT LOGFILE  -- with the rule-based responder below:
  * "news"/"headline" -> calls get_news, then answers "Top story: <first title>"
  * "delete"          -> run_command "rm -rf <DANGER_DIR>" (must be held for approval)
  * "bank"            -> fetch_page https://www.mybank.com/statement (must be blocked by the privacy rules)
  * "remember"        -> remember tool
  * "fastest way"     -> route tool to Portland Airport (from the phone's location)
  * anything else     -> "Argus mock reply."
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Tuple

Blocks = List[Dict[str, Any]]


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                out.append(text_of(b.get("content")))
    return " ".join(out)


def rules(body: Dict[str, Any]) -> Tuple[Blocks, str]:
    messages = body.get("messages") or []
    tools = [t.get("name") for t in body.get("tools") or []]
    last = messages[-1] if messages else {"content": ""}
    content = last.get("content")
    is_result = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    said = text_of(content).lower()
    if is_result:
        if "argus rules" in said:
            return [{"type": "text", "text": "I can't open that: it's blocked by your privacy rules."}], "end_turn"
        if "declined" in said:
            return [{"type": "text", "text": "Okay, I didn't delete anything."}], "end_turn"
        if "navigation link" in said:
            return [{"type": "text", "text": "Opening the route to the airport in Maps."}], "end_turn"
        m = re.search(r'"title":\s*"([^"]+)"', text_of(content))
        if m:
            return [{"type": "text", "text": "Top story: " + m.group(1)}], "end_turn"
        return [{"type": "text", "text": "Done."}], "end_turn"
    if ("news" in said or "headline" in said) and "get_news" in tools:
        return [{"type": "text", "text": "Let me check. "},
                {"type": "tool_use", "name": "get_news", "input": {"count": 2}}], "tool_use"
    if "delete" in said and "run_command" in tools:
        target = os.environ.get("DANGER_DIR", "/tmp/argus-e2e-precious")
        return [{"type": "tool_use", "name": "run_command", "input": {"command": f"rm -rf {target}"}}], "tool_use"
    if "bank" in said and "fetch_page" in tools:
        return [{"type": "tool_use", "name": "fetch_page", "input": {"url": "https://www.mybank.com/statement"}}], "tool_use"
    if "fastest way" in said and "route" in tools:
        return [{"type": "tool_use", "name": "route", "input": {"destination": "Portland Airport, OR"}}], "tool_use"
    if "remember" in said and "remember" in tools:
        return [{"type": "tool_use", "name": "remember", "input": {"fact": said[:80]}}], "tool_use"
    return [{"type": "text", "text": "Argus mock reply."}], "end_turn"


BLOCK_TYPES = {"text", "image", "tool_use", "tool_result", "server_tool_use", "web_search_tool_result",
               "bash_code_execution_tool_result", "text_editor_code_execution_tool_result",
               "thinking", "redacted_thinking", "document"}


def validate(body: Dict[str, Any]) -> str:
    """The checks the real Messages API makes that Argus could get wrong. Returns "" when the request is fine."""
    if not body.get("model") or not isinstance(body.get("max_tokens"), int):
        return "model and max_tokens are required"
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages: at least one message is required"
    if messages[0].get("role") != "user":
        return "messages: the first message must use the user role"
    output_config = body.get("output_config")
    effort = None
    if output_config is not None:
        effort = output_config.get("effort") if isinstance(output_config, dict) else None
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            return "output_config.effort: must be low, medium, high, xhigh or max"
    thinking = body.get("thinking")
    if thinking is not None:
        kind = thinking.get("type") if isinstance(thinking, dict) else None
        if kind not in ("adaptive", "between_tools", "disabled", "enabled"):
            return "thinking.type: unknown"
        sonnet55 = str(body["model"]).startswith("claude-sonnet-5-5")
        if kind == "disabled" and sonnet55:
            return "thinking: on claude-sonnet-5-5 use {\"type\": \"between_tools\"} instead of disabled"
        if kind == "between_tools" and (not sonnet55 or len(thinking) > 1 or effort in ("xhigh", "max")):
            return "thinking: between_tools needs claude-sonnet-5-5, no other fields, and effort high or below"
    last_question = max((i for i, m in enumerate(body["messages"]) if m.get("role") == "user" and (
        isinstance(m.get("content"), str) or not any(isinstance(b, dict) and b.get("type") == "tool_result"
                                                     for b in m["content"]))), default=0)
    for m in body["messages"][:last_question]:     # Argus never replays thinking from an earlier request
        if m.get("role") == "assistant" and isinstance(m.get("content"), list) and any(
                isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking") for b in m["content"]):
            return "thinking blocks from an earlier request were replayed after the system prompt changed"
    breakpoints = 0
    for block in (body.get("system") or []) if isinstance(body.get("system"), list) else []:
        breakpoints += "cache_control" in block
    names = set()
    for tool in body.get("tools") or []:
        breakpoints += "cache_control" in tool
        name = tool.get("name") or tool.get("type")
        if not name:
            return "tools: each tool needs a name or a type"
        if name in names:
            return f"tools: duplicate tool {name}"
        names.add(name)
        if "name" in tool and "type" not in tool and not isinstance(tool.get("input_schema"), dict):
            return f"tools.{name}: input_schema is required"
    pending: Dict[str, str] = {}
    for i, m in enumerate(messages):
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant"):
            return f"messages.{i}: unknown role {role!r}"
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(blocks, list) or not blocks:
            return f"messages.{i}: content must be a non-empty string or list"
        for j, b in enumerate(blocks):
            if not isinstance(b, dict) or b.get("type") not in BLOCK_TYPES:
                return f"messages.{i}.content.{j}: unknown block"
            breakpoints += "cache_control" in b
            if b["type"] == "text" and not str(b.get("text") or "").strip():
                return f"messages.{i}.content.{j}: text content blocks must be non-empty"
        if pending:   # the previous assistant turn called tools: this turn must answer every one of them first
            results = [b for b in blocks if b.get("type") == "tool_result"]
            if role != "user" or blocks[:len(results)] != results:
                return f"messages.{i}: tool_result blocks must come first, right after the tool_use blocks"
            got = {b.get("tool_use_id") for b in results}
            if got != set(pending):
                return f"messages.{i}: tool_use ids without tool_result: {sorted(set(pending) - got)}"
            for b in results:
                if b.get("toolset_name", "") != pending[b["tool_use_id"]]:
                    return f"messages.{i}: tool_result must echo toolset_name {pending[b['tool_use_id']]!r}"
            pending = {}
        elif any(b.get("type") == "tool_result" for b in blocks):
            return f"messages.{i}: tool_result without a matching tool_use"
        if role == "assistant":
            pending = {b["id"]: b.get("toolset_name", "") for b in blocks if b.get("type") == "tool_use"}
    if breakpoints > 4:
        return f"a maximum of 4 blocks with cache_control may be provided. Found {breakpoints}."
    return ""


class ClaudeMock(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, responder: Callable[[Dict[str, Any]], Tuple[Blocks, str]] = rules, port: int = 0,
                 log_path: str = "", key: str = "sk-ant-test"):
        super().__init__(("127.0.0.1", port), _Handler)
        self.responder = responder
        self.key = key
        self.requests: List[Dict[str, Any]] = []
        self.log_path = log_path
        self.fail_next: List[int] = []      # HTTP statuses to return before succeeding (retry tests)
        self.invalid: List[str] = []        # requests the real API would have rejected (should stay empty)
        self._ids = 0
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def handle_error(self, request, client_address):  # a client that hung up mid-stream (cancel) is fine
        pass

    def start(self) -> "ClaudeMock":
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def next_id(self, prefix: str) -> str:
        with self._lock:
            self._ids += 1
            return f"{prefix}_{self._ids:04d}"


class _Handler(BaseHTTPRequestHandler):
    server: ClaudeMock

    def log_message(self, *a):
        pass

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path != "/v1/messages":
            return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "no"}})
        if self.headers.get("x-api-key") != self.server.key or not self.headers.get("anthropic-version"):
            return self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        if self.server.fail_next:
            status = self.server.fail_next.pop(0)
            return self._json(status, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
        problem = validate(body)
        if problem:
            self.server.invalid.append(problem)
            return self._json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": problem}})
        self.server.requests.append(body)
        if self.server.log_path:
            with open(self.server.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(body) + "\n")
        blocks, stop = self.server.responder(body)
        blocks = [dict(b, id=b.get("id") or self.server.next_id("toolu" if b["type"] == "tool_use" else "srvtoolu"))
                  if b["type"] in ("tool_use", "server_tool_use") else b for b in blocks]
        usage = {"input_tokens": 1000, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        msg_id = self.server.next_id("msg")
        if not body.get("stream"):
            return self._json(200, {"id": msg_id, "type": "message", "role": "assistant", "model": body.get("model"),
                                    "content": blocks, "stop_reason": stop, "usage": usage})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(event: Dict[str, Any]) -> None:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

        send({"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [],
                                                   "model": body.get("model"), "stop_reason": None,
                                                   "usage": {"input_tokens": 1000, "output_tokens": 1}}})
        self.wfile.write(b"event: ping\ndata: {\"type\": \"ping\"}\n\n")
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                send({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
                words = re.findall(r"\S+\s*", b["text"]) or [b["text"]]
                for w in words:
                    send({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": w}})
            elif b["type"] in ("tool_use", "server_tool_use"):
                start = {k: v for k, v in b.items() if k != "input"}
                send({"type": "content_block_start", "index": i, "content_block": dict(start, input={})})
                raw = json.dumps(b.get("input") or {})
                for j in range(0, len(raw), 7):
                    send({"type": "content_block_delta", "index": i,
                          "delta": {"type": "input_json_delta", "partial_json": raw[j:j + 7]}})
            else:
                send({"type": "content_block_start", "index": i, "content_block": b})
            send({"type": "content_block_stop", "index": i})
        send({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
              "usage": {"output_tokens": 50}})
        send({"type": "message_stop"})


if __name__ == "__main__":
    server = ClaudeMock(rules, port=int(sys.argv[1]), log_path=sys.argv[2] if len(sys.argv) > 2 else "",
                        key=os.environ.get("MOCK_KEY", "sk-ant-test"))
    print(f"mock Claude on {server.url}", flush=True)
    server.serve_forever()
