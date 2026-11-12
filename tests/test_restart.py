"""Restarting has to work every time: a new copy waits for the old one, a stuck copy is stopped, stopping never
hangs, failed starts are noticed (and the next one is a safe one), and `argusctl restart` says whether Argus
really came back. Plus the report and status that show where things stand, and `argusctl voice`."""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from argus import config, diagnose, runtime, service
from argus.control import ControlServer

from fakes import SPEECH, FakeVoicePrint, make_argus, wait_for

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    monkeypatch.delenv("ARGUS_MODE", raising=False)
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setenv("PREFIX", "/usr")
    settings = config.load()
    config.ensure_private_dirs(settings)
    return settings


HOLDER = textwrap.dedent("""
    import fcntl, os, sys, time
    lock, age, seconds = sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
    f = open(lock, "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    f.seek(0); f.truncate(); f.write(f"{os.getpid()} {int(time.time()) - age}\\n"); f.flush()
    print("locked", flush=True)
    time.sleep(seconds)
""")


def hold_lock(settings, *, age=0, seconds=60.0, looks_like_argus=True):
    """Another process holding Argus's lock. Like the real one, it is Python running `-m argus run` (a stand-in
    argus package); with looks_like_argus=False it is some other Python program."""
    args = [str(settings.run_dir / "argus.lock"), str(age), str(seconds)]
    if looks_like_argus:
        fake = settings.home / "stand-in" / "argus"
        fake.mkdir(parents=True, exist_ok=True)
        (fake / "__init__.py").write_text("")
        (fake / "__main__.py").write_text(HOLDER)
        proc = subprocess.Popen([sys.executable, "-m", "argus", "run", *args], stdout=subprocess.PIPE, text=True,
                                cwd=fake.parent, env=dict(os.environ, PYTHONPATH=str(fake.parent)))
    else:
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, "run", *args], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_argus_is_told_apart_by_its_command_line():
    for command in ("/Users/me/.argus/venv/bin/python3 -I -m argus run", "python3.12 -m argus server",
                    "/usr/bin/python -X importtime -m argus run --headless",
                    "/Users/me/Applications/Argus.app/Contents/MacOS/Argus"):
        assert runtime.argus_command(command), command
    for command in ("bash -c python3 -m argus run", "python3 -m argus report", "python3 -c import x -m argus run",
                    "python3 script.py -m argus run", "vim notes -m argus run", "python3 -m argusx run", "", "python3"):
        assert not runtime.argus_command(command), command


# ---- one copy at a time ---------------------------------------------------------------------------------
def test_a_restarted_copy_waits_for_the_old_one_to_close(home):
    old = hold_lock(home, seconds=1.5)
    began = time.monotonic()
    lock = runtime.single_instance(home, wait=10)
    assert lock is not None and 1.0 < time.monotonic() - began < 8
    assert runtime.lock_holder(home)[0] == os.getpid()
    old.wait(5)
    lock.close()


def test_a_working_copy_is_left_alone(home):
    other = hold_lock(home)
    server = ControlServer(home.socket_path, {"status": lambda req: {"ok": True, "pid": other.pid}})
    server.start()
    try:
        began = time.monotonic()
        assert runtime.single_instance(home, wait=10) is None
        assert time.monotonic() - began < 3 and other.poll() is None      # answered: not stuck, not touched
    finally:
        server.stop()
        other.kill()
        other.wait()


def test_a_stuck_copy_is_stopped_so_the_new_one_can_start(home):
    stuck = hold_lock(home, age=500)                  # holds the lock, answers nothing, for minutes
    told = []
    lock = runtime.single_instance(home, wait=1.0, on_stuck=told.append)
    try:
        assert lock is not None and told == [stuck.pid]
        assert stuck.wait(5) is not None
    finally:
        if stuck.poll() is None:
            stuck.kill()


def test_a_copy_that_is_closing_is_waited_for_even_if_it_still_answers(home):
    old = hold_lock(home, seconds=1.5)
    (home.run_dir / "startup.json").write_text(json.dumps({"pid": old.pid, "phase": "stopped"}))
    server = ControlServer(home.socket_path, {"status": lambda req: {"ok": True, "pid": old.pid}})
    server.start()
    try:
        lock = runtime.single_instance(home, wait=10)
        assert lock is not None
        lock.close()
    finally:
        server.stop()
        old.wait(5)


def test_an_older_argus_that_didnt_write_its_pid_is_found_by_its_command_line(home, monkeypatch):
    old = hold_lock(home, age=500)
    (home.run_dir / "argus.lock").write_text("")      # 2.9 and before opened the lock file empty
    # (only this test's stand-in: other Argus processes on this machine, such as a parallel test run, are left alone)
    monkeypatch.setattr(runtime, "argus_processes", lambda: [(old.pid, "python -c ... -m argus run")])
    told = []
    lock = runtime.single_instance(home, wait=0.6, on_stuck=told.append)
    try:
        assert lock is not None and told == [old.pid] and old.wait(5) is not None
    finally:
        if old.poll() is None:
            old.kill()
    lock.close()
    other = hold_lock(home, seconds=30)
    (home.run_dir / "argus.lock").write_text("")
    try:
        monkeypatch.setattr(runtime, "argus_processes", lambda: [(other.pid, "python -m argus run")])
        assert f"running as pid {other.pid} but doesn't answer" in "\n".join(service.why_not(home, {"loaded": False}))
    finally:
        other.kill()
        other.wait()


def test_only_argus_is_ever_stopped(home):
    other = hold_lock(home, age=500, looks_like_argus=False)      # something else holding the file
    try:
        assert runtime.single_instance(home, wait=0.6) is None and other.poll() is None
        assert not runtime.is_argus(other.pid) and runtime.is_argus(None) is False
    finally:
        other.kill()
        other.wait()


def test_a_copy_that_is_still_loading_is_not_taken_for_stuck(home):
    loading = hold_lock(home, age=5)                  # started 5 s ago, models still loading
    try:
        assert runtime.single_instance(home, wait=0.6) is None and loading.poll() is None
    finally:
        loading.kill()
        loading.wait()


def test_argus_processes_are_found_by_their_command_line(home):
    proc = hold_lock(home, seconds=30)
    try:
        assert proc.pid in [pid for pid, _ in runtime.argus_processes()]
        assert runtime.end_process(proc.pid, grace=1.0) and not runtime.pid_alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert runtime.end_process(proc.pid) is True                  # already gone
    assert runtime.pid_alive(None) is False and runtime.pid_alive(-1) is False


# ---- how far a start got ----------------------------------------------------------------------------------
def test_failed_starts_bring_the_next_one_up_safely(home):
    def crashed_while(phase, failed=0, good=False, stopped=False):
        (home.run_dir / "startup.json").write_text(json.dumps(
            {"pid": 999999, "phase": phase, "failed_starts": failed, "good": good, "stopped": stopped,
             "error": "boom", "version": "2.9.0"}))
    crashed_while("loading: voice")
    first = runtime.Startup.begin(home, "2.10.0")
    assert first.failed_starts == 1 and not first.safe
    crashed_while("listening", failed=1)
    second = runtime.Startup.begin(home, "2.10.0")
    assert second.failed_starts == 2 and second.safe and second.data["previous"]["phase"] == "listening"
    second.listening(good_after=0.05)                 # listening a while: a good start again
    assert wait_for(lambda: runtime.read_startup(home).get("good") is True, timeout=3)
    assert runtime.read_startup(home)["failed_starts"] == 0 and runtime.read_startup(home)["safe"] is False
    crashed_while("listening", failed=3, good=True)   # a long run that ended: not a failed start
    assert runtime.Startup.begin(home).failed_starts == 0
    crashed_while("stopped", failed=1, stopped=True)
    assert runtime.Startup.begin(home).failed_starts == 0
    start = runtime.Startup.begin(home)
    start.failed("no microphone\n at all")
    assert runtime.read_startup(home)["error"] == "no microphone at all"
    start.stopped()
    assert runtime.read_startup(home)["phase"] == "stopped" and oct((home.run_dir / "startup.json").stat().st_mode & 0o777) == "0o600"


# ---- stopping never hangs ---------------------------------------------------------------------------------
STOPPER = textwrap.dedent("""
    import ctypes, sys, threading, time
    sys.path.insert(0, sys.argv[2])
    from argus import runtime
    def stop(signum):
        open(sys.argv[1], "w").write(str(signum))
        time.sleep(100)                     # part of the shutdown hangs
    runtime.stop_on_signals(stop, deadline=1.0)
    print("ready", flush=True)
    sleeper = threading.Thread(target=time.sleep, args=(100,), daemon=True)
    sleeper.start()
    # The main thread stuck in native code that never returns to Python (like a menu bar run loop):
    # pthread_join isn't interrupted by signals, so only the signal thread can notice the stop.
    ctypes.CDLL(None).pthread_join(ctypes.c_ulong(sleeper.ident), None)
""")


def test_stopping_works_even_when_the_main_thread_and_the_shutdown_are_stuck(tmp_path):
    flag = tmp_path / "stopped"
    proc = subprocess.Popen([sys.executable, "-c", STOPPER, str(flag), str(REPO)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    time.sleep(0.3)
    began = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(10) == 0 and time.monotonic() - began < 5
    assert flag.read_text() == str(int(signal.SIGTERM))


# ---- crashes and hangs leave a trace -------------------------------------------------------------------
CRASHER = textwrap.dedent("""
    import ctypes, os, signal, sys, time
    sys.path.insert(0, sys.argv[1])
    os.environ["ARGUS_HOME"] = sys.argv[2]
    from argus import config, runtime
    s = config.load(); config.ensure_private_dirs(s)
    runtime.capture_crashes(s)
    os.kill(os.getpid(), signal.SIGUSR1)       # where every thread is, without stopping
    time.sleep(0.3)
    ctypes.string_at(0)                        # a crash in native code
""")


def test_a_native_crash_and_a_hang_leave_a_trace(tmp_path):
    home = tmp_path / "h"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "crash.log").write_text("old crash\n" * 120_000)      # over 1 MB: started again, small
    proc = subprocess.run([sys.executable, "-c", CRASHER, str(REPO), str(home)], capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    logs = home / "logs"
    assert "File" in (logs / "threads.log").read_text()
    crash = diagnose.last_crash(logs / "crash.log")
    assert crash[0].startswith("--- Argus started") and any("Fatal Python error" in line for line in crash)
    assert oct((logs / "crash.log").stat().st_mode & 0o777) == "0o600" and "old crash" not in (logs / "crash.log").read_text()


def test_a_running_argus_shows_where_it_is_without_stopping(tmp_path, monkeypatch):
    home = tmp_path / "h"
    script = textwrap.dedent("""
        import os, sys, time
        sys.path.insert(0, sys.argv[1]); os.environ["ARGUS_HOME"] = sys.argv[2]
        from argus import config, runtime
        s = config.load(); config.ensure_private_dirs(s); runtime.capture_crashes(s)
        print("ready", flush=True)
        def waiting_for_the_voice(): time.sleep(60)
        waiting_for_the_voice()
    """)
    proc = subprocess.Popen([sys.executable, "-c", script, str(REPO), str(home)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        monkeypatch.setenv("ARGUS_HOME", str(home))
        dump = runtime.dump_threads(config.load(), proc.pid)
        assert "waiting_for_the_voice" in dump and proc.poll() is None
        assert any("waiting_for_the_voice" in line for line in diagnose.summarize_threads(dump))
    finally:
        proc.kill()
        proc.wait()
    assert runtime.dump_threads(config.load(), 999999) == ""


# ---- `argusctl restart` says whether Argus came back --------------------------------------------------------
class Launchd:
    """launchctl, as far as restart needs it."""

    def __init__(self, loaded=True, state="running", last_exit="0", on_kick=lambda: None, fail=None):
        self.loaded, self.state, self.last_exit, self.on_kick, self.fail = loaded, state, last_exit, on_kick, fail
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        verb = args[1]
        if self.fail and verb == self.fail[0]:
            return 1, self.fail[1]
        if verb == "print":
            if not self.loaded:
                return 113, "Could not find service"
            return 0, f"gui/501/com.argus.voice = {{\n\tstate = {self.state}\n\tpid = 4242\n\tlast exit code = {self.last_exit}\n}}"
        if verb in ("kickstart", "bootstrap"):
            self.loaded = True
            self.on_kick()
        if verb == "bootout":
            self.loaded = False
        return 0, ""


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def restart(home, launchd, **kw):
    clock = Clock()
    lines = []
    code = service.restart(home, run=launchd, out=lines.append, sleep=clock.sleep, clock=clock, **kw)
    return code, "\n".join(lines)


def test_restart_waits_until_the_new_argus_answers(home):
    pids = {"now": 111}
    server = ControlServer(home.socket_path, {"status": lambda req: {
        "ok": True, "pid": pids["now"], "mode": "listening", "version": "2.10.0", "voice": "natural (af_sky, 1.2x)"}})
    server.start()
    try:
        launchd = Launchd(on_kick=lambda: pids.update(now=222))
        code, out = restart(home, launchd)
        assert code == 0 and "Argus is back and listening (listening; version 2.10.0; voice: natural (af_sky" in out
        assert ["launchctl", "kickstart", "-k", service.target()] in launchd.calls
    finally:
        server.stop()


def test_restart_says_why_a_start_failed(home):
    err = home.audit_path.parent / "voice.err.log"
    err.write_text("an old line from yesterday\n")

    def fails():
        err.write_text(err.read_text() + "Traceback (most recent call last):\nRuntimeError: no voice model in /x\n")
        (home.run_dir / "startup.json").write_text(json.dumps(
            {"pid": 333, "started": time.time(), "phase": "failed", "phase_since": time.time(),
             "error": "no voice model in /x"}))
    code, out = restart(home, Launchd(on_kick=fails))
    assert code == 1 and "Argus couldn't start." in out and "The last start failed at" in out
    assert "RuntimeError: no voice model in /x" in out and "an old line" not in out


def test_restart_notices_a_crash_loop(home):
    code, out = restart(home, Launchd(state="spawn scheduled", last_exit="1"))
    assert code == 1 and "Argus stopped while starting." in out and "exit code 1" in out


def test_restart_gives_up_after_waiting_and_says_what_it_knows(home):
    code, out = restart(home, Launchd(), wait=5)
    assert code == 1 and "didn't answer within 5 seconds" in out and "argusctl report" in out


def test_restart_loads_the_service_when_it_was_unloaded(home, monkeypatch, tmp_path):
    plist = tmp_path / "com.argus.voice.plist"
    monkeypatch.setattr(service, "plist_path", lambda: plist)
    code, out = restart(home, Launchd(loaded=False), wait=1)
    assert code == 1 and "isn't set up to start at login. Run ./setup.sh" in out
    plist.write_text("<plist/>")
    launchd = Launchd(loaded=False)
    code, out = restart(home, launchd, wait=1)
    assert "Starting Argus" in out and any(c[1] == "bootstrap" for c in launchd.calls)
    code, out = restart(home, Launchd(fail=("kickstart", "Operation not permitted")))
    assert code == 1 and "macOS didn't start it: Operation not permitted" in out


def test_restart_force_stops_every_copy_first(home, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "p.plist")
    (tmp_path / "p.plist").write_text("<plist/>")
    ended = []
    monkeypatch.setattr(runtime, "argus_processes", lambda: [(5150, "python -m argus run")])
    monkeypatch.setattr(runtime, "end_process", lambda pid, **kw: ended.append(pid) or True)
    launchd = Launchd()
    code, out = restart(home, launchd, force=True, wait=1)
    assert "Stopping every copy of Argus" in out and ended == [5150]
    assert [c[1] for c in launchd.calls][:3] == ["print", "bootout", "bootstrap"]


def test_restart_elsewhere_points_to_the_right_command(home, monkeypatch):
    monkeypatch.setenv("ARGUS_MODE", "server")
    code, out = restart(home, Launchd())
    assert code == 1 and "sudo systemctl start argus" in out


def test_quit_stops_it(home):
    launchd = Launchd()
    lines = []
    assert service.stop(home, run=launchd, out=lines.append) == 0
    assert ["launchctl", "kill", "TERM", service.target()] in launchd.calls and "Argus stopped" in lines[-1]


def test_restart_details(home, monkeypatch, tmp_path):
    assert service.run_command(["true"]) == (0, "") and service.run_command(["/no/such/launchctl"])[0] == 127
    assert service.plist_path().name == "com.argus.voice.plist"
    pids = {"now": 1}
    server = ControlServer(home.socket_path, {"status": lambda req: {"ok": True, "pid": pids["now"], "mode": "listening",
                                                                    "safe_start": True}})
    server.start()
    try:
        code, out = restart(home, Launchd(on_kick=lambda: pids.update(now=2)))
        assert code == 0 and "It started safely, with the Mac's own voice" in out
    finally:
        server.stop()
    # macOS still closing the old one: "Bootstrap failed: 5", but loaded after all -> kickstart
    plist = tmp_path / "p.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(service, "plist_path", lambda: plist)

    class Slow(Launchd):
        def __call__(self, args):
            if args[1] == "bootstrap":
                self.calls.append(args)
                self.loaded = True
                return 5, "Bootstrap failed: 5: Input/output error"
            return super().__call__(args)
    slow = Slow(loaded=False)
    restart(home, slow, wait=1)
    assert [c[1] for c in slow.calls][:4] == ["print", "bootstrap", "print", "kickstart"]

    class Refuses(Launchd):
        def __call__(self, args):
            self.calls.append(args)
            return (1, "Bootstrap failed: 125") if args[1] == "bootstrap" else (113, "Could not find service")
    code, out = restart(home, Refuses(loaded=False), wait=1)
    assert code == 1 and "macOS didn't start it: Bootstrap failed: 125" in out


def test_quit_without_launchd_stops_the_copy_that_holds_the_lock(home):
    holder = hold_lock(home, seconds=60)
    lines = []
    assert service.stop(home, run=Launchd(loaded=False), out=lines.append, wait=5) == 0
    assert holder.wait(5) is not None and "Argus stopped" in lines[-1]


def test_why_argus_isnt_answering(home):
    (home.run_dir / "startup.json").write_text(json.dumps({"pid": 999999, "phase": "loading: voice", "phase_since": 0}))
    lines = service.why_not(home, {"loaded": True, "state": "not running", "last_exit": "", "last_signal": "Segmentation fault: 11"})
    text = "\n".join(lines)
    assert "ended with Segmentation fault: 11" in text and "went away while 'loading: voice'" in text and "a crash" in text
    assert "isn't loaded" in "\n".join(service.why_not(home, {"loaded": False}))
    (home.run_dir / "startup.json").write_text(json.dumps({"pid": os.getpid(), "phase": "loading: voice", "phase_since": 0}))
    assert "doesn't answer" in "\n".join(service.why_not(home, {"loaded": True, "state": "running"}))
    assert service.job_state(lambda args: (0, "state = running\npid = 12\nlast exit code = (never exited)")) == {
        "loaded": True, "state": "running", "pid": 12, "last_exit": "(never exited)", "last_signal": ""}
    assert service.new_lines(home.run_dir / "nothing.log", 0) == []
    (home.run_dir / "startup.json").write_text(json.dumps({"pid": 999999, "phase": "stopped", "phase_since": 0}))
    assert "Argus was stopped at" in "\n".join(service.why_not(home, {"loaded": True, "state": "not running", "last_exit": "0"}))
    other = hold_lock(home, seconds=30)
    try:
        assert f"Another copy of Argus (pid {other.pid}) holds the lock" in "\n".join(service.why_not(home, {"loaded": False}))
    finally:
        other.kill()
        other.wait()


def test_status_in_plain_words():
    lines = "\n".join(service.describe_status({
        "mode": "paused (on a call: Chrome)", "version": "2.10.0", "pid": 7, "up_s": 3720, "voice": "the Mac's own voice",
        "talk_over": "your voice", "mic_open": False, "on_call": ["Chrome"], "asleep": True, "safe_start": True,
        "last_error": "Claude API error 529"}))
    assert "Argus is paused (on a call: Chrome) (version 2.10.0, pid 7, up 1 h 2 min)." in lines
    assert "Paused while Chrome uses a microphone" in lines and "ignore_mic_apps" in lines
    assert "Asleep" in lines and "Started safely" in lines and "Claude API error 529" in lines
    silent = "\n".join(service.describe_status({"mode": "listening", "mic_open": True, "mic_silent": True}))
    assert "only silence comes in" in silent and "Privacy & Security" in silent
    assert "sound 3 s ago" in "\n".join(service.describe_status({"mode": "listening", "mic_open": True, "sound_s_ago": 3}))


def test_argusctl_status_and_restart_commands(home, monkeypatch, capsys):
    from argus.__main__ import main
    with pytest.raises(SystemExit):
        main(["status"])
    out = capsys.readouterr().out
    assert "Argus isn't running." in out and "Start it with: argusctl restart" in out
    server = ControlServer(home.socket_path, {"status": lambda req: {"ok": True, "mode": "listening", "pid": 9,
                                                                    "talk_over": "your voice"}})
    server.start()
    try:
        assert main(["status"]) == 0 and "Argus is listening" in capsys.readouterr().out
        assert main(["status", "--json"]) == 0 and '"pid": 9' in capsys.readouterr().out
    finally:
        server.stop()
    seen = []
    monkeypatch.setattr(service, "restart", lambda s, force, wait: seen.append((force, wait)) or 0)
    monkeypatch.setattr(service, "stop", lambda s: seen.append("stop") or 0)
    assert main(["restart", "--force", "--wait", "30"]) == 0 and main(["quit"]) == 0
    assert seen == [(True, 30.0), "stop"]


# ---- the report shows where things stand right now ------------------------------------------------------------
def test_the_report_shows_why_argus_is_stuck(home, monkeypatch):
    logs = home.audit_path.parent
    (logs / "voice.err.log").write_text(f"Argus is already running.\nloading {Path.home()}/.argus/models\n")
    (logs / "crash.log").write_text("--- Argus started, pid 5, 2026-11-12 10:20:01 ---\nFatal Python error: Segmentation fault\n\n"
                                    "Current thread 0x1 (most recent call first):\n  File \"voice.py\", line 9 in speak\n")
    (logs / "audit.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"ts": "2026-11-12T10:20:01", "event": "started", "version": "2.10.0", "pid": 5, "safe": False},
        {"ts": "2026-11-12T10:20:30", "event": "mic_silent", "seconds": 20},
        {"ts": "2026-11-12T10:21:00", "event": "stopped", "signal": 15},
        {"ts": "2026-11-12T10:21:05", "event": "safe_start", "failed_starts": 2},
        {"ts": "2026-11-12T10:21:06", "event": "stuck_copy_stopped", "pid": 77}]) + "\n")
    (home.run_dir / "startup.json").write_text(json.dumps({"pid": os.getpid(), "phase": "loading: voice", "phase_since": 0,
                                                           "dumps": True}))
    monkeypatch.setattr(runtime, "is_argus", lambda pid: True)
    dump = "Thread 0x1 (most recent call first):\n  File \"a.py\", line 1 in f\n  File \"b.py\", line 2 in g\n"
    text = diagnose.build_report(home, {}, "2.10.0", run=lambda args: (0, "state = running"), dump=lambda s, pid: dump)
    assert "Right now\n  Argus isn't answering." in text and "is 'loading: voice'" in text
    assert "Where it is right now" in text and "      File \"a.py\", line 1 in f" in text
    assert "started, version 2.10.0, pid 5" in text and "stopped (signal 15)" in text
    assert "The microphone gave only silence for 20 s" in text and "started safely" in text and "pid 77" in text
    assert "Argus is already running." in text and "loading ~/.argus/models" in text and str(Path.home()) not in text
    assert "The last crash" in text and "Segmentation fault" in text and "your voice enrolled: no" in text
    (home.run_dir / "startup.json").write_text(json.dumps({"pid": os.getpid(), "phase": "loading: voice", "phase_since": 0}))
    asked = []                                        # a process that can't take the snapshot isn't sent the signal
    diagnose.build_report(home, {}, run=lambda args: (0, ""), dump=lambda s, pid: asked.append(pid) or "")
    assert asked == []


def test_the_report_when_argus_answers(home):
    server = ControlServer(home.socket_path, {"status": lambda req: {
        "ok": True, "pid": 5, "mode": "listening", "version": "0.0.1", "up_s": 60, "voice": "quick", "mic_open": True}})
    server.start()
    try:
        text = diagnose.build_report(home, {}, "2.10.0")
    finally:
        server.stop()
    assert "Right now\n  Argus is listening (version 0.0.1" in text and "but " in text and "is installed: argusctl restart" in text


# ---- what Argus says about itself --------------------------------------------------------------------------
def test_status_has_what_a_restart_and_the_report_need(tmp_path):
    argus, voice, _ = make_argus(tmp_path, [])
    argus.extra_status = lambda: {"version": "2.10.0", "voice": "quick"}
    st = argus.status()
    assert st["pid"] == os.getpid() and st["version"] == "2.10.0" and st["voice"] == "quick" and st["mic_silent"] is False
    assert st["talk_over"] == "by name only until you run: argusctl enroll" and st["voice_enrolled"] is False
    argus.extra_status = lambda: 1 / 0               # status always answers
    assert argus.status()["ok"]
    assert argus.handlers()["show"]({})["ok"] and argus.show_requested
    argus.barge_in = "name"
    assert argus.status()["talk_over"].startswith("by name only (")
    argus.barge_in = "words"
    assert "headphones" in argus.status()["talk_over"]
    (tmp_path / "e").mkdir()
    enrolled, _, _ = make_argus(tmp_path / "e", [], voiceprint=FakeVoicePrint())
    assert enrolled.status()["talk_over"] == "your voice" and enrolled.status()["voice_enrolled"]


def test_a_microphone_that_gives_only_silence_is_noticed(tmp_path):
    argus, voice, _ = make_argus(tmp_path, [])
    argus.source.is_open = True
    zeros = np.zeros(512, dtype=np.float32)
    argus._heard_sound(zeros)
    assert not argus.mic_silent()
    argus._mic_open_since -= 25                       # 25 s of nothing but zeros
    argus._heard_sound(zeros)
    assert argus.mic_silent() and argus.status()["mic_silent"]
    log = (tmp_path / "audit.jsonl").read_text()
    assert '"mic_silent"' in log
    argus._heard_sound(zeros)
    assert (tmp_path / "audit.jsonl").read_text().count('"mic_silent"') == 1     # said once
    argus._heard_sound(SPEECH[:512])
    assert not argus.mic_silent() and argus.status()["sound_s_ago"] == 0


# ---- `argusctl voice` --------------------------------------------------------------------------------------------
def voice_args(name=None, **kw):
    return SimpleNamespace(name=name, speed=kw.get("speed"), try_it=kw.get("try_it", False),
                           natural=kw.get("natural", False), no_restart=kw.get("no_restart", False))


def test_argusctl_voice_changes_it_in_one_step(home, capsys):
    from argus.__main__ import VOICE_HELLO, cmd_voice
    path = home.home / "config.toml"
    path.write_text('# my settings\n[voice]\nengine = "kokoro"\nkokoro_voice = "am_michael"  # the default\nspeed = 1.2\n')
    (home.run_dir / "natural-voice-too-slow").write_text("0.9\n")
    assert cmd_voice(voice_args()) == 0
    out = capsys.readouterr().out
    assert "af_sky       American woman" in out and "am_michael   American man   <- now" in out
    assert "the natural one was too slow" in out and "argusctl voice af_sky" in out
    events = []
    code = cmd_voice(voice_args("af_sky", speed=1.1), restart=lambda: events.append("restart") or 0,
                     hello=lambda: events.append("hello"))
    assert code == 0 and events == ["restart", "hello"]
    s = config.load()
    assert (s.kokoro_voice, s.speed, s.voice_engine) == ("af_sky", 1.1, "kokoro")
    assert path.read_text().startswith("# my settings\n") and not (home.run_dir / "natural-voice-too-slow").exists()
    assert "Voice: af_sky at 1.1x." in capsys.readouterr().out and "new voice" in VOICE_HELLO
    assert cmd_voice(voice_args("quick", no_restart=True)) == 0 and config.load().voice_engine == "piper"
    assert "next start: argusctl restart" in capsys.readouterr().out
    assert cmd_voice(voice_args("mac", no_restart=True)) == 0 and config.load().voice_engine == "say"


def test_argusctl_voice_refuses_what_wouldnt_work(home, capsys):
    from argus.__main__ import cmd_voice
    assert cmd_voice(voice_args("hal9000")) == 1 and "no voice called hal9000" in capsys.readouterr().out
    assert cmd_voice(voice_args(speed=3.0)) == 1 and "0.5 to 2.0" in capsys.readouterr().out
    assert not (home.home / "config.toml").exists() or "hal9000" not in (home.home / "config.toml").read_text()


def test_argusctl_voice_try_first(home, capsys):
    from argus.__main__ import cmd_voice
    heard = []

    class Fake:
        def __init__(self, name, speed):
            self.name = name

        def speak(self, text):
            heard.append(self.name)
    assert cmd_voice(voice_args("af_bella", try_it=True), make=Fake, ask=lambda q: "n") == 0
    assert heard == ["af_bella"] and "Nothing changed" in capsys.readouterr().out and config.load().kokoro_voice == "am_michael"
    assert cmd_voice(voice_args("af_bella", try_it=True, no_restart=True), make=Fake, ask=lambda q: "") == 0
    assert config.load().kokoro_voice == "af_bella"

    def broken(name, speed):
        raise FileNotFoundError("no Kokoro voice model")
    assert cmd_voice(voice_args("af_sky", try_it=True), make=broken) == 1 and "./setup.sh" in capsys.readouterr().out


def test_argusctl_voice_natural_again(home, capsys):
    from argus.__main__ import cmd_voice
    (home.run_dir / "natural-voice-too-slow").write_text("0.9\n")
    assert cmd_voice(voice_args(natural=True, no_restart=True)) == 0
    assert not (home.run_dir / "natural-voice-too-slow").exists()


def test_the_voice_in_use_is_described(tmp_path):
    from argus.voice import AdaptiveVoice, RecordingVoice, SayVoice, describe_voice, voice_label
    assert voice_label("af_sky") == "American woman" and voice_label("bm_george") == "British man"
    assert describe_voice(SayVoice()) == "the Mac's own voice" and describe_voice(RecordingVoice()) == "RecordingVoice"

    class Natural:
        pass
    quick = RecordingVoice()
    adaptive = AdaptiveVoice(Natural(), quick)
    assert describe_voice(adaptive) == "Natural"
