"""Server mode (an always-on Linux machine): the phone gateway with the phone's location and navigation links,
reminders as push notifications, what the server may and may not do, and running it for real, as installed by
scripts/setup_server.sh."""
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from argus.audit import Audit
from argus.phone import Gatekeeper, PhoneServer
from fakes import Script, call, make_settings, say, wait_for
from mock_claude import ClaudeMock
from web_fakes import JsonService, routed_to

REPO = Path(__file__).resolve().parents[1]
TOPIC = "argus-" + "t" * 30


def post(port, path, body, token):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("POST", path, body=json.dumps(body).encode(),
                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read() or b"{}")


def test_gateway_passes_the_location_and_returns_the_link(tmp_path):
    asked = []

    def ask(content, location=None):
        asked.append((content, location))
        if location:
            return {"reply": "Twenty minutes via I-5.", "open": "https://maps.apple.com/?daddr=PDX&dirflg=d"}
        return {"reply": "Hello.", "open": "javascript:alert(1)"}
    server = PhoneServer(("127.0.0.1", 0), ask, Gatekeeper("phone-secret-token"), Audit(tmp_path / "audit.jsonl"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body = post(server.server_port, "/ask", {"text": "traffic to PDX?", "location": "45.52,-122.67"},
                            "phone-secret-token")
        assert status == 200 and body == {"reply": "Twenty minutes via I-5.",
                                          "open": "https://maps.apple.com/?daddr=PDX&dirflg=d"}
        assert asked[-1] == ("traffic to PDX?", {"lat": 45.52, "lon": -122.67})
        assert post(server.server_port, "/ask", {"text": "hi", "location": {"lat": "nowhere"}}, "phone-secret-token") == \
            (200, {"reply": "Hello."})                                   # a bad location is ignored; odd links dropped
        assert asked[-1] == ("hi", None)
        apps = {"Apple Maps": "https://maps.apple.com/?daddr=PDX&dirflg=d", "Waze": "https://waze.com/ul?q=PDX&navigate=yes",
                "Odd": "http://plain.example/", "Worse": 7}
        server.ask = lambda content, location=None: {"reply": "Going.", "open": apps["Apple Maps"], "links": apps}
        status, body = post(server.server_port, "/ask", {"text": "to PDX"}, "phone-secret-token")
        assert body == {"reply": "Going.", "open": apps["Apple Maps"],                 # one link per app on the phone
                        "links": {"Apple Maps": apps["Apple Maps"], "Waze": apps["Waze"]}}
        server.ask = lambda content, location=None: {"reply": "Going.", "links": {"Waze": apps["Waze"]}}
        assert post(server.server_port, "/ask", {"text": "to PDX"}, "phone-secret-token")[1] == {"reply": "Going."}
        log = (tmp_path / "audit.jsonl").read_text()
        assert '"location": true' in log and "45.52" not in log           # where you are isn't written down
    finally:
        server.shutdown()


@pytest.fixture
def server_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    made = []

    def build(responder, *, push=True, google=None, **overrides):
        from argus import config
        from argus.maps import Router, http_json
        from argus.push import Push
        from argus.server import ServerApp
        mock = ClaudeMock(responder).start()
        options = dict(api_base=mock.url, self_learning=False, phone_enabled=True, phone_host="127.0.0.1",
                       phone_port=0, push_enabled=True)
        s = make_settings(tmp_path, **dict(options, **overrides))
        config.ensure_private_dirs(s)
        ntfy = JsonService(lambda request: (200, {"id": "1"}))
        secrets = {"ANTHROPIC_API_KEY": "sk-ant-test"}
        request = http_json
        if google is not None:
            secrets["GOOGLE_MAPS_API_KEY"] = "AIza-test"
            request = routed_to(google, "/directions")
        app = ServerApp(s, secrets=secrets, router=Router(s, secrets, request=request),
                        push=Push(ntfy.url, TOPIC) if push else None)
        made.append(app)
        return app, mock, ntfy
    yield build
    for app in made:
        app.stop()


def test_server_answers_the_phone_with_live_traffic(server_app):
    google = JsonService(lambda request: (200, {"routes": [
        {"duration": "1500s", "staticDuration": "1450s", "distanceMeters": 24000, "description": "I-205 N"},
        {"duration": "2100s", "staticDuration": "1300s", "distanceMeters": 19800, "description": "I-5 N"}]}))
    script = Script(call("route", {"destination": "Portland Airport"}), say("Take I-205: 25 minutes, 10 faster than I-5."))
    app, mock, _ = server_app(script, google=google)
    assert app.start_gateway()
    token = app.settings.phone_token_path.read_text().strip()
    status, body = post(app.phone.server_port, "/ask", {"text": "fastest way to the airport?",
                                                        "location": {"latitude": 45.5, "longitude": -122.6}}, token)
    assert status == 200 and body["reply"] == "Take I-205: 25 minutes, 10 faster than I-5."
    assert body["open"] == "https://maps.apple.com/?daddr=Portland+Airport&dirflg=d"
    assert google.requests[0]["body"]["origin"]["location"]["latLng"] == {"latitude": 45.5, "longitude": -122.6}
    first = script.bodies[0]
    tools = {t["name"] for t in first["tools"]}
    assert "route" in tools and "run_command" not in tools and "open_app" not in tools and "read_file" in tools
    assert not any(t.get("type", "").startswith("computer") for t in first["tools"])
    assert "You run on a small always-on server" in first["system"][0]["text"]
    assert "Channel: Siri or the Argus iPhone app" in first["system"][1]["text"]
    assert token not in json.dumps(script.bodies)                     # the phone token never reaches Claude
    status = app.status()
    assert status["phone_gateway"].startswith("http://127.0.0.1:") and status["push"] is True
    assert status["maps"] == "live traffic from Google Routes API"


def test_server_sends_reminders_and_results_to_the_phone(server_app, tmp_path):
    app, _, ntfy = server_app(Script(say("Sunny, 18 degrees."), say("All done.")))
    app.deliver("Reminder: • call home")
    assert ntfy.requests[-1]["body"]["message"] == "Reminder: - call home"
    assert app.run_task("weather for tomorrow") == "Sunny, 18 degrees."
    handlers = app.handlers()
    assert handlers["ask"]({"text": "anything?"}) == {"ok": True, "reply": "All done."}
    assert handlers["ask"]({"text": "  "}) == {"ok": False, "reply": ""}
    assert handlers["say"]({"text": "test push"}) == {"ok": True} and ntfy.requests[-1]["body"]["message"] == "test push"
    assert handlers["say"]({"text": ""}) == {"ok": False}
    assert handlers["status"]({})["mode"] == "server"
    quiet, _, _ = server_app(Script(), push=False)
    quiet.deliver("Reminder: nobody hears this")
    assert '"delivery_failed"' in (tmp_path / "home" / "logs" / "audit.jsonl").read_text() or \
        "delivery_failed" in Path(quiet.settings.audit_path).read_text()
    assert quiet.handlers()["say"]({"text": "x"}) == {"ok": False}


def test_server_refuses_anything_risky(server_app):
    script = Script(call("write_file", {"path": "/etc/cron.d/x", "content": "boom"}), say("I can't do that here."),
                    call("schedule", {"action": "create", "text": "buy shares", "mode": "task", "at": "2099-01-01T09:00"}),
                    say("That needs your OK at the Mac."))
    app, _, _ = server_app(script)
    assert app.ask_phone("write a cron job") == {"reply": "I can't do that here.", "open": None}
    result = script.bodies[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "only works inside its own workspace" in result["content"]
    assert app.ask_phone("schedule a trade")["reply"] == "That needs your OK at the Mac."
    result = script.bodies[3]["messages"][-1]["content"][0]
    assert result["is_error"] and "declined" in result["content"]


def test_gateway_waits_for_tailscale(server_app, monkeypatch):
    from argus import server as server_mod
    app, _, _ = server_app(Script())
    answers = [None, "127.0.0.1"]
    monkeypatch.setattr(server_mod, "bind_address", lambda settings: answers.pop(0) if answers else "127.0.0.1")
    t = threading.Thread(target=app._gateway_loop, args=(0.05,), daemon=True)
    t.start()
    t.join(5)
    assert app.phone is not None and not answers
    app2, _, _ = server_app(Script())
    calls = []

    def failing(settings):
        calls.append(1)
        if len(calls) < 3:
            raise OSError("Cannot assign requested address")
        app2._stop.set()
        return None
    monkeypatch.setattr(server_mod, "bind_address", failing)
    app2._gateway_loop(0.01)
    assert len(calls) == 3 and app2.phone is None


def test_server_start_and_stop(server_app):
    app, _, _ = server_app(Script())
    app.start(retry=0.05)
    assert wait_for(lambda: app.phone is not None, 5)
    app.stop()
    off, _, _ = server_app(Script(), phone_enabled=False)
    off.start()
    assert off.phone is None and off.status()["phone_gateway"] is None


def test_server_main_runs_until_stopped(tmp_path, monkeypatch, capsys):
    from argus import server as server_mod
    from argus.control import send
    home = tmp_path / "home"
    monkeypatch.setenv("ARGUS_HOME", str(home))
    home.mkdir(mode=0o700)
    (home / "config.toml").write_text("[phone]\nenabled = false\n[schedule]\nmorning_headlines = \"\"\n")
    assert server_mod.main(threading.Event()) == 2 and "no Claude API key" in capsys.readouterr().err
    (home / "secrets.env").write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
    (home / "secrets.env").chmod(0o644)
    assert server_mod.main(threading.Event()) == 2 and "chmod 600" in capsys.readouterr().err
    (home / "secrets.env").chmod(0o600)
    stop = threading.Event()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", server_mod.main(stop)), daemon=True)
    t.start()
    socket_path = home / "run" / "argus.sock"
    assert wait_for(socket_path.exists, 10)
    assert send(socket_path, "status")["mode"] == "server"
    assert server_mod.main(threading.Event()) == 0 and "already running" in capsys.readouterr().out
    stop.set()
    t.join(10)
    assert result["code"] == 0
    (home / "config.toml").write_text("[phone]\nport = 0\n")
    assert server_mod.main(threading.Event()) == 2


def test_argusctl_server_command(monkeypatch):
    from argus import __main__ as cli
    from argus import server as server_mod
    monkeypatch.setattr(server_mod, "main", lambda: 7)
    assert cli.main(["server"]) == 7


def test_install_and_run_the_server_for_real(tmp_path):
    """scripts/setup_server.sh installs Argus into a root-owned style folder; started like systemd starts it
    (python3 -I run.py server, standard library only), it answers argus-admin and stops cleanly."""
    tailscale = tmp_path / "tailscale"
    tailscale.write_text("#!/bin/sh\necho 100.101.102.103\n")
    tailscale.chmod(0o755)
    python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    env = dict(os.environ, ARGUS_OPT_DIR=str(tmp_path / "opt" / "argus"), ARGUS_STATE_DIR=str(tmp_path / "var" / "argus"),
               ARGUS_UNIT_DIR=str(tmp_path / "systemd"), ARGUS_ADMIN_BIN=str(tmp_path / "sbin" / "argus-admin"),
               ARGUS_TAILSCALE=str(tailscale), ARGUS_PYTHON=python, ARGUS_ANTHROPIC_API_KEY="sk-ant-test",
               ARGUS_NAME="Sandeep", ARGUS_CITY="Portland, OR", ARGUS_TIMEZONE="America/Los_Angeles",
               ARGUS_SKIP_VALIDATION="1", ARGUS_PHONE="iphone")
    setup = subprocess.run(["bash", str(REPO / "scripts" / "setup_server.sh"), "--no-service"], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    assert setup.returncode == 0, setup.stdout + setup.stderr
    home = tmp_path / "var" / "argus" / ".argus"
    unit = (tmp_path / "systemd" / "argus.service").read_text()
    for line in ("User=argus", "NoNewPrivileges=yes", "ProtectSystem=strict", f"ReadWritePaths={tmp_path}/var/argus",
                 "ProtectHome=yes", "CapabilityBoundingSet=", "MemoryDenyWriteExecute=yes",
                 f"ExecStart={python} -I {tmp_path}/opt/argus/run.py server", "ARGUS_MODE=server"):
        assert line in unit, line
    secrets = (home / "secrets.env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-test" in secrets and "NTFY_TOPIC=argus-" in secrets
    assert oct((home / "secrets.env").stat().st_mode & 0o777) == "0o600"
    assert 'host = "100.101.102.103"' in (home / "config.toml").read_text()
    assert "sk-ant" not in (home / "config.toml").read_text() and "NTFY" not in (home / "config.toml").read_text()
    assert (tmp_path / "opt" / "argus" / "argus" / "server.py").exists()
    assert not (tmp_path / "opt" / "argus" / "argus" / "test_android.py").exists()
    # Point it at a stand-in Claude and at 127.0.0.1 (no Tailscale here), then start it like systemd does.
    config = (home / "config.toml").read_text().replace('host = "100.101.102.103"', 'host = "127.0.0.1"')
    mock = ClaudeMock(lambda body: ([{"type": "text", "text": "Server here."}], "end_turn")).start()
    (home / "config.toml").write_text(config.replace("[brain]\n", f'[brain]\napi_base = "{mock.url}"\n', 1)
                                      .replace("port = 8765", "port = 18799"))
    run_env = {"ARGUS_HOME": str(home), "ARGUS_MODE": "server", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.Popen([python, "-I", str(tmp_path / "opt" / "argus" / "run.py"), "server"], env=run_env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert wait_for((home / "run" / "argus.sock").exists, 20)
        admin = lambda *args: subprocess.run([python, "-I", str(tmp_path / "opt" / "argus" / "run.py"), *args],  # noqa
                                             env=run_env, capture_output=True, text=True, timeout=60)
        status = admin("status")
        assert status.returncode == 0 and '"mode": "server"' in status.stdout
        assert '"Server here."' in admin("ask", "hello").stdout
        token = admin("phone-token").stdout.strip()
        assert len(token) >= 32
        assert wait_for(lambda: post_ok(18799, token), 15)
        doctor = admin("doctor", "--offline")
        assert "Argus running: server" in doctor.stdout and "voice activity model" not in doctor.stdout
    finally:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, out


def post_ok(port, token):
    try:
        return post(port, "/ask", {"text": "ping"}, token) == (200, {"reply": "Server here."})
    except OSError:
        return False


def test_server_main_listens_for_stop_signals(tmp_path, monkeypatch):
    from argus import server as server_mod
    home = tmp_path / "home"
    monkeypatch.setenv("ARGUS_HOME", str(home))
    home.mkdir(mode=0o700)
    (home / "config.toml").write_text("[phone]\nenabled = false\n[schedule]\nmorning_headlines = \"\"\n")
    (home / "secrets.env").write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
    (home / "secrets.env").chmod(0o600)
    installed = {}
    monkeypatch.setattr(server_mod.signal, "signal", lambda sig, fn: installed.__setitem__(sig, fn))
    stop = threading.Event()
    stop.set()                                    # stop at once: this is about what main() sets up
    assert server_mod.main(stop) == 0
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    stop.clear()
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert stop.is_set()


def test_doctor_on_the_server(tmp_path, monkeypatch, capsys):
    from argus.__main__ import main
    from argus.control import ControlServer
    from argus import config
    home = tmp_path / "home"
    monkeypatch.setenv("ARGUS_HOME", str(home))
    monkeypatch.setenv("ARGUS_MODE", "server")
    home.mkdir(mode=0o700)
    (home / "config.toml").write_text('[push]\nenabled = true\n[maps]\ntraffic = "off"\n')
    (home / "secrets.env").write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
    (home / "secrets.env").chmod(0o600)
    assert main(["doctor", "--offline"]) == 1
    out = capsys.readouterr().out
    assert "no NTFY_TOPIC" in out and "sudo systemctl start argus" in out and "voice activity model" not in out
    assert "INFO  directions: no live-traffic service set up" in out
    (home / "secrets.env").write_text(f"ANTHROPIC_API_KEY=sk-ant-test\nNTFY_TOPIC={TOPIC}\n")
    s = config.load()
    config.ensure_private_dirs(s)
    server = ControlServer(s.socket_path, {"status": lambda r: {"mode": "server"}})
    server.start()
    try:
        assert main(["doctor", "--offline"]) == 0
        out = capsys.readouterr().out
        assert "PASS  push notifications set up" in out and "PASS  Argus running: server" in out
        assert "voice lock" not in out
    finally:
        server.stop()
    with pytest.raises(SystemExit):
        main(["status"])
    assert "Start it with: sudo systemctl start argus" in capsys.readouterr().out


def test_which_argus_this_is(monkeypatch):
    from argus import runtime
    for key in ("TERMUX_VERSION", "ARGUS_MODE", "PREFIX"):
        monkeypatch.delenv(key, raising=False)
    assert runtime.kind() == "mac"
    monkeypatch.setenv("ARGUS_MODE", "server")
    assert runtime.kind() == "server"
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert runtime.kind() == "android"


def test_logging_can_also_go_to_the_terminal(tmp_path):
    import logging
    from argus import runtime
    s = make_settings(tmp_path)
    s.workspace_dir = str(tmp_path / "ws")
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        import os as _os
        _os.environ["ARGUS_HOME"] = str(tmp_path / "home")
        runtime.setup_logging(s, verbose=True)
        assert any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers)
    finally:
        root.handlers[:] = before
        del os.environ["ARGUS_HOME"]
