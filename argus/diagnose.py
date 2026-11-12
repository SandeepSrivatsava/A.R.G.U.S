"""Seeing what went wrong: `argusctl errors` (recent problems in plain words, or live with --watch) and
`argusctl report` (one text file with everything needed to find a fault, safe to share).

Both read what Argus already writes on this computer: the activity log (audit.jsonl) and the app's own log
(argus.log). The report leaves out what you said and what Argus answered, and removes keys, tokens, email
addresses and your home folder's name.
"""
from __future__ import annotations

import json
import os
import platform
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

PROBLEM_EVENTS = ("llm_error", "tool_failed", "tool_blocked", "approval_failed", "fresh_session_after_refusal",
                  "handed_over", "tool_slow", "run_stopped")
_KEYS = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|\b[A-Za-z0-9_-]{32,}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LOG_TROUBLE = re.compile(r"\b(ERROR|CRITICAL|WARNING|Traceback|Error:|Exception)\b")


def describe(entry: Dict[str, Any]) -> Optional[str]:
    """One plain line for a log entry that is a problem (or explains one); None for everything else."""
    event = entry.get("event")
    if event == "turn":
        if not entry.get("error"):
            return None
        text = f"A request failed: {entry['error']}"
    elif event == "llm_error":
        text = f"Claude: {entry.get('error')}"
    elif event == "tool_failed":
        text = f"The {entry.get('tool')} tool failed: {entry.get('error')}"
    elif event == "tool_blocked":
        text = f"Blocked by Argus's rules ({entry.get('tool')}): {entry.get('reason')}"
    elif event == "approval_failed":
        text = f"The approval dialog failed ({entry.get('tool')}): {entry.get('error')}"
    elif event == "fresh_session_after_refusal":
        text = f"Claude refused the conversation, so Argus started a fresh one: {entry.get('error')}"
    elif event == "handed_over":
        text = f"(note) The quick model passed a request up to {entry.get('to')}"
    elif event == "barge_in":
        text = f"(note) Argus stopped talking because it heard {entry.get('why')}" + (
            f" (voice match {entry.get('score')})" if entry.get("score") is not None else "")
    elif event == "voice_switched":
        text = (f"The natural voice was too slow on this computer ({entry.get('work_per_second')} s of work per "
                "second of speech), so Argus switched to the quick voice")
    elif event == "tool_slow":
        text = f"(note) The {entry.get('tool')} tool took {entry.get('seconds')} s"
    elif event == "mic_silent":
        text = (f"The microphone gave only silence for {entry.get('seconds')} s: macOS may have taken away Argus's "
                "microphone permission (System Settings > Privacy & Security > Microphone > Argus)")
    elif event == "safe_start":
        text = (f"The last {entry.get('failed_starts')} starts failed, so Argus started safely (the Mac's own voice, "
                "no extras)")
    elif event == "stuck_copy_stopped":
        text = f"A copy of Argus (pid {entry.get('pid')}) was stuck, so it was stopped to let a new one start"
    else:
        return None
    return f"{str(entry.get('ts', ''))[:19].replace('T', ' ')}  {' '.join(str(text).split())}"


def read_entries(path: Path, last: int = 3000) -> List[Dict[str, Any]]:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-last:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def problems(entries: Iterable[Dict[str, Any]], notes: bool = True) -> List[str]:
    found = [line for line in (describe(e) for e in entries) if line]
    return found if notes else [line for line in found if "(note)" not in line]


def log_trouble(path: Path, last: int = 40) -> List[str]:
    """Warnings, errors and crash traces from the app's own log, newest last."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
    except OSError:
        return []
    keep: List[str] = []
    in_trace = False
    for line in lines:
        if "Traceback" in line:
            in_trace = True
        if in_trace or _LOG_TROUBLE.search(line):
            keep.append(line[:400])
        if in_trace and line and not line.startswith((" ", "\t", "Traceback")):
            in_trace = False                      # the last line of a trace names the error
    return keep[-last:]


def redact(text: str, secrets: Sequence[str] = (), home: Optional[str] = None) -> str:
    for value in sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True):
        text = text.replace(value, "[key removed]")
    text = _KEYS.sub("[key removed]", text)
    text = _EMAIL.sub("[email removed]", text)
    home = home if home is not None else str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    return text


def follow(path: Path, poll: float = 1.0, stop=lambda: False) -> Iterator[str]:
    """New problems as they are written to the activity log."""
    path = Path(path)
    try:
        position = path.stat().st_size
    except OSError:
        position = 0
    while not stop():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size < position:                       # the log was rotated
            position = 0
        if size > position:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(position)
                chunk = fh.read()
                position = fh.tell()
            for line in chunk.splitlines():
                try:
                    told = describe(json.loads(line))
                except ValueError:
                    continue
                if told:
                    yield told
        time.sleep(poll)


START_EVENTS = ("started", "stopped", "safe_start", "stuck_copy_stopped", "mic_silent")


def starts_and_stops(entries: Iterable[Dict[str, Any]], last: int = 8) -> List[str]:
    lines = []
    for e in [e for e in entries if e.get("event") in START_EVENTS][-last:]:
        when = str(e.get("ts", ""))[:19].replace("T", " ")
        event = e.get("event")
        if event == "started":
            what = f"started, version {e.get('version') or '?'}, pid {e.get('pid') or '?'}" + (
                " (safe start)" if e.get("safe") else "")
        elif event == "stopped":
            what = "stopped" + (f" (signal {e.get('signal')})" if e.get("signal") else "")
        else:
            what = describe(e) or event
            what = what[21:] if what[:4].isdigit() else what
        lines.append(f"  {when}  {what}")
    return lines or ["  none recorded"]


def summarize_threads(dump: str, frames: int = 6, limit: int = 70) -> List[str]:
    """faulthandler's dump, shortened: each thread with its innermost calls."""
    out: List[str] = []
    kept = 0
    for line in dump.splitlines():
        if not line.strip():
            continue
        if not line.startswith(" "):
            out.append(line.strip())
            kept = 0
        elif kept < frames:
            out.append("  " + line.strip())
            kept += 1
    return out[:limit]


def tail(path: Path, last: int = 20) -> List[str]:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line[:300] for line in lines if line.strip()][-last:]


def last_crash(path: Path, last: int = 40) -> List[str]:
    """The most recent native crash in logs/crash.log (faulthandler), with the start it belongs to."""
    lines = tail(path, 4000)
    where = max((i for i, line in enumerate(lines) if "Fatal Python error" in line), default=None)
    if where is None:
        return []
    started = max((i for i in range(where) if lines[i].startswith("--- Argus started")), default=where)
    return lines[started:started + 1] + lines[where:where + last] if started != where else lines[where:where + last]


def right_now(settings, *, run=None, dump=None) -> List[str]:
    """Is Argus running and listening this moment, and if not, why not (and where it's stuck)."""
    from . import runtime, service
    status = runtime.answering(settings, timeout=3.0)
    if status is not None and status.get("pid"):
        lines = ["  " + line.strip() for line in service.describe_status(status)]
        installed = runtime.version()
        if status.get("version") and installed and status["version"] != installed:
            lines.append(f"  The Argus running is {status['version']}, but {installed} is installed: argusctl restart")
        return lines
    lines = ["  Argus isn't answering" + (" (an older version is running)" if status is not None else "") + "."]
    lines += ["  " + line for line in service.why_not(settings, run=run or service.run_command)]
    start = runtime.read_startup(settings)
    pid = start.get("pid")
    if start.get("dumps") and pid and runtime.pid_alive(int(pid)) and runtime.is_argus(int(pid)):
        dumped = (dump or runtime.dump_threads)(settings, int(pid))
        if dumped.strip():
            lines.append("  Where it is right now (each thread, the innermost call first):")
            lines += ["    " + line for line in summarize_threads(dumped)]
    return lines


def build_report(settings, secrets: Dict[str, str], version: str = "", *, run=None, dump=None) -> str:
    entries = read_entries(settings.audit_path)
    turns = [e for e in entries if e.get("event") == "turn"]
    failed = [e for e in turns if e.get("error")]
    models: Dict[str, int] = {}
    for e in turns:
        models[str(e.get("model") or "?")] = models.get(str(e.get("model") or "?"), 0) + 1
    logs = settings.audit_path.parent
    lines = [f"Argus report, {time.strftime('%Y-%m-%d %H:%M %Z')}",
             f"Argus {version or '?'} on {platform.system()} {platform.release()} ({platform.machine()}), "
             f"Python {platform.python_version()}",
             "",
             "Right now"] + right_now(settings, run=run, dump=dump) + [
             "",
             "Settings",
             f"  models: everyday {settings.light_model or settings.model}, main {settings.model}, "
             f"heavy {settings.heavy_model or settings.model} (routing {settings.routing})",
             f"  effort: spoken {settings.voice_effort}, other {settings.effort}, heavy {settings.heavy_effort}",
             f"  phone: {settings.phone_type}, gateway {'on' if settings.phone_enabled else 'off'}; "
             f"mail {'on' if settings.mail_enabled else 'off'}; "
             f"Google {'connected' if secrets.get('GOOGLE_REFRESH_TOKEN') else 'not connected'}; web search {'on' if settings.web_search else 'off'}",
             f"  voice: {settings.voice_engine} {settings.kokoro_voice} at {settings.speed}x"
             + (" (switched to the quick voice: the natural one was too slow here)"
                if (settings.run_dir / "natural-voice-too-slow").exists() else "")
             + f"; interrupting: {settings.barge_in}"
             + f"; your voice enrolled: {'yes' if settings.voiceprint_path.exists() else 'no (argusctl enroll)'}",
             "  keys saved: " + (", ".join(sorted(k for k, v in secrets.items() if v)) or "none"),
             "",
             f"Requests in the log: {len(turns)}, failed: {len(failed)}; by model: "
             + (", ".join(f"{m} {n}" for m, n in sorted(models.items())) or "none"),
             "",
             "Recent problems (newest last)"]
    lines += ["  " + p for p in problems(entries)[-40:]] or ["  none"]
    lines += ["", "Recent timings (seconds)"]
    timings = [e for e in entries if e.get("event") == "timing"][-8:]
    lines += [f"  {str(e.get('ts', ''))[11:19]} first word {e.get('first_word_s')}, speech to text "
              f"{e.get('speech_to_text_s')}, Claude {e.get('claude_s')}, tools {e.get('tools')}, "
              f"voice (made, sound) {e.get('voice')}"
              for e in timings] or ["  none"]
    lines += ["", "From the app's log (warnings and errors)"]
    lines += ["  " + line for line in log_trouble(logs / "argus.log")] or ["  none"]
    lines += ["", "Starts and stops (newest last)"] + starts_and_stops(entries)
    printed = tail(logs / "voice.err.log", 20)
    if printed:
        lines += ["", "What Argus printed when starting (logs/voice.err.log, last lines)"] + ["  " + p for p in printed]
    crash = last_crash(logs / "crash.log")
    if crash:
        lines += ["", "The last crash (logs/crash.log)"] + ["  " + c for c in crash]
    try:
        launch = (logs / "launchctl.err").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        launch = ""
    if launch:
        lines += ["", "Last start at login", "  " + launch[:400]]
    lines += ["", "What you said and what Argus answered are not in this report."]
    return redact("\n".join(lines) + "\n", [v for v in secrets.values() if v])


def write_report(text: str, folder: Optional[Path] = None) -> Path:
    folder = folder or (Path.home() / "Desktop")
    if not folder.is_dir():
        folder = Path.home()
    path = folder / "argus-report.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
