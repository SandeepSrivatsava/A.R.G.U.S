"""Small pieces every way of running Argus shares (the Mac app, the always-on server, Android): the log
file, the lock that keeps a second copy from starting, and the start-up record that says how far a start got.
Nothing here needs the Mac's audio libraries.

A restart has to work every time, so:
- a new copy waits for the old one to finish closing instead of giving up at once (single_instance);
- a copy that holds the lock but has stopped answering for a while is stuck, and is stopped so the new one
  can start (only an Argus process, checked by its command line);
- stopping never hangs: the stop signal is noticed on its own thread and Argus leaves within a few seconds
  even if part of the shutdown hangs (stop_on_signals);
- every start writes how far it got to run/startup.json, so `argusctl restart`, `status` and `report` can say
  what Argus is doing or where it stopped, and two failed starts in a row bring the next one up in safe mode
  (the plain Mac voice) instead of failing the same way forever;
- a crash inside native code (a voice or speech model) leaves a trace in logs/crash.log, and `kill -USR1`
  writes where every thread is to logs/threads.log without stopping Argus (capture_crashes).
"""
from __future__ import annotations

import faulthandler
import fcntl
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config as config_mod

log = logging.getLogger("argus.runtime")

LOCK_WAIT = 25.0       # seconds a new copy waits for the old one to close (a restart)
STUCK_AFTER = 90.0     # a copy holding the lock this long without answering is stuck
GOOD_UPTIME = 60.0     # listening this long makes a start a good one
SAFE_AFTER = 2         # failed starts in a row before the next start is a safe one
STOP_DEADLINE = 4.0    # seconds to close cleanly before leaving anyway

_PYTHON = re.compile(r"python[\d.]*")
_open_files: List[Any] = []          # crash and thread-dump files stay open for faulthandler


def setup_logging(settings: config_mod.Settings, verbose: bool = False) -> None:
    path = settings.audit_path.parent / "argus.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    if verbose or sys.stderr.isatty():
        root.addHandler(logging.StreamHandler())
    root.setLevel(logging.INFO)
    os.chmod(path, 0o600)


def version() -> str:
    try:
        from importlib.metadata import version as installed
        return installed("argus")
    except Exception:
        try:
            text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
            found = re.search(r'^version = "([^"]+)"', text, re.M)
            return found.group(1) if found else ""
        except Exception:
            return ""


# ---- one copy at a time ------------------------------------------------------------------------------
def _try_lock(path: Path):
    fh = open(path, "a+")            # not "w": the holder's pid is in the file, keep it until we hold the lock
    os.chmod(path, 0o600)
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()} {int(time.time())}\n")
    fh.flush()
    return fh


def lock_holder(settings: config_mod.Settings) -> Tuple[Optional[int], Optional[float]]:
    """(pid, when it started) of the copy holding the lock, as it wrote them; (None, None) if unknown."""
    try:
        parts = (settings.run_dir / "argus.lock").read_text().split()
        return int(parts[0]), (float(parts[1]) if len(parts) > 1 else None)
    except (OSError, ValueError, IndexError):
        return None, None


def pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    if _reap(pid):                       # a child of ours that has exited (a zombie) is gone
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def command_of(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(out.stdout.split())


def argus_command(command: str) -> bool:
    """Is this command line Argus itself: Python running `-m argus run` (or `server`), or Argus.app's launcher?
    Read strictly, word by word, so that a shell or an editor that merely mentions those words never counts."""
    parts = command.split()
    if not parts:
        return False
    if parts[0].endswith("/Argus.app/Contents/MacOS/Argus"):
        return True
    if not _PYTHON.fullmatch(os.path.basename(parts[0])):
        return False
    args = parts[1:]
    i = 0
    while i < len(args):
        if args[i] == "-m":
            return args[i + 1:i + 2] == ["argus"] and args[i + 2:i + 3] in (["run"], ["server"])
        if not args[i].startswith("-") or args[i] in ("-c", "--"):
            return False                  # a script or code before -m: something else
        i += 2 if args[i] in ("-X", "-W") else 1
    return False


def is_argus(pid: Optional[int]) -> bool:
    """Only Argus's own processes are ever stopped: checked by their command line."""
    return bool(pid) and argus_command(command_of(int(pid)))


def argus_processes() -> List[Tuple[int, str]]:
    """Running Argus copies (the Python process and the Argus.app launcher), not counting this one."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "command="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and int(pid) != os.getpid() and argus_command(command):
            found.append((int(pid), " ".join(command.split())))
    return found


def end_process(pid: int, grace: float = 3.0, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Ask a process to stop, then make it. True once it's gone."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        waited = 0.0
        while waited < wait:
            if not pid_alive(pid):
                return True
            sleep(0.1)
            waited += 0.1
    return not pid_alive(pid)


def _reap(pid: int) -> bool:
    """A child of ours that has exited is gone (tests start the stuck copy themselves)."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == pid
    except ChildProcessError:
        return False


def answering(settings: config_mod.Settings, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    """The running copy's status, or None if no copy answers."""
    from .control import send
    try:
        reply = send(settings.socket_path, "status", timeout=timeout)
    except (OSError, ValueError):
        return None
    return reply if isinstance(reply, dict) else None


def single_instance(settings: config_mod.Settings, wait: float = 0.0, *, sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic, stuck_after: float = STUCK_AFTER,
                    on_stuck: Callable[[int], None] = lambda pid: None):
    """An exclusive lock so a second copy (e.g. opened from Finder) exits instead of double-answering.

    With `wait`, a copy that is still closing (a restart) gets that long to finish. A copy that answers is
    left alone (None: this one should exit). One that holds the lock without answering for longer than
    `stuck_after` seconds since it started is stuck: it is stopped and this copy takes over."""
    path = settings.run_dir / "argus.lock"
    fh = _try_lock(path)
    if fh is not None or wait <= 0:
        return fh
    deadline = clock() + wait
    while clock() < deadline:
        if answering(settings) is not None and read_startup(settings).get("phase") != "stopped":
            return None                    # a working copy (one that's closing has said "stopped" already)
        sleep(0.5)
        fh = _try_lock(path)
        if fh is not None:
            return fh
    pid, started = lock_holder(settings)
    if pid:
        stuck = [pid] if (pid != os.getpid() and pid_alive(pid) and is_argus(pid)
                          and (started is None or time.time() - started > stuck_after)) else []
    else:                  # an older Argus that didn't write its pid in the lock: its Python process
        stuck = [p for p, command in argus_processes() if "-m argus run" in command and p != os.getppid()]
    if stuck and answering(settings) is None:
        for holder in stuck:
            log.warning("Argus (pid %s) holds the lock but doesn't answer; stopping it so this copy can start", holder)
            on_stuck(holder)
            end_process(holder, sleep=sleep)
        for _ in range(20):
            fh = _try_lock(path)
            if fh is not None:
                return fh
            sleep(0.25)
    return None


# ---- how far this start got ----------------------------------------------------------------------------
def read_startup(settings: config_mod.Settings) -> Dict[str, Any]:
    try:
        data = json.loads((settings.run_dir / "startup.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class Startup:
    """run/startup.json: this start's pid, version, phase ("starting", "loading: voice", "listening",
    "stopped", "failed") and whether the previous starts failed."""

    def __init__(self, settings: config_mod.Settings, data: Dict[str, Any], clock: Callable[[], float] = time.time):
        self.path = settings.run_dir / "startup.json"
        self.data = data
        self.clock = clock
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

    @property
    def safe(self) -> bool:
        return bool(self.data.get("safe"))

    @property
    def failed_starts(self) -> int:
        return int(self.data.get("failed_starts") or 0)

    @classmethod
    def begin(cls, settings: config_mod.Settings, version_text: str = "", *, dumps: bool = False,
              clock: Callable[[], float] = time.time) -> "Startup":
        """`dumps`: this process writes a thread snapshot on SIGUSR1 (capture_crashes); only then may the
        report send it one (otherwise the signal would end it)."""
        previous = read_startup(settings)
        failed = 0
        if previous and previous.get("pid") != os.getpid():
            ended_badly = not previous.get("stopped") and not pid_alive(int(previous.get("pid") or 0))
            if ended_badly and not previous.get("good"):
                failed = int(previous.get("failed_starts") or 0) + 1
        now = clock()
        start = cls(settings, {"pid": os.getpid(), "version": version_text, "started": now, "phase": "starting",
                               "phase_since": now, "good": False, "stopped": False, "failed_starts": failed,
                               "safe": failed >= SAFE_AFTER, "dumps": dumps}, clock)
        if failed:
            start.data["previous"] = {k: previous.get(k) for k in ("phase", "error", "version")}
        start._write()
        return start

    def _write(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("couldn't write %s: %s", self.path, exc)

    def phase(self, name: str, **extra: Any) -> None:
        with self._lock:
            self.data.update(phase=name, phase_since=self.clock(), **extra)
            self._write()

    def listening(self, good_after: float = GOOD_UPTIME) -> None:
        self.phase("listening")
        self._timer = threading.Timer(good_after, self._good)
        self._timer.daemon = True
        self._timer.start()

    def _good(self) -> None:
        with self._lock:
            if self.data.get("stopped"):
                return
            self.data.update(good=True, failed_starts=0, safe=False)
            self._write()

    def failed(self, error: str) -> None:
        self.phase("failed", error=" ".join(str(error).split())[:300])

    def stopped(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self.phase("stopped", stopped=True)


# ---- leaving: always, and quickly ------------------------------------------------------------------------
def stop_on_signals(stop: Callable[[int], None], deadline: float = STOP_DEADLINE,
                    leave: Callable[[int], None] = os._exit) -> None:
    """SIGTERM (launchd, `argusctl quit` / `restart`) or SIGHUP: run stop(signal) and leave within `deadline`
    seconds, even if part of the shutdown hangs. The signal is picked up on its own thread (through Python's
    wakeup file descriptor), so a busy main thread such as the menu bar can't hold it up. Main thread only."""
    lock = threading.Lock()
    begun: List[int] = []

    def finish(signum: int) -> None:
        with lock:
            if begun:
                return
            begun.append(signum)
        worker = threading.Thread(target=stop, args=(signum,), name="argus-stopping", daemon=True)
        worker.start()
        worker.join(deadline)
        if worker.is_alive():
            log.warning("stopping took longer than %.0f s; leaving anyway", deadline)
        leave(0)

    read_end, write_end = os.pipe()
    os.set_blocking(write_end, False)
    signal.set_wakeup_fd(write_end, warn_on_full_buffer=False)
    wanted = {int(signal.SIGTERM), int(signal.SIGHUP)}
    for sig in wanted:
        signal.signal(sig, lambda signum, frame: threading.Thread(target=finish, args=(signum,), daemon=True).start())

    def watch() -> None:
        while True:
            try:
                data = os.read(read_end, 64)
            except OSError:
                return
            if not data:
                return
            for value in data:
                if value in wanted:
                    finish(value)
                    return

    threading.Thread(target=watch, name="argus-signals", daemon=True).start()


# ---- crashes and hangs leave a trace ---------------------------------------------------------------------
def capture_crashes(settings: config_mod.Settings) -> bool:
    """True if `kill -USR1` now writes a thread snapshot (dump_threads) instead of ending the process."""
    logs = settings.audit_path.parent
    logs.mkdir(parents=True, exist_ok=True)
    try:
        crash_path, threads_path = logs / "crash.log", logs / "threads.log"
        for path in (crash_path, threads_path):
            if path.exists() and path.stat().st_size > 1_000_000:
                path.write_text("")                       # keep them small
        crash = open(crash_path, "a")
        os.chmod(crash_path, 0o600)
        crash.write(f"--- Argus started, pid {os.getpid()}, {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        crash.flush()
        faulthandler.enable(file=crash, all_threads=True)
        _open_files.append(crash)
        if hasattr(signal, "SIGUSR1"):
            threads = open(threads_path, "a")
            os.chmod(threads_path, 0o600)
            faulthandler.register(signal.SIGUSR1, file=threads, all_threads=True, chain=False)
            _open_files.append(threads)
            return True
    except (OSError, ValueError, RuntimeError) as exc:   # never a reason not to start
        log.warning("crash capture unavailable: %s", exc)
    return False


def dump_threads(settings: config_mod.Settings, pid: int, wait: float = 3.0) -> str:
    """Where every thread of a running (possibly stuck) Argus is right now, without stopping it."""
    path = settings.audit_path.parent / "threads.log"
    try:
        before = path.stat().st_size if path.exists() else 0
        os.kill(pid, signal.SIGUSR1)
    except (OSError, AttributeError):
        return ""
    deadline = time.monotonic() + wait
    size = before
    while time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            now = path.stat().st_size
        except OSError:
            continue
        if now > before and now == size:              # written and settled
            break
        size = now
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(before)
            return fh.read()
    except OSError:
        return ""


def kind() -> str:
    """Which Argus this is: "android" (in Termux on a phone), "server" (the always-on machine; its systemd
    service and admin command set ARGUS_MODE=server) or "mac"."""
    if os.environ.get("TERMUX_VERSION") or "com.termux" in os.environ.get("PREFIX", ""):
        return "android"
    if os.environ.get("ARGUS_MODE") == "server":
        return "server"
    return "mac"


START_HINT = {"mac": "argusctl restart   (or: open -a Argus)", "server": "sudo systemctl start argus",
              "android": "argus-android serve &"}
