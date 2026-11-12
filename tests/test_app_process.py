"""The real Argus process (`argusctl run --headless`) started, checked and stopped the way launchd does it,
with a stand-in Claude and a silent stand-in microphone. Needs the speech models (ARGUS_TEST_MODELS)."""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from argus.control import send

from fakes import wait_for
from mock_claude import ClaudeMock, rules

MODELS = Path(os.environ.get("ARGUS_TEST_MODELS", "/nonexistent"))
REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not (MODELS / "silero_vad.onnx").exists(), reason="speech models not available")


@pytest.fixture
def argus_home(tmp_path):
    home = tmp_path / "argus-home"
    (home / "models").mkdir(parents=True)
    for name, target in (("silero_vad.onnx", "silero_vad.onnx"), ("asr", "sherpa-onnx-moonshine-base-en-int8"),
                         ("tts", "vits-piper-en_US-ryan-medium")):
        os.symlink(MODELS / target, home / "models" / name)
    mock = ClaudeMock(rules).start()
    (home / "config.toml").write_text(
        f'user_name = "Test"\n[brain]\napi_base = "{mock.url}"\nworkspace = "{tmp_path / "ws"}"\n'
        '[schedule]\nmorning_headlines = ""\n')
    secrets = home / "secrets.env"
    secrets.write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
    secrets.chmod(0o600)
    home.chmod(0o700)
    yield home
    mock.shutdown()


def start(home, *args):
    env = dict(os.environ, ARGUS_HOME=str(home),
               PYTHONPATH=os.pathsep.join([str(REPO / "tests" / "fake_modules"), str(REPO)]))
    return subprocess.Popen([sys.executable, "-m", "argus", "run", "--headless", *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def test_argus_starts_answers_refuses_a_second_copy_and_stops_cleanly(argus_home):
    proc = start(argus_home)
    sock = argus_home / "run" / "argus.sock"
    try:
        assert wait_for(sock.exists, timeout=60), proc.stdout.read() if proc.poll() is not None else "no socket"
        assert wait_for(lambda: send(sock, "status").get("mode") == "listening", timeout=30)
        assert send(sock, "ask", text="Argus, hello")["ok"]
        audit = argus_home / "logs" / "audit.jsonl"
        assert wait_for(lambda: audit.exists() and '"turn"' in audit.read_text(), timeout=30)
        status = send(sock, "status")
        assert status["pid"] == proc.pid and status["version"] and status["voice"] and status["safe_start"] is False
        startup = json.loads((argus_home / "run" / "startup.json").read_text())
        assert startup["pid"] == proc.pid and startup["phase"] == "listening" and startup["failed_starts"] == 0
        assert startup["dumps"] is True
        assert (argus_home / "run" / "argus.lock").read_text().split()[0] == str(proc.pid)
        second = start(argus_home)
        out, _ = second.communicate(timeout=60)
        assert second.returncode == 0 and f"already running (pid {proc.pid})" in out
        assert (argus_home / "logs" / "argus.log").exists()
        assert oct((argus_home / "logs" / "argus.log").stat().st_mode & 0o777) == "0o600"
        assert "--- Argus started, pid" in (argus_home / "logs" / "crash.log").read_text()
    finally:
        proc.send_signal(signal.SIGTERM)                       # what `argusctl quit` / launchd sends
        proc.wait(timeout=30)
    assert proc.returncode == 0
    log = (argus_home / "logs" / "audit.jsonl").read_text()
    assert '"stopped", "signal": 15' in log and '"started", "name": "Argus", "version"' in log
    assert not sock.exists()
    assert json.loads((argus_home / "run" / "startup.json").read_text())["phase"] == "stopped"


def test_a_restart_right_after_a_stop_comes_back(argus_home):
    """The restart race: the new copy starts while the old one is still closing, and must not give up."""
    first = start(argus_home)
    sock = argus_home / "run" / "argus.sock"
    try:
        assert wait_for(lambda: sock.exists() and send(sock, "status").get("mode") == "listening", timeout=60)
        first.send_signal(signal.SIGTERM)
        second = start(argus_home)                              # at once, like launchd after kickstart -k
        assert first.wait(timeout=30) == 0
        assert wait_for(lambda: sock.exists() and send(sock, "status").get("pid") == second.pid, timeout=60), \
            second.stdout.read() if second.poll() is not None else "no answer"
    finally:
        for proc in (first, locals().get("second")):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=30)


def test_argus_explains_why_it_cant_start(argus_home):
    (argus_home / "secrets.env").chmod(0o644)                 # readable by others: refuse to run with it
    proc = start(argus_home)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 2 and "Argus could not start" in out and "chmod 600" in out
    (argus_home / "secrets.env").chmod(0o600)
    (argus_home / "config.toml").write_text('[security]\nscreen_control = "yes"\n')
    proc = start(argus_home)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 2 and "problem in Argus's settings" in out and "true or false" in out
    assert "Traceback" not in out
