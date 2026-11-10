"""Argus's tools: what the brain can do besides talking. Each tool is a JSON schema for Claude plus
a Python function. The policy (policy.py) decides before every call whether it may run."""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import sys
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
from .protected import is_protected, sandbox_profile

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
    glasses: Any = None                                        # bridge to the iPhone app (glasses camera)
    timezone: Optional[str] = None
    location: Optional[Dict[str, float]] = None                # where the phone is (sent with this request)
    locate: Optional[Callable[[], Optional[Dict[str, float]]]] = None   # or a way to ask the phone (Android)
    links: List[str] = field(default_factory=list)             # links for the phone to open (navigation)


@dataclass
class Tool:
    name: str
    description: str
    schema: Dict[str, Any]
    run: Callable[[Dict[str, Any], Context], ToolOutput]
    channels: frozenset = frozenset({"voice", "phone", "scheduler", "cli", "android"})

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


# On macOS every command runs inside a kernel-enforced sandbox that can't read or change the places in
# protected.py, so even a disguised command can't reach your keys. (Elsewhere, e.g. in tests, it's skipped.)
SANDBOX_EXEC = "/usr/bin/sandbox-exec" if sys.platform == "darwin" else ""


def sandbox_prefix(home: Optional[Path] = None) -> List[str]:
    if not SANDBOX_EXEC or not os.path.exists(SANDBOX_EXEC):
        return []
    return [SANDBOX_EXEC, "-p", sandbox_profile(Path(home or Path.home()))]


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(float(value if value not in (None, "") else default), high))
    except (TypeError, ValueError):
        raise ValueError(f"expected a number, got {value!r}") from None


def run_command(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    command = str(args.get("command") or "").strip()
    if not command:
        return ToolOutput.error("no command given")
    try:
        timeout = _number(args.get("timeout_seconds"), 60, 1, 600)
    except ValueError as exc:
        return ToolOutput.error(f"timeout_seconds: {exc}")
    cwd = Path(os.path.expanduser(str(args.get("cwd") or ctx.workspace)))
    if not cwd.is_absolute():
        cwd = ctx.workspace / cwd
    if not cwd.is_dir():
        return ToolOutput.error(f"folder not found: {cwd}")
    shell = "/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/sh"
    try:
        proc = subprocess.Popen(sandbox_prefix() + [shell, "-c", command], cwd=str(cwd), env=child_env(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except OSError as exc:
        return ToolOutput.error(f"couldn't start the command: {exc}")
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
            why = "cancelled" if ctx.cancel.is_set() else f"timed out after {timeout:g}s"
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
    try:
        limit = int(_number(args.get("max_chars"), 40_000, 500, 200_000))
    except ValueError as exc:
        return ToolOutput.error(f"max_chars: {exc}")
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
    if not root.is_dir():
        return ToolOutput.error(f"not a folder: {root}")
    home = Path.home().resolve()
    hits: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if len(hits) >= 100 or ctx.cancel.is_set():
            break
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "Library")
                       and not is_protected(Path(dirpath) / d, home)]
        for name in filenames:
            if len(hits) >= 100 or ctx.cancel.is_set():
                break
            p = Path(dirpath) / name
            if is_protected(p, home) or p.is_symlink():   # never follow a link into a protected place
                continue
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


class TransientFetchError(OSError):
    """A failure worth one more try: a timeout, a dropped connection, a name that didn't resolve, or a busy
    site (HTTP 429 or 5xx)."""


def public_ip(host: str) -> Optional[str]:
    """The address to connect to, but only if every address the name resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except UnicodeError:
        return None
    except OSError as exc:      # gaierror and friends: no such name, or no DNS right now
        raise TransientFetchError(f"couldn't find {host} (the name doesn't resolve)") from exc
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return None
        ips.append(str(ip))
    return ips[0] if ips else None


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
        try:
            conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Argus/1.0",
                                               "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("location"):
                url = urljoin(url, resp.getheader("location"))
                continue
            if resp.status == 429 or resp.status >= 500:
                raise TransientFetchError(f"the site answered HTTP {resp.status}")
            if resp.status >= 400:
                raise ValueError(f"the site answered HTTP {resp.status}")
            data = resp.read(max_bytes + 1)
            ctype = (resp.getheader("content-type") or "").split(";")[0].strip().lower()
            return url, ctype, data[:max_bytes]
        except (TransientFetchError, ValueError):
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise TransientFetchError(str(exc) or type(exc).__name__) from exc
        finally:
            conn.close()
    raise ValueError("too many redirects")


def fetch_page(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    url = str(args.get("url") or "").strip()
    try:
        limit = int(_number(args.get("max_chars"), 20_000, 1000, 100_000))
    except ValueError as exc:
        return ToolOutput.error(f"max_chars: {exc}")
    for attempt in (1, 2):
        try:
            final, ctype, data = fetch_url(url)
            break
        except TransientFetchError as exc:          # a timeout or a hiccup: one quick second try
            if attempt == 2 or ctx.cancel.is_set():
                return ToolOutput.error(f"couldn't fetch {url}: {exc}")
            time.sleep(0.4)
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
    try:
        count = int(_number(args.get("count"), 8, 1, 20))
    except ValueError as exc:
        return ToolOutput.error(f"count: {exc}")
    articles, errors = news_mod.headlines(args.get("topic") or None, count,
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


def _whole(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def forget(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    ok = ctx.store.forget(_whole(args.get("id")))
    return ToolOutput("forgotten") if ok else ToolOutput.error("no such memory")


def recall(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    query = str(args.get("query") or "").strip().lower()
    facts = ctx.store.facts(query, 50)
    lessons = [(i, text) for i, text, _ in ctx.store.lessons(200) if not query or query in text.lower()][:30]
    parts = []
    if facts:
        parts.append("\n".join(f"#{i}: {f}" for i, f in facts))
    if lessons:
        parts.append("Lessons:\n" + "\n".join(f"L{i}: {text}" for i, text in lessons))
    return ToolOutput("\n\n".join(parts) if parts else "nothing remembered yet")


def learn(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    from .lessons import save_lesson
    lesson_id, outcome = save_lesson(ctx.store, str(args.get("lesson") or ""), "you")
    if lesson_id is None:
        return ToolOutput.error(f"not saved: {outcome}")
    return ToolOutput(f"lesson L{lesson_id} {outcome}")


def forget_lesson(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    ok = ctx.store.forget_lesson(_whole(args.get("id")))
    return ToolOutput("lesson forgotten") if ok else ToolOutput.error("no such lesson")


def parse_when(text: str, tz: Optional[str] = None) -> float:
    """ISO 8601 local date-time ("2026-11-09T18:30") -> epoch seconds."""
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        zone = None
        if tz:
            from zoneinfo import ZoneInfo
            try:
                zone = ZoneInfo(tz)
            except Exception:   # unknown zone: use the Mac's own time
                zone = None
        value = value.replace(tzinfo=zone) if zone else value.astimezone()
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
        ok = ctx.store.cancel_task(_whole(args.get("id")))
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


def open_app(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    name = str(args.get("name") or "").strip()
    if not re.fullmatch(r"[\w .&'+-]{1,60}", name):
        return ToolOutput.error("that doesn't look like an app name")
    try:
        proc = subprocess.run(["open", "-a", name], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolOutput.error(f"couldn't open {name}: {exc}")
    return ToolOutput(f"opened {name}") if proc.returncode == 0 else ToolOutput.error(proc.stderr.strip() or f"couldn't open {name}")


def open_url(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    url = str(args.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return ToolOutput.error("only http and https links can be opened")
    try:
        proc = subprocess.run(["open", url], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolOutput.error(f"couldn't open that link: {exc}")
    return ToolOutput(f"opened {url} in the browser") if proc.returncode == 0 else ToolOutput.error(
        proc.stderr.strip() or "couldn't open that link")


def glasses_look(args: Dict[str, Any], ctx: Context) -> ToolOutput:
    if ctx.glasses is None:
        return ToolOutput.error("the glasses aren't connected to Argus (the Argus iPhone app isn't running)")
    try:
        jpeg = ctx.glasses.capture(timeout=25, cancel=ctx.cancel)
    except Exception as exc:
        return ToolOutput.error(f"couldn't get a picture from the glasses: {exc}")
    return ToolOutput([{"type": "text", "text": "Photo from the user's Meta glasses, just now:"},
                       {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(jpeg).decode()}}])


def build_tools(*, mac: bool = True, glasses: bool = True, commands: bool = True) -> Dict[str, Tool]:
    """The tools for one kind of Argus: the Mac has them all; the always-on server and Android have no shell
    commands (there's no kernel sandbox to run them in)."""
    tools = [
        Tool("run_command", "Run a shell command on the Mac (zsh) in Argus's workspace folder and return its output. "
             "Use for files, system info, calculations, and anything a command line can do. Risky commands need "
             "the user's approval; some are never allowed.",
             _obj({"command": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                   "cwd": {"type": "string", "description": "folder to run in (default: workspace)"}}, ["command"]), run_command),
        Tool("read_file", "Read a text file or image.",
             _obj({"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ["path"]), read_file),
        Tool("write_file", "Create or overwrite (or append to) a text file. Inside the workspace no approval is needed.",
             _obj({"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean"}},
                  ["path", "content"]), write_file),
        Tool("list_files", "List a folder's contents.", _obj({"path": {"type": "string"}, "include_hidden": {"type": "boolean"}}),
             list_files),
        Tool("search_files", "Search file names and text inside files under a folder (regular expression).",
             _obj({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]), search_files),
        Tool("fetch_page", "Read a web page (text) or image by URL.",
             _obj({"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]), fetch_page,
             frozenset({"voice", "phone", "scheduler", "cli", "android", "guest"})),
        Tool("get_news", "Today's top headlines (Google News, BBC, NPR, Reuters), optionally about a topic.",
             _obj({"topic": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 20}}), get_news,
             frozenset({"voice", "phone", "scheduler", "cli", "android", "guest"})),
        Tool("remember", "Save a lasting fact or preference about the user for future conversations.",
             _obj({"fact": {"type": "string"}}, ["fact"]), remember),
        Tool("forget", "Delete a remembered fact by its number.", _obj({"id": {"type": "integer"}}, ["id"]), forget),
        Tool("recall", "Look up remembered facts and learned lessons.", _obj({"query": {"type": "string"}}), recall),
        Tool("learn", "Save a lesson: one short, general rule for doing things better next time. Use it when the user "
             "corrects you or tells you how they want something done from now on (one-off facts go to remember).",
             _obj({"lesson": {"type": "string", "description": "e.g. 'When Sandeep says the report, he means the "
                                                              "latest PDF in ~/Documents/Work.'"}}, ["lesson"]),
             learn, frozenset({"voice", "phone", "cli", "android"})),
        Tool("forget_lesson", "Delete a learned lesson by its L-number.", _obj({"id": {"type": "integer"}}, ["id"]),
             forget_lesson, frozenset({"voice", "phone", "cli", "android"})),
        Tool("schedule", "Create, list or cancel reminders and scheduled tasks. 'reminder' says/sends the text at the time; "
             "'task' has Argus do the text as a request at that time and send the result. Give either 'at' (ISO local "
             "date-time) or 'cron' (5-field repeat, e.g. '0 7 * * 1-5').",
             _obj({"action": {"type": "string", "enum": ["create", "list", "cancel"]}, "text": {"type": "string"},
                   "mode": {"type": "string", "enum": ["reminder", "task"]}, "at": {"type": "string"},
                   "cron": {"type": "string"}, "id": {"type": "integer"}}, ["action"]), schedule),
    ]
    if not commands:
        tools = [t for t in tools if t.name != "run_command"]
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
                          _obj({}), glasses_look, frozenset({"voice", "phone", "cli"})))
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
    if name == "phone_call":
        return f"phone {args.get('business') or args.get('number')}: {str(args.get('goal'))[:120]}"
    if name == "route":
        return f"directions to {str(args.get('destination'))[:120]}"
    if name == "search_messages":
        return f"search messages: {str(args.get('query'))[:120]}"
    if name == "send_text":
        return f"text {args.get('to')}: {str(args.get('message'))[:120]}"
    return f"{name} {json.dumps(args, ensure_ascii=False)[:160]}"
