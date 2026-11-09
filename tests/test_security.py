import base64
import http.client
import io
import json
import os
import stat
import threading

import pytest

from argus import config as config_mod
from argus.audit import Audit
from argus.control import ControlServer, send
from argus.guard import Guard, is_blocked_app, mentions_blocked_site, read_state, write_state
from argus.hermes import Hermes
from argus.hook import evaluate, main as hook_main
from argus.phone import Gatekeeper, PhoneServer, image_data_url, load_or_create_token

from fakes import FakeHermes


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "argus"))
    return tmp_path / "argus"


def settings_with(**kw):
    s = config_mod.Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---- config ------------------------------------------------------------------------------------------
def test_config_loads_sections(home):
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        'name = "Argus"\nuser_name = "Sandeep"\n[voice]\nengine = "say"\nrate = 200\n'
        '[security]\nblocked_sites = ["mybank.com"]\n[phone]\nenabled = true\nport = 9999\n')
    s = config_mod.load()
    assert (s.user_name, s.voice_engine, s.rate, s.blocked_sites, s.phone_enabled, s.phone_port) == \
        ("Sandeep", "say", 200, ["mybank.com"], True, 9999)
    assert "com.1password.1password" in s.blocked_apps


def test_config_typo_in_section_is_an_error(home):
    home.mkdir(parents=True)
    (home / "config.toml").write_text('[secutiry]\nblocked_sites = ["mybank.com"]\n')
    with pytest.raises(ValueError):
        config_mod.load()


def test_private_dirs(home):
    s = config_mod.load()
    config_mod.ensure_private_dirs(s)
    for d in (s.home, s.run_dir, s.audit_path.parent):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


# ---- audit -------------------------------------------------------------------------------------------
def test_audit_is_private_and_rotates(tmp_path):
    a = Audit(tmp_path / "audit.jsonl", max_bytes=200, backups=2)
    for i in range(20):
        a.log("request", text=f"message {i}")
    assert stat.S_IMODE(os.stat(tmp_path / "audit.jsonl").st_mode) == 0o600
    assert (tmp_path / "audit.jsonl.1").exists() and not (tmp_path / "audit.jsonl.3").exists()
    assert a.tail(1)[0]["text"] == "message 19"


# ---- privacy rules (the Hermes pre_tool_call hook) -------------------------------------------------------
UNLOCKED_SAFARI = {"locked": False, "front_bundle": "com.apple.Safari", "front_name": "Safari"}


def call(tool, **args):
    return {"tool_name": tool, "tool_input": args, "session_id": "s"}


def test_blocked_apps_cannot_be_targeted():
    s = settings_with()
    assert evaluate(call("computer_use", action="capture", app="1Password"), s, UNLOCKED_SAFARI)
    assert evaluate(call("computer_use", action="click", app="com.apple.keychainaccess"), s, UNLOCKED_SAFARI)
    assert evaluate(call("computer_use", action="capture", app="Safari"), s, UNLOCKED_SAFARI) is None


def test_front_app_checked_when_no_app_named():
    s = settings_with()
    front_pw = {"locked": False, "front_bundle": "com.1password.1password", "front_name": "1Password"}
    assert "1Password" in evaluate(call("computer_use", action="capture"), s, front_pw)
    assert evaluate(call("computer_use", action="capture", app="screen"), s, front_pw)
    assert evaluate(call("computer_use", action="capture"), s, UNLOCKED_SAFARI) is None
    assert evaluate(call("computer_use", action="list_apps"), s, front_pw) is None


def test_locked_mac_and_private_screen_block_desktop_use():
    s = settings_with()
    assert "locked" in evaluate(call("computer_use", action="capture", app="Safari"), s, {"locked": True})
    private = dict(UNLOCKED_SAFARI, screen_private=True)
    assert "switched off" in evaluate(call("computer_use", action="capture", app="Safari"), s, private)


def test_unknown_front_app_fails_closed(monkeypatch):
    import argus.hook as hook
    monkeypatch.setattr(hook, "frontmost_app", lambda: None)
    reason = evaluate(call("computer_use", action="click"), settings_with(), {"locked": False})
    assert reason and "couldn't check" in reason


def test_blocked_sites_anywhere_in_arguments():
    s = settings_with(blocked_sites=["mybank.com", "paypal.com"])
    assert evaluate(call("browser_navigate", url="https://secure.mybank.com/login"), s, UNLOCKED_SAFARI)
    assert evaluate(call("computer_use", action="type", app="Safari", text="paypal.com\n"), s, UNLOCKED_SAFARI)
    assert evaluate(call("web_extract", urls=["https://news.example.com", "https://www.paypal.com/x"]), s, {})
    assert evaluate(call("browser_navigate", url="https://notmybank.com.au"), s, {}) is None
    assert mentions_blocked_site({"a": ["x", {"b": "go to MyBank.com"}]}, ["mybank.com"]) == "mybank.com"


def test_other_tools_pass(home):
    assert evaluate(call("web_search", query="weather"), settings_with(), {}) is None


def test_hook_main_reads_stdin_and_audits(home):
    config_mod.ensure_private_dirs(config_mod.load())
    write_state(config_mod.load().state_path, dict(UNLOCKED_SAFARI))
    out = io.StringIO()
    hook_main(io.StringIO(json.dumps(call("computer_use", action="capture", app="Keychain Access"))), out)
    assert json.loads(out.getvalue())["decision"] == "block"
    out = io.StringIO()
    hook_main(io.StringIO(json.dumps(call("computer_use", action="capture", app="Notes"))), out)
    assert json.loads(out.getvalue()) == {}
    events = [e["event"] for e in Audit(config_mod.load().audit_path).tail(5)]
    assert events == ["tool_blocked", "tool_allowed"]


def test_hook_blocks_when_settings_are_broken(home):
    home.mkdir(parents=True)
    (home / "config.toml").write_text("this is not toml = = =")
    out = io.StringIO()
    hook_main(io.StringIO(json.dumps(call("computer_use", action="capture", app="Notes"))), out)
    assert json.loads(out.getvalue())["decision"] == "block"


def test_is_blocked_app_variants():
    blocked = config_mod.DEFAULT_BLOCKED_APPS
    for app in ("1Password 7", "keychain", "Passwords", "System Settings", "com.bitwarden.desktop"):
        assert is_blocked_app(app, blocked), app
    for app in ("Safari", "Notes", "System Information", "com.apple.mail", ""):
        assert not is_blocked_app(app, blocked), app


def test_state_file_freshness(tmp_path):
    p = tmp_path / "state.json"
    write_state(p, {"locked": False})
    assert read_state(p)["locked"] is False
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    data = json.loads(p.read_text())
    data["updated"] -= 60
    p.write_text(json.dumps(data))
    assert read_state(p) is None          # stale state is ignored (hook re-checks itself)


def test_guard_reports_lock_changes(tmp_path):
    seen = []
    states = iter([False, False, True, True, False])
    g = Guard(tmp_path / "state.json", seen.append, locked_fn=lambda: next(states),
              front_fn=lambda: ("com.apple.Safari", "Safari"))
    for _ in range(5):
        g.poll_once()
    assert seen == [True, False]
    assert read_state(tmp_path / "state.json")["front_name"] == "Safari"


# ---- control socket ----------------------------------------------------------------------------------
def test_control_socket_is_owner_only(tmp_path):
    sock = tmp_path / "run" / "argus.sock"
    server = ControlServer(sock, {"status": lambda req: {"ok": True, "mode": "listening"},
                                  "boom": lambda req: 1 / 0})
    server.start()
    try:
        assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(sock.parent).st_mode) == 0o700
        assert send(sock, "status") == {"ok": True, "mode": "listening"}
        assert send(sock, "nope")["ok"] is False
        assert "division" in send(sock, "boom")["error"]
    finally:
        server.stop()


# ---- phone gateway -----------------------------------------------------------------------------------
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()


def test_token_file(tmp_path):
    t = load_or_create_token(tmp_path / "phone_token")
    assert len(t) >= 40 and load_or_create_token(tmp_path / "phone_token") == t
    assert stat.S_IMODE(os.stat(tmp_path / "phone_token").st_mode) == 0o600


def test_image_validation():
    assert image_data_url(PNG[:20] + "\n" + PNG[20:]).startswith("data:image/png;base64,")
    for bad in ("not base64!!", base64.b64encode(b"GIF89a....").decode()):
        with pytest.raises(ValueError):
            image_data_url(bad)


def test_gatekeeper_locks_out_after_bad_tokens():
    t = [0.0]
    g = Gatekeeper("secret-token", clock=lambda: t[0])
    assert g.check("100.1.1.1", "Bearer secret-token")
    for _ in range(5):
        assert not g.check("100.1.1.2", "Bearer wrong")
    assert g.locked_out("100.1.1.2") and not g.locked_out("100.1.1.1")
    t[0] += 16 * 60
    assert not g.locked_out("100.1.1.2")


@pytest.fixture
def phone(tmp_path):
    hermes = FakeHermes()
    server = PhoneServer(("127.0.0.1", 0), Hermes(hermes.url, "test-key"), Gatekeeper("phone-secret-token"),
                         Audit(tmp_path / "audit.jsonl"), user="Sandeep")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, hermes
    server.shutdown()
    hermes.shutdown()


def post(server, path, body, token="phone-secret-token"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    data = json.dumps(body).encode()
    conn.request("POST", path, body=data, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read() or b"{}")


def test_phone_ask_and_look(phone):
    server, hermes = phone
    assert post(server, "/ask", {"text": "What's on my calendar?"}) == (200, {"reply": "Sure thing."})
    chat = hermes.chats[-1]
    assert chat["session"].startswith("argus-phone-")
    assert chat["messages"][0]["role"] == "system" and "Siri" in chat["messages"][0]["content"]
    assert post(server, "/look", {"image": PNG})[0] == 200
    parts = hermes.chats[-1]["messages"][1]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_phone_rejects_bad_requests(phone):
    server, hermes = phone
    assert post(server, "/ask", {"text": "hi"}, token="nope")[0] == 401
    assert post(server, "/ask", {"text": ""})[0] == 400
    assert post(server, "/look", {"image": "zzz"})[0] == 400
    assert post(server, "/admin", {"text": "hi"})[0] == 404
    assert hermes.chats == []
