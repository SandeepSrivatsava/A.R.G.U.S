"""Negative and error cases for the tools and the rules: protected places, bad inputs, failing commands,
hostile web pages, and schedules that can't work."""
import os
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from argus import protected, toolbox
from argus.config import Settings
from argus.cron import Cron, CronError
from argus.glasses import GlassesBridge
from argus.policy import ALLOW, ASK, BLOCK, Policy
from argus.store import Store
from argus.toolbox import Context

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".argus").mkdir(parents=True)
    (h / ".argus" / "secrets.env").write_text("ANTHROPIC_API_KEY=sk-ant-SECRET\n")
    (h / ".ssh").mkdir()
    (h / ".ssh" / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY----- sk-ant-SECRET\n")
    (h / "Library" / "Keychains").mkdir(parents=True)
    (h / "Library" / "Keychains" / "login.txt").write_text("sk-ant-SECRET")
    (h / "Library" / "Notes").mkdir()
    (h / "Library" / "Notes" / "todo.txt").write_text("call about sk-ant-SECRET rotation\n")
    (h / ".netrc").write_text("machine x password sk-ant-SECRET\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    return h


@pytest.fixture
def ws(home):
    d = home / "argus-workspace"
    d.mkdir()
    return d


@pytest.fixture
def ctx(tmp_path, ws):
    return Context(channel="voice", workspace=ws, store=Store(tmp_path / "db.sqlite"), timezone="America/Los_Angeles")


@pytest.fixture
def policy(ws, home):
    state = {"locked": False, "front_bundle": "com.apple.Safari", "front_name": "Safari", "visible_apps": []}
    p = Policy(Settings(blocked_sites=["mybank.com"]), workspace=ws, home=home, desktop_state=lambda: dict(state))
    p.state = state
    return p


# ---- protected places -----------------------------------------------------------------------------------
def test_file_tools_cant_touch_protected_places(policy, home, ws):
    for tool, path in (("read_file", "~/.argus/config.toml"), ("read_file", str(home / ".ARGUS" / "secrets.env")),
                       ("list_files", "~/.ssh"), ("search_files", str(home / ".argus")), ("read_file", "~/.netrc"),
                       ("search_files", "~/Library/Keychains"), ("write_file", "~/.ssh/authorized_keys"),
                       ("read_file", "~/Library/Application Support/Google/Chrome/Default/Cookies")):
        assert policy.check(tool, {"path": path, "content": "x"}, "voice").action == BLOCK, (tool, path)
    (ws / "innocent.txt").symlink_to(home / ".argus" / "secrets.env")       # a link into a protected place
    assert policy.check("read_file", {"path": "innocent.txt"}, "voice").action == BLOCK
    assert policy.check("list_files", {"path": "~"}, "voice").action == ALLOW
    assert policy.check("read_file", {"path": "~/argus-workspace/notes.txt"}, "voice").action == ALLOW


def test_search_never_reads_protected_places_or_follows_links(ctx, home, ws):
    (ws / "link.txt").symlink_to(home / ".argus" / "secrets.env")
    out = toolbox.search_files({"path": str(home / "Library"), "pattern": "sk-ant"}, ctx).content
    assert "Notes/todo.txt" in out and "Keychains" not in out
    out = toolbox.search_files({"path": str(home), "pattern": "SECRET"}, ctx).content
    assert out == "no matches"                     # .argus, .ssh, .netrc, Library and the link are all skipped
    assert toolbox.search_files({"path": str(home / "nope"), "pattern": "x"}, ctx).is_error
    assert "bad search pattern" in toolbox.search_files({"path": str(ws), "pattern": "(unclosed"}, ctx).content


@pytest.mark.parametrize("command", [
    "cat ~/.argus/secrets.env", "cd ~/.argus && cat secrets.env", "ls -la ~/.ssh", "cat ~/.ARGUS/config.toml",
    "tar czf /tmp/k.tgz ~/Library/Keychains", "cp ~/.netrc /tmp/", "base64 < ~/.aws/credentials",
    "sqlite3 '~/Library/Application Support/Google/Chrome/Default/Cookies' .dump",
    "cat ~/Library/Application\\ Support/Microsoft\\ Edge/Default/Cookies", "gh auth token > ~/.config/gh/x",
    "cat argus.db", "grep KEY secrets.env"])
def test_commands_that_reach_for_secrets_are_blocked(policy, command):
    assert policy.check("run_command", {"command": command}, "voice").action == BLOCK


@pytest.mark.parametrize("command", ["ls ~/argus-workspace", "cat notes.argus.txt", "docker ps", "echo .argusx"])
def test_similar_looking_commands_still_run(policy, command):
    assert policy.check("run_command", {"command": command}, "voice").action == ALLOW


def test_commands_cant_start_inside_a_protected_folder(policy, home):
    assert policy.check("run_command", {"command": "cat secrets.env", "cwd": "~/.argus"}, "voice").action == BLOCK
    assert policy.check("run_command", {"command": "ls", "cwd": str(home / ".ssh")}, "voice").action == BLOCK
    assert policy.check("run_command", {"command": "ls", "cwd": "~/argus-workspace"}, "voice").action == ALLOW


def test_sandbox_profile_covers_every_protected_place(tmp_path):
    assert protected._sb_quote('a"b\\c') == '"a\\"b\\\\c"'
    odd = tmp_path / 'we"ird\\home'
    profile = protected.sandbox_profile(odd)
    assert profile.startswith("(version 1)\n(allow default)\n(deny file-read* file-write*")
    for d in protected.PROTECTED_DIRS:
        assert f"(subpath {protected._sb_quote(str(odd / d))})" in profile
    for f in protected.PROTECTED_FILES:
        assert f"(literal {protected._sb_quote(str(odd / f))})" in profile
    assert profile.count("(") - profile.count('\\(') == profile.count(")") - profile.count('\\)')


def test_commands_run_inside_the_sandbox_when_the_mac_has_one(ctx, monkeypatch, tmp_path):
    fake = tmp_path / "sandbox-exec"
    seen = tmp_path / "profile.txt"
    fake.write_text(f'#!/bin/sh\n[ "$1" = "-p" ] || exit 64\nprintf "%s" "$2" > "{seen}"\nshift 2\nexec "$@"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(toolbox, "SANDBOX_EXEC", str(fake))
    out = toolbox.run_command({"command": "echo inside"}, ctx)
    assert not out.is_error and "inside" in out.content
    assert '(subpath "' in seen.read_text() and "/.argus" in seen.read_text()
    monkeypatch.setattr(toolbox, "SANDBOX_EXEC", str(tmp_path / "missing"))
    assert toolbox.sandbox_prefix() == []


def test_is_protected_handles_case_and_lookalikes(home):
    assert protected.is_protected(home / ".Argus" / "x", home)
    assert protected.is_protected(home / ".ssh", home)
    assert not protected.is_protected(home / ".argus-old", home)
    assert not protected.is_protected(home / "argus" / ".ssh-notes.txt", home)
    assert not protected.is_protected(Path("/tmp/.argus"), home)


# ---- shell commands --------------------------------------------------------------------------------------
def test_run_command_bad_inputs(ctx):
    assert toolbox.run_command({"command": "   "}, ctx).is_error
    assert "timeout_seconds" in toolbox.run_command({"command": "true", "timeout_seconds": "soon"}, ctx).content
    assert "folder not found" in toolbox.run_command({"command": "ls", "cwd": "no/such/dir"}, ctx).content
    out = toolbox.run_command({"command": "echo oops >&2; exit 3"}, ctx)
    assert out.is_error and "exit code 3" in out.content and "oops" in out.content
    assert "(no output)" in toolbox.run_command({"command": "true"}, ctx).content


def test_run_command_timeout_kills_the_whole_process_group(ctx, ws):
    started = time.monotonic()
    out = toolbox.run_command({"command": f"(sleep 30; touch {ws}/late) & sleep 30", "timeout_seconds": 1}, ctx)
    assert out.is_error and "timed out after 1s" in out.content
    assert time.monotonic() - started < 10
    time.sleep(0.3)
    assert not (ws / "late").exists()


def test_run_command_cancel(ctx):
    threading.Timer(0.3, ctx.cancel.set).start()
    out = toolbox.run_command({"command": "sleep 20"}, ctx)
    assert out.is_error and "cancelled" in out.content


def test_huge_output_is_clipped(ctx):
    out = toolbox.run_command({"command": "head -c 50000 /dev/zero | tr '\\0' 'a'"}, ctx)
    assert "characters cut" in out.content and len(out.content) < 13_000


def test_command_that_cant_start(ctx, monkeypatch):
    def broken(*a, **k):
        raise OSError("no shell")
    monkeypatch.setattr(toolbox.subprocess, "Popen", broken)
    assert "couldn't start the command" in toolbox.run_command({"command": "ls"}, ctx).content


# ---- files -----------------------------------------------------------------------------------------------
def test_file_tool_errors(ctx, ws):
    assert "no such file" in toolbox.read_file({"path": "missing.txt"}, ctx).content
    (ws / "bin.dat").write_bytes(b"\0\1\2" * 100)
    assert "binary" in toolbox.read_file({"path": "bin.dat"}, ctx).content
    (ws / "big.png").write_bytes(b"\x89PNG" + b"\0" * (5 * 1024 * 1024 + 1))
    assert "larger than 5 MB" in toolbox.read_file({"path": "big.png"}, ctx).content
    (ws / "ok.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 10)
    assert toolbox.read_file({"path": "ok.png"}, ctx).content[0]["type"] == "image"
    (ws / "t.txt").write_text("x" * 5000)
    assert "max_chars" in toolbox.read_file({"path": "t.txt", "max_chars": "lots"}, ctx).content
    assert "characters cut" in toolbox.read_file({"path": "t.txt", "max_chars": 600}, ctx).content
    assert "not a folder" in toolbox.list_files({"path": "t.txt"}, ctx).content


def test_write_file_appends_and_creates_folders(ctx, ws):
    toolbox.write_file({"path": "a/b/c.txt", "content": "one\n"}, ctx)
    toolbox.write_file({"path": "a/b/c.txt", "content": "two\n", "append": True}, ctx)
    assert (ws / "a/b/c.txt").read_text() == "one\ntwo\n"
    assert not list((ws / "a/b").glob(".*argus-tmp"))           # no leftover temp file


def test_list_files_hides_dotfiles_and_caps_long_folders(ctx, ws):
    (ws / ".hidden").write_text("x")
    for i in range(305):
        (ws / f"f{i:03}.txt").write_text("x")
    out = toolbox.list_files({"path": "."}, ctx).content
    assert ".hidden" not in out and "more not shown" in out
    assert ".hidden" in toolbox.list_files({"path": ".", "include_hidden": True}, ctx).content
    (ws / "empty").mkdir()
    assert "is empty" in toolbox.list_files({"path": "empty"}, ctx).content


def test_search_skips_big_and_non_text_files(ctx, ws):
    (ws / "big.log").write_text("needle\n" + "x" * 1_100_000)
    (ws / "latin1.txt").write_bytes("needle caf\xe9".encode("latin-1"))
    (ws / "ok.txt").write_text("a needle here")
    out = toolbox.search_files({"path": str(ws), "pattern": "needle"}, ctx).content
    assert "ok.txt:1" in out and "big.log" not in out and "latin1.txt" not in out


# ---- the web -----------------------------------------------------------------------------------------------
class Site(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _SiteHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def handle_error(self, request, client_address):
        pass


class _SiteHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        port = self.server.server_port
        routes = {
            "/missing": (404, "text/html", b"nope"),
            "/loop": (302, "text/html", b""),
            "/to-private": (302, "text/html", b""),
            "/pdf": (200, "application/pdf", b"%PDF-1.7"),
            "/pic": (200, "image/png", b"\x89PNG\r\n\x1a\n" + b"\0" * 16),
            "/huge": (200, "text/plain", b"y" * 3_500_000),
            "/page": (200, "text/html; charset=utf-8", b"<title>T</title><p>Hello <b>web</b></p><script>x</script>"),
        }
        status, ctype, body = routes.get(self.path, (404, "text/plain", b""))
        self.send_response(status)
        if self.path == "/loop":
            self.send_header("Location", "/loop")
        if self.path == "/to-private":
            self.send_header("Location", f"http://localhost:{port}/page")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def site(monkeypatch):
    server = Site()
    # "public.example" stands for a public web site; everything else resolves as it really does.
    real = toolbox.public_ip
    monkeypatch.setattr(toolbox, "public_ip", lambda host: "127.0.0.1" if host == "public.example" else real(host))
    yield f"http://public.example:{server.server_port}"
    server.shutdown()


def test_fetch_failures(ctx, site):
    assert "HTTP 404" in toolbox.fetch_page({"url": f"{site}/missing"}, ctx).content
    assert "too many redirects" in toolbox.fetch_page({"url": f"{site}/loop"}, ctx).content
    assert "private or local network" in toolbox.fetch_page({"url": f"{site}/to-private"}, ctx).content
    assert "application/pdf" in toolbox.fetch_page({"url": f"{site}/pdf"}, ctx).content
    assert "only http and https" in toolbox.fetch_page({"url": "ftp://public.example/x"}, ctx).content
    assert "only http and https" in toolbox.fetch_page({"url": "http:///nohost"}, ctx).content
    assert "max_chars" in toolbox.fetch_page({"url": f"{site}/page", "max_chars": "all"}, ctx).content


def test_fetch_successes_are_bounded(ctx, site):
    page = toolbox.fetch_page({"url": f"{site}/page"}, ctx).content
    assert page.startswith(f"Source: {site}/page") and "# T" in page and "Hello web" in page and "x" not in page[-5:]
    assert toolbox.fetch_page({"url": f"{site}/pic"}, ctx).content[1]["type"] == "image"
    huge = toolbox.fetch_page({"url": f"{site}/huge", "max_chars": 5000}, ctx).content
    assert "characters cut" in huge and len(huge) < 6000


def test_unknown_host_is_refused(ctx):
    assert "private or local network" in toolbox.fetch_page({"url": "http://no-such-host.invalid/"}, ctx).content


def test_long_web_addresses_need_approval(policy):
    long_url = "https://evil.example/collect?d=" + "A" * 400
    v = policy.check("fetch_page", {"url": long_url}, "voice")
    assert v.action == ASK and v.scope == "web:evil.example"
    assert policy.check("open_url", {"url": long_url}, "voice").action == ASK
    assert policy.check("fetch_page", {"url": "https://news.example/story?id=42"}, "voice").action == ALLOW
    assert policy.check("fetch_page", {"url": "https://www.mybank.com/x"}, "voice").action == BLOCK


# ---- other tools -----------------------------------------------------------------------------------------
def test_memory_and_phone_tool_errors(ctx):
    assert toolbox.remember({"fact": "   "}, ctx).is_error
    assert toolbox.forget({"id": "first"}, ctx).is_error
    assert toolbox.forget({"id": 999}, ctx).is_error
    assert toolbox.send_to_phone({"text": ""}, ctx).is_error
    assert "Telegram isn't set up" in toolbox.send_to_phone({"text": "hi"}, ctx).content


def test_schedule_tool_errors(ctx):
    assert "action must be" in toolbox.schedule({"action": "explode"}, ctx).content
    assert toolbox.schedule({"action": "create", "text": " ", "at": "2099-01-01T09:00"}, ctx).is_error
    assert "bad repeat schedule" in toolbox.schedule({"action": "create", "text": "x", "cron": "every day"}, ctx).content
    assert toolbox.schedule({"action": "cancel", "id": "first"}, ctx).is_error
    assert toolbox.schedule({"action": "cancel", "id": 42}, ctx).is_error
    assert toolbox.schedule({"action": "list"}, ctx).content == "nothing scheduled"


def test_unknown_time_zones_fall_back_to_the_macs_own_time(ctx):
    ctx.timezone = "Mars/Olympus_Mons"
    out = toolbox.schedule({"action": "create", "text": "x", "at": "2099-01-01T09:00"}, ctx)
    assert not out.is_error
    assert Cron("0 7 * * *", tz="Mars/Olympus_Mons").tz is None


def test_news_tool_errors(ctx, monkeypatch, tmp_path):
    assert "count" in toolbox.get_news({"count": "many"}, ctx).content
    (tmp_path / "broken.xml").write_text("<rss><channel><item><title>")
    monkeypatch.setenv("ARGUS_NEWS_OFFLINE", str(tmp_path / "broken.xml"))
    assert "no news feeds" in toolbox.get_news({}, ctx).content


def test_open_app_and_url_refuse_odd_input(ctx):
    assert "doesn't look like an app name" in toolbox.open_app({"name": "Safari; rm -rf ~"}, ctx).content
    assert "doesn't look like an app name" in toolbox.open_app({"name": ""}, ctx).content
    assert "only http and https" in toolbox.open_url({"url": "file:///etc/passwd"}, ctx).content
    assert "only http and https" in toolbox.open_url({"url": "-a Terminal"}, ctx).content


def test_glasses_tool_without_the_phone_app(ctx):
    assert "aren't connected" in toolbox.glasses_look({}, ctx).content
    ctx.glasses = GlassesBridge()
    assert "iPhone app isn't open" in toolbox.glasses_look({}, ctx).content
    ctx.glasses.update_status(False)
    assert "aren't connected to the phone" in toolbox.glasses_look({}, ctx).content


def test_glasses_capture_gives_up_and_can_be_cancelled():
    bridge = GlassesBridge()
    bridge.update_status(True, "Ray-Ban Meta")
    with pytest.raises(RuntimeError, match="in time"):
        bridge.capture(timeout=0.2)
    assert bridge.next_request(wait=0.1) is None           # the abandoned request isn't left waiting
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    with pytest.raises(RuntimeError):
        bridge.capture(timeout=10, cancel=cancel)
    assert not bridge.deliver("unknown-request", b"jpeg")
    assert "connected since" in bridge.describe()
    bridge.app_seen = 0                                    # the phone app went quiet
    assert bridge.describe() is None


# ---- schedules -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("expr", ["* * * *", "61 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8",
                                  "*/0 * * * *", "1-0 * * * *", "x * * * *", "*/a * * * *", "5-60 * * * *"])
def test_bad_cron_expressions(expr):
    with pytest.raises(CronError):
        Cron(expr)


def test_cron_edge_cases():
    with pytest.raises(CronError, match="never runs"):
        Cron("0 0 31 2 *").next_after(time.time())
    jan = time.mktime((2027, 1, 1, 0, 0, 0, 0, 0, -1))
    nxt = time.localtime(Cron("0 9 * jan,jul mon").next_after(jan))
    assert (nxt.tm_mon, nxt.tm_hour, nxt.tm_wday) == (1, 9, 0)
    first = time.localtime(Cron("0 12 13 * fri").next_after(jan))   # classic cron: the 13th OR a Friday
    assert first.tm_mday == 1 and first.tm_wday == 4
    assert time.localtime(Cron("15 */6 * * *").next_after(jan)).tm_hour == 0
    assert time.localtime(Cron("5 3 * * 7").next_after(jan)).tm_wday == 6   # 7 is Sunday too
    leap = time.localtime(Cron("0 9 29 2 *").next_after(time.mktime((2097, 3, 1, 0, 0, 0, 0, 0, -1))))
    assert (leap.tm_year, leap.tm_mon, leap.tm_mday) == (2104, 2, 29)       # 2100 isn't a leap year


def test_store_rolls_back_a_failed_history_rewrite(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    sid = store.new_session("voice", "mac")
    store.append(sid, "user", "keep me")
    with pytest.raises(KeyError):
        store.replace_history(sid, [{"role": "user", "content": "new"}, {"content": "missing role"}])
    assert store.history(sid) == [{"role": "user", "content": "keep me"}]


def test_store_prunes_old_conversations(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    old = store.new_session("voice", "mac")
    store.append(old, "user", "old")
    store._q("UPDATE sessions SET updated=? WHERE id=?", (time.time() - 60 * 86400, old))
    fresh = store.new_session("voice", "mac")
    store.append(fresh, "user", "new")
    assert store.prune_sessions(older_than_days=45) == 1
    assert store.history(old) == [] and store.history(fresh)
    assert stat.S_IMODE(os.stat(tmp_path / "db.sqlite").st_mode) == 0o600
    store.close()
