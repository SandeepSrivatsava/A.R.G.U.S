"""Handing work to a Paperclip server (github.com/paperclipai/paperclip, MIT), when you run one.

Paperclip runs a "company" of AI agents with an org chart, tickets, budgets and approvals, and has its own
dashboard (port 3100). Argus can put a job on its board as a ticket, assign it to one of its agents, and read
back how it's going. Argus's own crew (crew.py) does the same kind of work without a Paperclip server; this is
for when you want the job on Paperclip.

Set up: run Paperclip (`npx paperclipai@latest onboard --yes`), make a board API key in its settings, then
`argusctl paperclip setup` (asks for the address, the company and the key; the key goes into secrets.env).

Safety: the server must be on this Mac or reached over Tailscale (localhost, 100.64.0.0/10, *.ts.net): a job
never goes to an internet address. Only the ticket's title and description are sent. The key is never in a
command line or in what Claude sees.
API (docs.paperclip.ing / docs/api/issues.md): Bearer token; POST /api/companies/{company}/issues;
GET /api/issues/{id}; GET /api/issues/{id}/comments; GET /api/companies/{company}/agents; GET /api/health.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("argus.paperclip")
TIMEOUT = 15


class PaperclipError(Exception):
    pass


def allowed_url(url: str) -> bool:
    """Only this Mac or Tailscale: localhost, 127/8, ::1, 100.64.0.0/10, or a *.ts.net name."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")


class Paperclip:
    def __init__(self, url: str, company: str, key: str, opener: Optional[Callable[..., Any]] = None):
        if not allowed_url(url):
            raise PaperclipError("the Paperclip address must be on this Mac or on Tailscale "
                                 "(e.g. http://localhost:3100)")
        self.url, self.company, self.key = url.rstrip("/"), company, key
        self.opener = opener or urllib.request.urlopen

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.key:
            req.add_header("Authorization", f"Bearer {self.key}")
        try:
            with self.opener(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise PaperclipError(f"Paperclip answered {exc.code} for {method} {path}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PaperclipError(f"couldn't reach Paperclip at {self.url} ({getattr(exc, 'reason', exc)}); "
                                 "is it running?") from exc
        try:
            return json.loads(raw or b"null")
        except ValueError as exc:
            raise PaperclipError("Paperclip sent back something that isn't JSON") from exc

    def health(self) -> bool:
        return self._call("GET", "/api/health") is not None

    def agents(self) -> List[Dict[str, Any]]:
        out = self._call("GET", f"/api/companies/{self.company}/agents")
        return list(out.get("agents", out.get("items", [])) if isinstance(out, dict) else out or [])

    def find_agent(self, name: str) -> Optional[Dict[str, Any]]:
        name = name.strip().lower()
        for a in self.agents():
            fields = [str(a.get(k) or "").lower() for k in ("id", "name", "urlKey", "shortname", "role", "title")]
            if name in fields:
                return a
        for a in self.agents():
            if any(name in str(a.get(k) or "").lower() for k in ("name", "role", "title")):
                return a
        return None

    def create(self, title: str, description: str, agent: str = "", priority: str = "medium") -> Dict[str, Any]:
        body: Dict[str, Any] = {"title": title[:200], "description": description[:8000], "status": "todo",
                                "priority": priority if priority in ("critical", "high", "medium", "low") else "medium"}
        if agent:
            found = self.find_agent(agent)
            if found is None:
                raise PaperclipError(f"no agent called {agent} in that Paperclip company")
            body["assigneeAgentId"] = found.get("id")
        try:
            return self._call("POST", f"/api/companies/{self.company}/issues", body)
        except PaperclipError as exc:
            # Paperclip can answer 500 after saving the ticket (their issue #3310): look for it before giving up
            if "answered 500" not in str(exc):
                raise
            for issue in self._call("GET", f"/api/companies/{self.company}/issues") or []:
                if isinstance(issue, dict) and issue.get("title") == body["title"]:
                    return issue
            raise

    def issue(self, issue_id: str) -> Dict[str, Any]:
        return self._call("GET", f"/api/issues/{urllib.parse.quote(issue_id)}")

    def comments(self, issue_id: str) -> List[Dict[str, Any]]:
        out = self._call("GET", f"/api/issues/{urllib.parse.quote(issue_id)}/comments")
        return list(out.get("comments", []) if isinstance(out, dict) else out or [])


def describe(issue: Dict[str, Any], comments: List[Dict[str, Any]]) -> str:
    name = issue.get("identifier") or issue.get("id")
    lines = [f"{name}: {issue.get('title')} [{issue.get('status')}]"]
    for c in comments[-5:]:
        body = " ".join(str(c.get("body") or "").split())[:400]
        if body:
            lines.append(f"- {body}")
    return "\n".join(lines)


DESCRIPTION = (
    "Put a job on the user's Paperclip board (their own AI-agent company server) and check on it. Only when the "
    "user asks for Paperclip; otherwise big jobs go to the crew tool. action 'create' (title, description: the job "
    "in the user's words, agent: an agent's name or role, e.g. 'CEO', priority), 'status' (issue id or identifier), "
    "'agents' (who's in the company).")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
    "action": {"type": "string", "enum": ["create", "status", "agents"]},
    "title": {"type": "string"}, "description": {"type": "string"}, "agent": {"type": "string"},
    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]}, "issue": {"type": "string"}}}


def attach(settings, tools: Dict[str, Any], key: str, opener=None) -> Optional[Paperclip]:
    url, company = getattr(settings, "paperclip_url", ""), getattr(settings, "paperclip_company", "")
    if not url or not company:
        return None
    try:
        client = Paperclip(url, company, key, opener=opener)
    except PaperclipError as exc:
        log.warning("Paperclip is off: %s", exc)
        return None
    from .toolbox import Tool, ToolOutput

    def run(args: Dict[str, Any], ctx) -> ToolOutput:
        action = args.get("action")
        try:
            if action == "create":
                title = " ".join(str(args.get("title") or "").split())
                if not title:
                    return ToolOutput.error("give the job a title")
                issue = client.create(title, str(args.get("description") or title), str(args.get("agent") or ""),
                                      str(args.get("priority") or "medium"))
                who = f", assigned to {args.get('agent')}" if args.get("agent") else ""
                return ToolOutput(f"On the Paperclip board as {issue.get('identifier') or issue.get('id')}{who}.")
            if action == "status":
                ref = str(args.get("issue") or "")
                if not ref:
                    return ToolOutput.error("which issue?")
                return ToolOutput(describe(client.issue(ref), client.comments(ref)))
            if action == "agents":
                found = client.agents()
                return ToolOutput("\n".join(f"{a.get('name')} ({a.get('role') or a.get('title') or 'agent'})"
                                            for a in found) or "No agents in that company yet.")
        except PaperclipError as exc:
            return ToolOutput.error(str(exc))
        return ToolOutput.error("unknown action")

    tools["paperclip"] = Tool("paperclip", DESCRIPTION, SCHEMA, run, frozenset({"voice", "phone", "cli", "android"}))
    return client
