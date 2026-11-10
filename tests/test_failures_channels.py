"""Negative and error cases for the ways in: the iPhone gateway, schedules, settings, the activity log and
the control socket."""
import base64
import http.client
import json
import os
import socket
import stat
import threading
import time
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
    ('[security]\nscreen_control = "false"\n', "true or false"),
    ('[security]\nblocked_sites = "mybank.com"\n', "list"),
    ('[security]\nblocked_apps = [1, 2]\n', "list"),
    ('[phone]\nport = "8765"\n', "number"),
    ('[phone]\nport = 87.5\n', "whole number"),
    ('[phone]\nport = 99999\n', "out of range"),
    ('[phone]\nport = true\n', "number"),
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
