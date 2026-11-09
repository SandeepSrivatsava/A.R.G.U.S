#!/usr/bin/env python3
"""A tiny stand-in for an OpenAI-compatible model server, used only by tests/e2e_hermes.sh.

It lets the end-to-end test drive Hermes without a real API key:
- a user message mentioning "headline" or "news" gets a terminal tool call that runs the news script on a
  saved feed; when the tool's output comes back the mock answers "Top story: <first headline>";
- "delete" gets a dangerous terminal command (rm -rf on a test folder), which Hermes must hold for approval;
- "bank" gets a web_extract call to https://www.mybank.com, which Argus's privacy hook must block;
- anything else gets a short fixed reply.
Every request body is appended to the log file so the test can check what Hermes sent.

Usage: mock_llm.py PORT LOGFILE NEWS_SCRIPT FIXTURE
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG_PATH, NEWS_SCRIPT, FIXTURE = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
DANGER_DIR = sys.argv[5] if len(sys.argv) > 5 else "/tmp/argus-e2e-precious"
MODEL = "mock-model"


def text_of(message):
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def decide(body):
    messages = body.get("messages") or []
    tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        result = text_of(last)
        if "privacy rule" in result:
            return {"content": "I can't open that: it's blocked by your privacy rules."}
        if re.search(r"denied|blocked|not approved", result, re.I):
            return {"content": "Okay, I didn't delete anything."}
        match = re.search(r'"title":\s*"([^"]+)"', result.replace('\\"', '"'))
        return {"content": "Top story: " + (match.group(1) if match else "no headline found")}
    said = text_of(last).lower() if last.get("role") == "user" else ""
    if ("headline" in said or "news" in said) and "terminal" in tools:
        command = f'python3 "{NEWS_SCRIPT}" --offline "{FIXTURE}" --format json --top 2'
        return {"tool_call": {"name": "terminal", "arguments": json.dumps({"command": command})}}
    if "delete" in said and "terminal" in tools:
        return {"tool_call": {"name": "terminal", "arguments": json.dumps({"command": f"rm -rf {DANGER_DIR}"})}}
    if "bank" in said and "web_extract" in tools:
        return {"tool_call": {"name": "web_extract", "arguments": json.dumps({"urls": ["https://www.mybank.com/statement"]})}}
    return {"content": "Argus mock reply."}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "context_length": 131072}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        with open(LOG_PATH, "a", encoding="utf-8") as log:
            log.write(json.dumps({"path": self.path, "body": body}) + "\n")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        answer = decide(body)
        created, cid = int(time.time()), f"chatcmpl-{time.time_ns()}"
        if "tool_call" in answer:
            call = {"id": "call_1", "type": "function", "function": answer["tool_call"]}
            message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": answer["content"]}, "stop"
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        if not body.get("stream"):
            self._json(200, {"id": cid, "object": "chat.completion", "created": created, "model": MODEL,
                             "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def send(chunk):
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()

        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL}
        if finish == "tool_calls":
            delta = {"role": "assistant", "tool_calls": [dict(call, index=0)]}
        else:
            delta = {"role": "assistant", "content": message["content"]}
        send(dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]))
        send(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": finish}]))
        if (body.get("stream_options") or {}).get("include_usage"):
            send(dict(base, choices=[], usage=usage))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
