import base64
import json
import os
import stat

import pytest

from argus import config as config_mod
from argus.audit import Audit
from argus.control import ControlServer, send
from argus.guard import Guard, is_blocked_app, read_state, write_state
from argus.phone import Gatekeeper, image_data_url, load_or_create_token


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
        'name = "Argus"\nuser_name = "Sandeep"\ncity = "Portland, OR"\n[voice]\nengine = "say"\nrate = 200\n'
        '[security]\nblocked_sites = ["mybank.com"]\n[phone]\nenabled = true\nport = 9999\n'
        '[brain]\nworkspace = "~/ws"\n[listening]\nclap = "single"\n')
    s = config_mod.load()
    assert (s.user_name, s.voice_engine, s.rate, s.blocked_sites, s.phone_enabled, s.phone_port) == \
        ("Sandeep", "say", 200, ["mybank.com"], True, 9999)
    assert s.clap == "single" and str(s.workspace).endswith("/ws")
    assert "com.1password.1password" in s.blocked_apps


def test_settings_left_over_from_telegram_are_ignored(home):
    home.mkdir(parents=True)
    (home / "config.toml").write_text('[security]\ntelegram_desktop = false\nscreen_control = false\n'
                                      '[telegram]\nenabled = true\nuser_id = 987654321\n')
    s = config_mod.load()                              # an old config.toml still loads
    assert s.screen_control is False and not hasattr(s, "telegram_user_id")


def test_config_typos_are_errors(home):
    home.mkdir(parents=True)
    for bad in ('[secutiry]\nblocked_sites = ["mybank.com"]\n', '[security]\nblocked_site = ["mybank.com"]\n'):
        (home / "config.toml").write_text(bad)
        with pytest.raises(ValueError):
            config_mod.load()


def test_secrets_file_must_be_private(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("ANTHROPIC_API_KEY=sk-ant-x\n# note\nTELEGRAM_BOT_TOKEN='1:abc'\n")
    os.chmod(p, 0o600)
    assert config_mod.read_secrets(p) == {"ANTHROPIC_API_KEY": "sk-ant-x", "TELEGRAM_BOT_TOKEN": "1:abc"}
    os.chmod(p, 0o644)
    with pytest.raises(PermissionError):
        config_mod.read_secrets(p)


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


def test_usage_adds_up_spend_from_the_activity_log(home, capsys):
    from argus.__main__ import cmd_usage
    audit = Audit(config_mod.load().audit_path)
    audit.log("turn", cost_usd=0.0123, web_searches=2)
    audit.log("turn", cost_usd=0.0200)
    audit.log("conversation_compacted", cost_usd=0.0007)
    audit.log("request", text="no cost here")
    assert cmd_usage(None) == 0
    out = capsys.readouterr().out
    assert "Today: $0.03 over 2 requests" in out and "including 2 web searches" in out
