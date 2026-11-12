"""Argus's login service on the Mac (launchd): `argusctl restart`, `quit` and `status` — and saying plainly
whether Argus really came back, or why not, instead of "restarted" when it didn't.

launchd starts Argus.app's small launcher, which starts the Python process. Where things stand comes from
four places: launchctl (is the service loaded and running, how did the last run end), run/startup.json (how far
the start got), the control socket (a running Argus answers) and logs/voice.err.log (what a failed start
printed). Nothing here sends anything off the Mac.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import runtime

LABEL = "com.argus.voice"

Run = Callable[[List[str]], Tuple[int, str]]
Out = Callable[[str], None]


def target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def run_command(args: List[str], timeout: float = 20.0) -> Tuple[int, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def job_state(run: Run = run_command) -> Dict[str, Any]:
    """launchd's view of the service: loaded? running? how did the last run end?"""
    code, out = run(["launchctl", "print", target()])
    if code != 0:
        return {"loaded": False}

    def field(name: str) -> str:
        found = re.search(rf"^\s*{name} = (.+)$", out, re.M)
        return found.group(1).strip() if found else ""
    pid = field("pid")
    return {"loaded": True, "state": field("state"), "pid": int(pid) if pid.isdigit() else None,
            "last_exit": field("last exit code"), "last_signal": field("last terminating signal")}


def ended_badly(job: Dict[str, Any]) -> bool:
    last = str(job.get("last_exit") or "")
    return bool(job.get("loaded")) and job.get("state") != "running" and (
        bool(job.get("last_signal")) or (last not in ("", "0", "(never exited)")))


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def new_lines(path: Path, since: int, last: int = 15) -> List[str]:
    """What was written to a log after `since` bytes (what this start printed), at most `last` lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(since if _size(path) >= since else 0)
            text = fh.read()
    except OSError:
        return []
    return [line.rstrip()[:300] for line in text.splitlines() if line.strip()][-last:]


def why_not(settings, job: Optional[Dict[str, Any]] = None, *, err_since: int = 0, run: Run = run_command) -> List[str]:
    """Plain lines on why Argus isn't answering."""
    from .diagnose import redact
    job = job if job is not None else job_state(run)
    lines: List[str] = []
    start = runtime.read_startup(settings)
    pid = start.get("pid")
    if runtime.kind() == "mac":
        if not job.get("loaded"):
            lines.append("The login service isn't loaded (it was stopped with launchctl, or setup didn't finish).")
        elif job.get("state") == "running":
            lines.append("The service is running.")
        elif ended_badly(job):
            how = job.get("last_signal") or f"exit code {job.get('last_exit')}"
            lines.append(f"The last start ended with {how}; macOS tries again every 15 seconds.")
    if start:
        when = time.strftime("%H:%M:%S", time.localtime(float(start.get("phase_since") or 0)))
        alive = runtime.pid_alive(pid)
        phase = start.get("phase")
        if phase == "failed":
            lines.append(f"The last start failed at {when}: {start.get('error')}")
        elif phase == "stopped":
            lines.append(f"Argus was stopped at {when}.")
        elif alive:
            lines.append(f"Argus (pid {pid}) is '{phase}' since {when} but doesn't answer.")
        else:
            lines.append(f"Argus (pid {pid}) went away while '{phase}' (at {when}) without saying why: a crash. "
                         "argusctl report has the details.")
    holder, _ = runtime.lock_holder(settings)
    if holder and holder != pid and runtime.pid_alive(holder) and runtime.is_argus(holder):
        lines.append(f"Another copy of Argus (pid {holder}) holds the lock. argusctl restart --force stops it.")
    elif not holder:
        others = [p for p, command in runtime.argus_processes() if "-m argus run" in command and p != pid]
        if others:
            lines.append(f"Argus is running as pid {', '.join(map(str, others))} but doesn't answer. "
                         "argusctl restart --force stops it.")
    printed = new_lines(settings.audit_path.parent / "voice.err.log", err_since)
    if printed:
        lines.append("What it printed:")
        lines += ["    " + redact(line) for line in printed]
    return lines


def wait_until_back(settings, old_pid: Optional[int], began: float, *, err_since: int = 0, run: Run = run_command,
                    out: Out = print, wait: float = 90.0, sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic) -> int:
    """Wait until a new Argus answers (0), or say why it didn't (1)."""
    deadline = clock() + wait
    told = None
    bad_since: Optional[float] = None
    while clock() < deadline:
        status = runtime.answering(settings, timeout=2.0)
        if status and status.get("pid") and status.get("pid") != old_pid:
            out(f"Argus is back and listening ({status.get('mode')}; version {status.get('version') or '?'}; "
                f"voice: {status.get('voice') or '?'}).")
            if status.get("safe_start"):
                out("  It started safely, with the Mac's own voice, because the last starts failed. "
                    "Send me: argusctl report")
            return 0
        start = runtime.read_startup(settings)
        if start.get("pid") and start.get("pid") != old_pid and float(start.get("started") or 0) >= began - 2:
            phase = start.get("phase")
            if phase != told:
                out(f"  {phase}…")
                told = phase
            if phase == "failed":
                out("Argus couldn't start.")
                for line in why_not(settings, err_since=err_since, run=run):
                    out("  " + line)
                return 1
        if runtime.kind() == "mac":
            job = job_state(run)
            if ended_badly(job):
                bad_since = bad_since if bad_since is not None else clock()
                if clock() - bad_since >= 3.0:
                    out("Argus stopped while starting.")
                    for line in why_not(settings, job, err_since=err_since, run=run):
                        out("  " + line)
                    return 1
            else:
                bad_since = None
        sleep(0.5)
    out(f"Argus didn't answer within {int(wait)} seconds.")
    for line in why_not(settings, err_since=err_since, run=run):
        out("  " + line)
    out("Send me the report: argusctl report")
    return 1


def _bootstrap(run: Run, sleep: Callable[[float], None]) -> Tuple[int, str]:
    """Load the service. Right after it was unloaded macOS may still be closing the old one ("Bootstrap
    failed: 5"), so try a few times."""
    code, text = 1, ""
    for attempt in range(1, 6):
        code, text = run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path())])
        if code == 0:
            return code, text
        if job_state(run).get("loaded"):
            return run(["launchctl", "kickstart", "-k", target()])
        sleep(attempt)
    return code, text


def restart(settings, *, force: bool = False, run: Run = run_command, out: Out = print, wait: float = 90.0,
            sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    if runtime.kind() != "mac":
        out(f"Restart it with: {runtime.START_HINT[runtime.kind()]}")
        return 1
    before = runtime.answering(settings) or {}
    old_pid = before.get("pid") or runtime.lock_holder(settings)[0]
    err_since = _size(settings.audit_path.parent / "voice.err.log")
    began = time.time()
    job = job_state(run)
    if force:
        out("Stopping every copy of Argus…")
        if job.get("loaded"):
            run(["launchctl", "bootout", target()])
        for pid, _command in runtime.argus_processes():
            runtime.end_process(pid, sleep=sleep)
        job = {"loaded": False}
    if job.get("loaded"):
        out("Restarting Argus…")
        code, text = run(["launchctl", "kickstart", "-k", target()])
    else:
        if not plist_path().exists():
            out("Argus isn't set up to start at login. Run ./setup.sh in the A.R.G.U.S folder.")
            return 1
        out("Starting Argus…")
        code, text = _bootstrap(run, sleep)
    if code != 0:
        out(f"macOS didn't start it: {' '.join(text.split())[:300]}")
        out("Try: argusctl restart --force")
        return 1
    return wait_until_back(settings, old_pid, began, err_since=err_since, run=run, out=out, wait=wait,
                           sleep=sleep, clock=clock)


def stop(settings, *, run: Run = run_command, out: Out = print, wait: float = 10.0,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    """`argusctl quit`: stop Argus until `argusctl restart` (or the next login)."""
    holder = (runtime.answering(settings) or {}).get("pid") or runtime.lock_holder(settings)[0]
    if runtime.kind() == "mac" and job_state(run).get("loaded"):
        run(["launchctl", "kill", "TERM", target()])
    elif holder and runtime.pid_alive(holder) and runtime.is_argus(holder):
        os.kill(holder, 15)
    deadline = clock() + wait
    while clock() < deadline and holder and runtime.pid_alive(holder):
        sleep(0.25)
    if holder and runtime.pid_alive(holder) and runtime.is_argus(holder):
        runtime.end_process(holder, grace=0.5, sleep=sleep)
    out("Argus stopped. Start it again: argusctl restart")
    return 0


def describe_status(status: Dict[str, Any]) -> List[str]:
    """`argusctl status` in plain words."""
    up = int(status.get("up_s") or 0)
    lines = [f"Argus is {status.get('mode')} (version {status.get('version') or '?'}, pid {status.get('pid') or '?'}, "
             f"up {up // 3600} h {up % 3600 // 60} min)."]
    lines.append(f"  Voice: {status.get('voice') or '?'}")
    lines.append(f"  Talking over Argus: {status.get('talk_over') or '?'}")
    if status.get("mic_silent"):
        lines.append("  Microphone: open, but only silence comes in. Allow it: System Settings → Privacy & Security → "
                     "Microphone → Argus, then argusctl restart")
    elif status.get("mic_open"):
        ago = status.get("sound_s_ago")
        lines.append("  Microphone: on" + (f" (sound {ago} s ago)" if ago is not None else ""))
    else:
        lines.append("  Microphone: closed (" + str(status.get("mode")) + ")")
    if status.get("on_call"):
        lines.append(f"  Paused while {', '.join(status['on_call'])} uses a microphone (a call). If that's wrong, "
                     "add the app to ignore_mic_apps under [listening] in ~/.argus/config.toml")
    if status.get("asleep"):
        lines.append("  Asleep: say “Argus, wake up” (or argusctl wake)")
    if status.get("safe_start"):
        lines.append("  Started safely (the Mac's own voice) because the last starts failed: argusctl report")
    if status.get("last_error"):
        lines.append(f"  Last problem: {' '.join(str(status['last_error']).split())[:200]}")
    return lines
