"""Argus's tools: what the brain can do besides talking. Each tool is a JSON schema for Claude plus
a Python function. The policy (policy.py) decides before every call whether it may run."""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import urljoin, urlsplit

from . import news as news_mod
from .cron import Cron, CronError
from .llm import connect

Content = Union[str, List[Dict[str, Any]]]
MAX_OUTPUT = 12_000
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


@dataclass
class ToolOutput:
    content: Content
    is_error: bool = False

    @classmethod
    def error(cls, message: str) -> "ToolOutput":
        return cls(message, True)


@dataclass
class Context:
    """What a tool may use while it runs."""
    channel: str
    workspace: Path
    store: Any
    cancel: threading.Event = field(default_factory=threading.Event)
    notify: Callable[[str], bool] = lambda text: False          # send a message to your phone (Telegram)
    glasses: Any = None                                        # bridge to the iPhone app (glasses camera)
    timezone: Optional[str] = None


@dataclass
class Tool:
    name: str
    description: str
    schema: Dict[str, Any]
    run: Callable[[Dict[str, Any], Context], ToolOutput]
    channels: frozenset = frozenset({"voice", "telegram", "phone", "scheduler", "cli"})

    def spec(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def _obj(props: Dict[str, Any], required: List[str] = ()) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n… [{len(text) - limit} characters cut] …\n" + text[-half:]


# ---- terminal ------------------------------------------------------------------------------------------
SAFE_ENV_KEYS = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "SHELL")


def child_env() -> Dict[str, str]:
    """A clean environment for commands: no API keys or tokens ever reach them."""
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    home = env.get("HOME", str(Path.home()))
    env["PATH"] = ":".join([f"{home}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                            "/usr/sbin", "/sbin"])
    env["TERM"] = "dumb"
    env["PAGER"] = "cat"
    return env


def run_command(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    command = str(args.get("command") or "").strip()
    timeout = max(1, min(int(args.get("timeout_seconds") or 60), 600))
    cwd = Path(os.path.expanduser(str(args.get("cwd") or ctx.workspace)))
    if not cwd.is_absolute():
        cwd = ctx.workspace / cwd
    if not cwd.is_dir():
        return ToolOutput.error(f"folder not found: {cwd}")
    shell = "/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/sh"
    proc = subprocess.Popen([shell, "-c", command], cwd=str(cwd), env=child_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + timeout
    out: List[bytes] = []
    reader = threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True)
    reader.start()
    while proc.poll() is None:
        if ctx.cancel.is_set() or time.monotonic() > deadline:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            reader.join(2)
            why = "cancelled" if ctx.cancel.is_set() else f"timed out after {timeout}s"
            return ToolOutput.error(f"command {why}\n{_clip(b''.join(out).decode('utf-8', 'replace'))}")
        time.sleep(0.05)
    reader.join(5)
    text = b"".join(out).decode("utf-8", "replace")
    result = f"exit code {proc.returncode}\n{_clip(text)}" if text else f"exit code {proc.returncode} (no output)"
    return ToolOutput(result, is_error=proc.returncode != 0)


# ---- files -------------------------------------------------------------------------------------------
def _path(ctx: Context, raw: Any) -> Path:
    p = Path(os.path.expanduser(str(raw or ".")))
    return (p if p.is_absolute() else ctx.workspace / p).resolve()


def read_file(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    path = _path(ctx, args.get("path"))
    if not path.is_file():
        return ToolOutput.error(f"no such file: {path}")
    if path.suffix.lower() in IMAGE_TYPES:
        data = path.read_bytes()
        if len(data) > 5 * 1024 * 1024:
            return ToolOutput.error("image is larger than 5 MB")
        return ToolOutput([{"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[path.suffix.lower()],
                                                        "data": base64.b64encode(data).decode()}}])
    data = path.read_bytes()[:2_000_000]
    if b"\0" in data[:4096]:
        return ToolOutput.error("that's a binary file; Argus can read text files and images")
    limit = max(500, min(int(args.get("max_chars") or 40_000), 200_000))
    return ToolOutput(_clip(data.decode("utf-8", "replace"), limit))


def write_file(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    path = _path(ctx, args.get("path"))
    content = str(args.get("content") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.get("append"):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(content)
    else:
        tmp = path.with_name(f".{path.name}.argus-tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    return ToolOutput(f"wrote {len(content)} characters to {path}")


def list_files(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    path = _path(ctx, args.get("path"))
    if not path.is_dir():
        return ToolOutput.error(f"not a folder: {path}")
    entries = []
    for i, entry in enumerate(sorted(path.iterdir(), key=lambda p: p.name.lower())):
        if i >= 300:
            entries.append("… (more not shown)")
            break
        if entry.name.startswith(".") and not args.get("include_hidden"):
            continue
        kind = "/" if entry.is_dir() else f"  {entry.stat().st_size} bytes" if entry.is_file() else ""
        entries.append(f"{entry.name}{kind}")
    return ToolOutput(f"{path}:\n" + "\n".join(entries) if entries else f"{path} is empty")


def search_files(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    root = _path(ctx, args.get("path"))
    try:
        pattern = re.compile(str(args.get("pattern") or ""), re.IGNORECASE)
    except re.error as exc:
        return ToolOutput.error(f"bad search pattern: {exc}")
    hits: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "Library")]
        for name in filenames:
            if len(hits) >= 100 or ctx.cancel.is_set():
                break
            p = Path(dirpath) / name
            if pattern.search(name):
                hits.append(f"{p}  (name matches)")
            try:
                if p.stat().st_size > 1_000_000:
                    continue
                with p.open("r", encoding="utf-8", errors="strict") as fh:
                    for n, line in enumerate(fh, 1):
                        if pattern.search(line):
                            hits.append(f"{p}:{n}: {line.strip()[:200]}")
                            if len(hits) >= 100:
                                break
            except (UnicodeDecodeError, OSError):
                continue
    return ToolOutput("\n".join(hits) if hits else "no matches")


# ---- the web (our own fetcher: no private addresses, size and time limits) ----------------------------
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
             "footer", "blockquote", "pre", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    parser = _Text()
    parser.feed(raw)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    title = " ".join(parser.title.split())
    return (f"# {title}\n\n" if title else "") + text.strip()


def public_ip(host: str) -> Optional[str]:
    """The address to connect to, but only if every address the name resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return None
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return None
        ips.append(str(ip))
    return ips[0] if ips else None


def public_address(host: str) -> bool:
    return public_ip(host) is not None


def fetch_url(url: str, max_bytes: int = 3_000_000, timeout: float = 20.0, redirects: int = 5,
              allow_private: bool = False):
    """GET a web page. Returns (final_url, content_type, bytes). Refuses local and private networks."""
    for _ in range(redirects + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("only http and https web addresses are allowed")
        ip = None
        if not allow_private:
            ip = public_ip(parts.hostname)
            if ip is None:
                raise ValueError("that address is on a private or local network, which Argus doesn't fetch")
        conn = connect(f"{parts.scheme}://{parts.netloc}", timeout)
        if ip and not getattr(conn, "_tunnel_host", None):
            # Connect to the address that was just checked, not to whatever DNS answers a moment later
            # (a "DNS rebinding" trick). TLS still verifies the certificate for the real host name.
            create = conn._create_connection
            conn._create_connection = lambda address, *a, _ip=ip, **kw: create((_ip, address[1]), *a, **kw)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Argus/1.0",
                                           "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5"})
        resp = conn.getresponse()
        if resp.status in (301, 302, 303, 307, 308) and resp.getheader("location"):
            url = urljoin(url, resp.getheader("location"))
            conn.close()
            continue
        if resp.status >= 400:
            conn.close()
            raise ValueError(f"the site answered HTTP {resp.status}")
        data = resp.read(max_bytes + 1)
        ctype = (resp.getheader("content-type") or "").split(";")[0].strip().lower()
        conn.close()
        return url, ctype, data[:max_bytes]
    raise ValueError("too many redirects")


def fetch_page(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    url = str(args.get("url") or "").strip()
    limit = max(1000, min(int(args.get("max_chars") or 20_000), 100_000))
    try:
        final, ctype, data = fetch_url(url)
    except (ValueError, OSError) as exc:
        return ToolOutput.error(f"couldn't fetch {url}: {exc}")
    if ctype.startswith("image/") and len(data) < 5_000_000 and ctype in IMAGE_TYPES.values():
        return ToolOutput([{"type": "text", "text": f"Image from {final}"},
                           {"type": "image", "source": {"type": "base64", "media_type": ctype,
                                                        "data": base64.b64encode(data).decode()}}])
    if ctype in ("application/pdf",) or ctype.startswith(("audio/", "video/", "application/octet")):
        return ToolOutput.error(f"{final} is a {ctype} file, which Argus can't read as a page")
    text = data.decode("utf-8", "replace")
    if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        text = html_to_text(text)
    return ToolOutput(f"Source: {final}\n\n{_clip(text, limit)}")


# ---- news, memory, schedules, phone, apps, glasses -----------------------------------------------------
def get_news(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    articles, errors = news_mod.headlines(args.get("topic") or None, int(args.get("count") or 8),
                                          offline=os.environ.get("ARGUS_NEWS_OFFLINE", "").split(os.pathsep)
                                          if os.environ.get("ARGUS_NEWS_OFFLINE") else None)
    if not articles:
        return ToolOutput.error("no news feeds could be reached right now")
    return ToolOutput(json.dumps([{"title": a.title, "source": a.source, "published": a.published,
                                   "summary": a.summary, "link": a.link} for a in articles], ensure_ascii=False))


def remember(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    fact = " ".join(str(args.get("fact") or "").split())
    if not fact:
        return ToolOutput.error("nothing to remember")
    return ToolOutput(f"remembered (#{ctx.store.remember(fact[:500])})")


def forget(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    ok = ctx.store.forget(int(args.get("id") or 0))
    return ToolOutput("forgotten" if ok else "no such memory") if ok else ToolOutput.error("no such memory")


def recall(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    facts = ctx.store.facts(str(args.get("query") or ""), 50)
    return ToolOutput("\n".join(f"#{i}: {f}" for i, f in facts) if facts else "nothing remembered yet")


def parse_when(text: str, tz: Optional[str] = None) -> float:
    """ISO 8601 local date-time ("2026-11-09T18:30") -> epoch seconds."""
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        if tz:
            from zoneinfo import ZoneInfo
            value = value.replace(tzinfo=ZoneInfo(tz))
        else:
            value = value.astimezone()
    return value.timestamp()


def schedule(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    action = args.get("action")
    if action == "list":
        tasks = ctx.store.tasks()
        if not tasks:
            return ToolOutput("nothing scheduled")
        return ToolOutput("\n".join(
            f"#{t['id']} [{t['mode']}] {t['text']} — " + (f"repeats {t['cron']}" if t["cron"] else "") +
            (f" next {datetime.fromtimestamp(t['next_run']).strftime('%a %d %b %H:%M')}" if t["next_run"] else "")
            for t in tasks))
    if action == "cancel":
        ok = ctx.store.cancel_task(int(args.get("id") or 0))
        return ToolOutput("cancelled") if ok else ToolOutput.error("no such scheduled item")
    if action != "create":
        return ToolOutput.error("action must be create, list or cancel")
    text = " ".join(str(args.get("text") or "").split())
    mode = "task" if args.get("mode") == "task" else "reminder"
    if not text:
        return ToolOutput.error("say what the reminder or task is")
    if args.get("cron"):
        try:
            cron = Cron(str(args["cron"]), tz=ctx.timezone)
        except CronError as exc:
            return ToolOutput.error(f"bad repeat schedule: {exc}")
        first = cron.next_after(time.time())
        tid = ctx.store.add_task(text, mode, cron=str(args["cron"]), next_run=first, origin=ctx.channel)
        return ToolOutput(f"scheduled #{tid}, repeating ({args['cron']}); first run "
                          f"{datetime.fromtimestamp(first).strftime('%a %d %b %H:%M')}")
    try:
        due = parse_when(str(args.get("at") or ""), ctx.timezone)
    except ValueError:
        return ToolOutput.error("give the time as an ISO date-time like 2026-11-09T18:30")
    if due < time.time() - 60:
        return ToolOutput.error("that time has already passed")
    tid = ctx.store.add_task(text, mode, due=due, origin=ctx.channel)
    return ToolOutput(f"scheduled #{tid} for {datetime.fromtimestamp(due).strftime('%a %d %b %H:%M')}")


def send_to_phone(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolOutput.error("nothing to send")
    return ToolOutput("sent to your phone") if ctx.notify(text[:3900]) else ToolOutput.error(
        "couldn't send it: Telegram isn't set up or isn't reachable")


def open_app(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    name = str(args.get("name") or "").strip()
    if not re.fullmatch(r"[\w .&'+-]{1,60}", name):
        return ToolOutput.error("that doesn't look like an app name")
    proc = subprocess.run(["open", "-a", name], capture_output=True, text=True, timeout=20)
    return ToolOutput(f"opened {name}") if proc.returncode == 0 else ToolOutput.error(proc.stderr.strip() or f"couldn't open {name}")


def open_url(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    url = str(args.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return ToolOutput.error("only http and https links can be opened")
    proc = subprocess.run(["open", url], capture_output=True, text=True, timeout=20)
    return ToolOutput(f"opened {url} in the browser") if proc.returncode == 0 else ToolOutput.error(proc.stderr.strip())


def glasses_look(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    if ctx.glasses is None:
        return ToolOutput.error("the glasses aren't connected to Argus (the Argus iPhone app isn't running)")
    try:
        jpeg = ctx.glasses.capture(timeout=25, cancel=ctx.cancel)
    except Exception as exc:
        return ToolOutput.error(f"couldn't get a picture from the glasses: {exc}")
    return ToolOutput([{"type": "text", "text": "Photo from Sandeep's Meta glasses, just now:"},
                       {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(jpeg).decode()}}])


def build_tools(*, mac: bool = True, glasses: bool = True) -> Dict[str, Tool]:
    tools = [
        Tool("run_command", "Run a shell command on the Mac (zsh) in Argus's workspace folder and return its output. "
             "Use for files, system info, calculations, and anything a command line can do. Risky commands need "
             "the user's approval; some are never allowed.",
             _obj({"command": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                   "cwd": {"type": "string", "description": "folder to run in (default: workspace)"}}, ["command"]), run_command),
        Tool("read_file", "Read a text file or image on the Mac.",
             _obj({"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ["path"]), read_file),
        Tool("write_file", "Create or overwrite (or append to) a text file. Inside the workspace no approval is needed.",
             _obj({"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean"}},
                  ["path", "content"]), write_file),
        Tool("list_files", "List a folder's contents.", _obj({"path": {"type": "string"}, "include_hidden": {"type": "boolean"}}),
             list_files),
        Tool("search_files", "Search file names and text inside files under a folder (regular expression).",
             _obj({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]), search_files),
        Tool("fetch_page", "Read a web page (text) or image by URL.",
             _obj({"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]), fetch_page),
        Tool("get_news", "Today's top headlines (Google News, BBC, NPR, Reuters), optionally about a topic.",
             _obj({"topic": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 20}}), get_news),
        Tool("remember", "Save a lasting fact or preference about the user for future conversations.",
             _obj({"fact": {"type": "string"}}, ["fact"]), remember),
        Tool("forget", "Delete a remembered fact by its number.", _obj({"id": {"type": "integer"}}, ["id"]), forget),
        Tool("recall", "Look up remembered facts.", _obj({"query": {"type": "string"}}), recall),
        Tool("schedule", "Create, list or cancel reminders and scheduled tasks. 'reminder' says/sends the text at the time; "
             "'task' has Argus do the text as a request at that time and send the result. Give either 'at' (ISO local "
             "date-time) or 'cron' (5-field repeat, e.g. '0 7 * * 1-5').",
             _obj({"action": {"type": "string", "enum": ["create", "list", "cancel"]}, "text": {"type": "string"},
                   "mode": {"type": "string", "enum": ["reminder", "task"]}, "at": {"type": "string"},
                   "cron": {"type": "string"}, "id": {"type": "integer"}}, ["action"]), schedule),
        Tool("send_to_phone", "Send a text message to the user's phone (Telegram), e.g. a link or a list to keep.",
             _obj({"text": {"type": "string"}}, ["text"]), send_to_phone),
    ]
    if mac:
        tools += [
            Tool("open_app", "Open (or bring forward) a Mac app by name.", _obj({"name": {"type": "string"}}, ["name"]),
                 open_app, frozenset({"voice", "cli"})),
            Tool("open_url", "Open a link in the Mac's default browser for the user to see.",
                 _obj({"url": {"type": "string"}}, ["url"]), open_url, frozenset({"voice", "cli"})),
        ]
    if glasses:
        tools.append(Tool("glasses_look", "Take a photo with the user's Meta glasses camera (what they're looking at) "
                          "and look at it. Only when the user asks about what they see.",
                          _obj({}), glasses_look, frozenset({"voice", "phone", "telegram", "cli"})))
    return {t.name: t for t in tools}


def web_search_spec(blocked: List[str], city: str = "", timezone: str = "") -> Dict[str, Any]:
    """Anthropic's server-side web search (runs on Anthropic's side; $10 per 1,000 searches)."""
    spec: Dict[str, Any] = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
    domains = sorted({d.strip().lower() for d in blocked if d and d.strip()})
    if domains:
        spec["blocked_domains"] = domains
    loc = {"type": "approximate"}
    if city:
        loc["city"] = city.split(",")[0].strip()[:60]
    if timezone:
        loc["timezone"] = timezone
    if len(loc) > 1:
        spec["user_location"] = loc
    return spec


def describe_call(name: str, args: Dict[str, Any]) -> str:
    """Short human description of a tool call, for the activity log and approval dialogs."""
    if name == "run_command":
        return f"run: {str(args.get('command'))[:200]}"
    if name in ("read_file", "write_file", "list_files"):
        return f"{name.replace('_', ' ')}: {args.get('path')}"
    if name in ("fetch_page", "open_url"):
        return f"{name.replace('_', ' ')}: {args.get('url')}"
    if name == "open_app":
        return f"open {args.get('name')}"
    return f"{name} {json.dumps(args, ensure_ascii=False)[:160]}"
