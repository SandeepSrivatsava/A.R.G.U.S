"""Negative and error cases for the ways in: Telegram, the iPhone gateway, schedules, settings, the
activity log and the control socket."""
import base64
import http.client
import json
import os
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from argus import config as config_mod
from argus import phone as phone_mod
from argus.audit import Audit
from argus.control import ControlServer, send
from argus.glasses import GlassesBridge
from argus.phone import Gatekeeper, PhoneServer, bind_address, load_or_create_token
from argus.scheduler import BRIEFING_JOB, Scheduler, sync_default_jobs
from argus.store import Store
from argus.telegram import TelegramAPI, TelegramBot, TelegramError

from fakes import FakeTelegram, Script, make_agent, say, wait_for

USER = 987654321


# ---- Telegram ------------------------------------------------------------------------------------------
@pytest.fixture
def tg():
    server = FakeTelegram()
    yield server
    server.shutdown()


def make_bot(tmp_path, tg, script):
    agent, _ = make_agent(tmp_path, script)
    bot = TelegramBot(TelegramAPI(tg.token, tg.url), USER, agent, agent.store, Audit(tmp_path / "tg-audit.jsonl"),
                      approval_timeout=2)
    return bot, agent


class Truncating(ThreadingHTTPServer):
    """Promises a long body, sends a few bytes and hangs up (an IncompleteRead), or sends junk JSON."""
    daemon_threads = True

    def __init__(self, mode):
        super().__init__(("127.0.0.1", 0), _TruncatingHandler)
        self.mode = mode
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def handle_error(self, request, client_address):
        pass


class _TruncatingHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        if self.server.mode == "junk":
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"not json!")
        else:
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"ok": tr')
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)

    do_POST = do_GET = _reply


@pytest.mark.parametrize("mode", ["junk", "truncated"])
def test_telegram_transport_errors_become_telegram_errors(mode):
    server = Truncating(mode)
    api = TelegramAPI("123:abc", f"http://127.0.0.1:{server.server_port}")
    with pytest.raises(TelegramError):
        api.call("getMe")
    if mode == "truncated":                       # a half-downloaded photo is an error, not a broken picture
        with pytest.raises(TelegramError, match="cut off"):
            api.download("photos/x.jpg")
    else:                                         # a file can hold any bytes at all
        assert api.download("photos/x.jpg") == b"not json!"
    server.shutdown()


def test_telegram_rejections_and_unreachable_servers(tg):
    with pytest.raises(TelegramError):
        TelegramAPI("wrong-token", tg.url).call("getMe")           # 401 with no JSON body
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(TelegramError, match="failed"):
        TelegramAPI("123:abc", f"http://127.0.0.1:{port}").call("getMe", timeout=2)
    with pytest.raises(TelegramError, match="HTTP 404"):
        TelegramAPI(tg.token, tg.url).download("photos/missing.jpg")


def test_downloads_are_size_limited(tg):
    tg.files["photos/big.jpg"] = b"x" * 2000
    with pytest.raises(TelegramError, match="too big"):
        TelegramAPI(tg.token, tg.url).download("photos/big.jpg", limit=1000)


def test_polling_keeps_going_after_any_error(tmp_path, tg):
    bot, _ = make_bot(tmp_path, tg, Script())
    failures = [TelegramError("409 Conflict"), RuntimeError("weird"), KeyError("update_id")]
    waits = []

    def poll_once():
        if failures:
            raise failures.pop(0)
        bot.stop()
        return 0

    bot.poll_once = poll_once
    bot._stop.wait = lambda seconds: waits.append(seconds)
    bot.run()
    assert waits == [2.0, 4.0, 8.0]                                 # backs off, never dies


def test_a_bad_update_is_skipped_and_never_seen_again(tmp_path, tg):
    bot, _ = make_bot(tmp_path, tg, Script())
    tg.push({"message": {"from": {"id": USER}, "chat": "not-a-dict"}})
    tg.message("/help")
    assert bot.poll_once(timeout=0) == 2
    assert any("Argus here" in m["text"] for m in tg.sent)
    assert bot.poll_once(timeout=0) == 0                            # the offset moved past both


def test_unsupported_messages_get_a_short_answer(tmp_path, tg):
    bot, _ = make_bot(tmp_path, tg, Script())
    bot.handle_update({"message": {"from": {"id": USER}, "chat": {"id": USER, "type": "private"},
                                   "sticker": {"file_id": "s1"}}})
    assert tg.sent[-1]["text"] == "I can read text and photos for now."


def test_photo_that_cant_be_downloaded(tmp_path, tg):
    bot, _ = make_bot(tmp_path, tg, Script())
    bot.handle_update({"message": {"from": {"id": USER}, "chat": {"id": USER, "type": "private"},
                                   "photo": [{"file_id": "gone"}]}})
    assert tg.sent[-1]["text"].startswith("I couldn't open that photo")


def test_long_replies_are_split_and_failed_sends_dont_crash(tmp_path, tg):
    bot, _ = make_bot(tmp_path, tg, Script())
    bot.send("a" * 9000)
    assert [len(m["text"]) for m in tg.sent] == [3900, 3900, 1200]
    bot.api = TelegramAPI("wrong-token", tg.url)
    assert bot.send("hello") is None and not bot.notify("hello")
    assert bot.send("") is None


def test_busy_bot_and_a_crashing_brain(tmp_path, tg):
    gate = threading.Event()

    def slow(body):
        gate.wait(5)
        return say("finally")

    bot, agent = make_bot(tmp_path, tg, slow)
    bot.handle_update({"message": {"from": {"id": USER}, "chat": {"id": USER, "type": "private"}, "text": "one"}})
    bot.handle_update({"message": {"from": {"id": USER}, "chat": {"id": USER, "type": "private"}, "text": "two"}})
    assert wait_for(lambda: any("Still working" in m["text"] for m in tg.sent))
    gate.set()
    assert wait_for(lambda: any(m["text"] == "finally" for m in tg.sent))
    assert wait_for(lambda: not bot._worker.is_alive())

    def crash(**kwargs):
        raise RuntimeError("database is locked")
    agent.run = crash
    bot.handle_update({"message": {"from": {"id": USER}, "chat": {"id": USER, "type": "private"}, "text": "three"}})
    assert wait_for(lambda: any("Sorry, that failed: database is locked" in m["text"] for m in tg.sent))


def test_buttons_from_strangers_or_stale_approvals_do_nothing(tmp_path, tg):
    bot, _ = make_bot(tmp_path, tg, Script())
    bot.handle_update({"callback_query": {"id": "c1", "from": {"id": 1}, "data": "ap:x:once",
                                          "message": {"chat": {"id": 1, "type": "private"}}}})
    bot.handle_update({"callback_query": {"id": "c2", "from": {"id": USER}, "data": "ap:nonexistent:once",
                                          "message": {"chat": {"id": USER, "type": "private"}}}})
    bot.handle_update({"callback_query": {"id": "c3", "from": {"id": USER}, "data": "garbage",
                                          "message": {"chat": {"id": USER, "type": "private"}}}})
    assert "telegram_stranger" in (tmp_path / "tg-audit.jsonl").read_text()


# ---- iPhone gateway ---------------------------------------------------------------------------------------
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()


@pytest.fixture
def phone(tmp_path):
    state = {"fail": False, "block": None}

    def ask(content):
        if state["block"]:
            state["block"].wait(5)
        if state["fail"]:
            raise RuntimeError("brain unavailable")
        return "ok"

    server = PhoneServer(("127.0.0.1", 0), ask, Gatekeeper("phone-secret-token"), Audit(tmp_path / "audit.jsonl"),
                         GlassesBridge())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, state
    server.shutdown()


def raw(server, method, path, body=b"", headers=None, token="phone-secret-token"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    h = {"Authorization": f"Bearer {token}"}
    h.update(headers or {})
    conn.putrequest(method, path)
    for k, v in h.items():
        conn.putheader(k, v)
    conn.endheaders()
    if body:
        conn.send(body)
    resp = conn.getresponse()
    data = resp.read()
    try:
        return resp.status, json.loads(data or b"{}")
    except ValueError:
        return resp.status, {}


def test_gateway_rejects_malformed_bodies(phone):
    server, _ = phone
    assert raw(server, "POST", "/ask", b'{"text": "hi"}', {"Content-Length": "abc"})[0] == 400
    assert raw(server, "POST", "/ask", b"", {"Content-Length": "0"})[0] == 400
    assert raw(server, "POST", "/ask", b"", {"Content-Length": str(17 * 1024 * 1024)})[0] == 413
    assert raw(server, "POST", "/ask", b"[1, 2]", {"Content-Length": "6"})[0] == 400
    assert raw(server, "POST", "/ask", b"{oops", {"Content-Length": "5"})[0] == 400
    body = json.dumps({"text": "   "}).encode()
    assert raw(server, "POST", "/ask", body, {"Content-Length": str(len(body))}) == (400, {"error": "text is empty"})


@pytest.mark.parametrize("image,why", [
    ("%%%not-base64%%%", "not valid base64"),
    (base64.b64encode(b"\0\0\0\x18ftypheic" + b"\0" * 16).decode(), "HEIC"),
    (base64.b64encode(b"GIF89a" + b"\0" * 16).decode(), "PNG or JPEG"),
])
def test_gateway_rejects_bad_pictures(phone, image, why):
    server, _ = phone
    body = json.dumps({"image": image}).encode()
    status, reply = raw(server, "POST", "/look", body, {"Content-Length": str(len(body))})
    assert status == 400 and why in reply["error"]


def test_gateway_accepts_data_urls(phone):
    server, _ = phone
    body = json.dumps({"image": "data:image/png;base64," + PNG}).encode()
    assert raw(server, "POST", "/look", body, {"Content-Length": str(len(body))}) == (200, {"reply": "ok"})


def test_gateway_unknown_paths_and_methods(phone):
    server, _ = phone
    assert raw(server, "GET", "/admin")[0] == 404
    assert raw(server, "POST", "/shutdown", b"{}", {"Content-Length": "2"})[0] == 404
    assert raw(server, "POST", "/glasses/reboot", b"{}", {"Content-Length": "2"})[0] == 404
    assert raw(server, "PUT", "/ask", b"{}", {"Content-Length": "2"})[0] == 501
    assert raw(server, "GET", "/ping", token="wrong")[0] == 401


def test_gateway_glasses_frame_errors(phone):
    server, _ = phone
    body = json.dumps({"request_id": "stale", "image": PNG}).encode()
    assert raw(server, "POST", "/glasses/frame", body, {"Content-Length": str(len(body))}) == (409, {"ok": False})
    body = json.dumps({"request_id": "x", "image": "bad!"}).encode()
    assert raw(server, "POST", "/glasses/frame", body, {"Content-Length": str(len(body))})[0] == 400


def test_gateway_reports_a_failing_brain_and_a_busy_mac(phone):
    server, state = phone
    body = json.dumps({"text": "hi"}).encode()
    state["fail"] = True
    assert raw(server, "POST", "/ask", body, {"Content-Length": str(len(body))})[0] == 502
    state["fail"], state["block"] = False, threading.Event()
    threads = [threading.Thread(target=raw, args=(server, "POST", "/ask", body, {"Content-Length": str(len(body))}))
               for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    assert raw(server, "POST", "/ask", body, {"Content-Length": str(len(body))})[0] == 429
    state["block"].set()
    for t in threads:
        t.join(5)


def test_gateway_lockout_applies_even_to_the_right_token(phone):
    server, _ = phone
    for _ in range(5):
        assert raw(server, "GET", "/ping", token="guess")[0] == 401
    assert raw(server, "GET", "/ping")[0] == 429
    assert "phone_bad_token" in Path(server.audit.path).read_text()


def test_gateway_only_binds_to_tailscale_or_this_mac(monkeypatch):
    class S:
        phone_host = ""
    monkeypatch.setattr(phone_mod, "tailscale_ip", lambda: None)
    for host, ok in (("127.0.0.1", True), ("100.64.0.1", True), ("100.127.255.254", True), ("100.1.2.3", False),
                     ("100.128.0.1", False), ("192.168.1.10", False), ("0.0.0.0", False), ("::", False),
                     ("my-mac.local", False), ("", False)):
        S.phone_host = host
        assert (bind_address(S) is not None) == ok, host


def test_tailscale_address_is_read_and_checked(monkeypatch, tmp_path):
    fake = tmp_path / "tailscale"
    fake.write_text("#!/bin/sh\necho 192.168.1.4\necho 100.101.102.103\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert phone_mod.tailscale_ip() == "100.101.102.103"
    fake.write_text("#!/bin/sh\necho 100.1.2.3\n")                  # public address space, not Tailscale's
    assert phone_mod.tailscale_ip() is None


def test_short_or_empty_token_files_are_replaced(tmp_path):
    path = tmp_path / "phone_token"
    path.write_text("short\n")
    path.chmod(0o644)
    token = load_or_create_token(path)
    assert len(token) >= 32 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token(path) == token


# ---- schedules -------------------------------------------------------------------------------------------
def test_scheduler_survives_failures_and_says_so(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    delivered = []
    now = time.time()
    store.add_task("water plants", "reminder", due=now - 10)
    store.add_task("summarise email", "task", due=now - 10)
    store.add_task("broken repeat", "reminder", cron="not a cron", next_run=now - 10)

    def deliver(text):
        if text.startswith("Reminder: water"):
            raise OSError("speaker unplugged")
        delivered.append(text)

    def run_task(text):
        raise RuntimeError("Claude unreachable")

    s = Scheduler(store, deliver=deliver, run_task=run_task, headlines=lambda: "news")
    assert s.tick(now) == 3
    assert any("couldn't run" in t and "Claude unreachable" in t for t in delivered)
    assert store.tasks() == []                       # nothing is retried forever; the broken repeat is retired
    assert s.tick(now + 60) == 0


def test_late_reminders_say_how_late(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    delivered = []
    now = time.time()
    store.add_task("stretch", "reminder", due=now - 1800)
    Scheduler(store, deliver=delivered.append, run_task=str, headlines=str).tick(now)
    assert delivered == ["Reminder: stretch (this was due 30 minutes ago)"]


def test_scheduler_loop_keeps_running_after_a_crash(tmp_path):
    s = Scheduler(Store(tmp_path / "db.sqlite"), deliver=print, run_task=str, headlines=str, interval=0.01)
    calls = []

    def tick(now=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk full")
        s.stop()
        return 0
    s.tick = tick
    s.run()
    assert len(calls) == 2


def test_bad_default_schedules_are_skipped(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    sync_default_jobs(store, "every morning", "", None)
    assert store.tasks() == []
    sync_default_jobs(store, "", "30 7 * * 1-5", "America/Los_Angeles")
    assert [t["text"] for t in store.tasks()] == [BRIEFING_JOB]


# ---- settings and secrets ---------------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "argus"))
    (tmp_path / "argus").mkdir()
    return tmp_path / "argus"


@pytest.mark.parametrize("toml,why", [
    ('[security]\ntelegram_desktop = "false"\n', "true or false"),
    ('[security]\nblocked_sites = "mybank.com"\n', "list"),
    ('[security]\nblocked_apps = [1, 2]\n', "list"),
    ('[phone]\nport = "8765"\n', "number"),
    ('[phone]\nport = 87.5\n', "whole number"),
    ('[phone]\nport = 99999\n', "out of range"),
    ('[telegram]\nuser_id = true\n', "number"),
    ('[listening]\nclap = "loud"\n', "clap"),
    ('[listening]\nvoice_lock_threshold = 2\n', "out of range"),
    ('[voice]\nengine = "robot"\n', "engine"),
    ('timezone = "Mars/Base"\n', "time zone"),
    ('user_name = 42\n', "text"),
    ('[schedule]\nmorning_headlines = "7am daily"\n', "morning_headlines"),
    ('brain = "fast"\n', "must be a section"),
    ('[brain\nmodel = "x"\n', ""),
])
def test_bad_settings_are_clear_errors(home, toml, why):
    (home / "config.toml").write_text(toml)
    with pytest.raises(ValueError) as info:
        config_mod.load()
    assert why in str(info.value)


def test_secrets_file_parsing(tmp_path):
    p = tmp_path / "secrets.env"
    assert config_mod.read_secrets(p) == {}
    p.write_text("# comment\n\nNOEQUALS\nA = 'quoted'\nB=\"double\"\nC=x=y\n")
    p.chmod(0o600)
    assert config_mod.read_secrets(p) == {"A": "quoted", "B": "double", "C": "x=y"}


def test_learned_name_variants_are_added(home):
    (home / "learned_names.txt").write_text("argos  Argas\n")
    s = config_mod.load()
    s.extra_variants = ["Arcus"]
    assert s.all_variants() == ["arcus", "argos", "argas"]


# ---- activity log and control socket ---------------------------------------------------------------------
def test_activity_log_tightens_a_readable_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("")
    path.chmod(0o644)
    Audit(path).log("request", text="hi")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_activity_log_never_raises(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.mkdir()                                     # something is in the way: logging must still not crash
    Audit(path).log("request", text="hi")
    assert Audit(path).tail() == []
    good = tmp_path / "log.jsonl"
    good.write_text('{"event": "a"}\nnot json\n{"event": "b"}\n')
    assert [e["event"] for e in Audit(good).tail()] == ["a", "b"]
    assert Audit(tmp_path / "missing.jsonl").tail() == []


def test_control_socket_errors(tmp_path):
    def broken(req):
        raise RuntimeError("handler exploded")
    path = tmp_path / "run" / "argus.sock"
    path.parent.mkdir()
    path.write_text("stale")                         # left over from a crash: replaced on start
    server = ControlServer(path, {"status": lambda req: {"ok": True}, "broken": broken})
    server.start()
    try:
        assert send(path, "status") == {"ok": True}
        assert send(path, "nonsense") == {"ok": False, "error": "unknown command"}
        assert send(path, "broken") == {"ok": False, "error": "handler exploded"}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(path))
            s.sendall(b"[1, 2, 3]\n")
            assert json.loads(s.recv(4096))["ok"] is False
    finally:
        server.stop()
    assert not path.exists()
    with pytest.raises(OSError):
        send(path, "status")
