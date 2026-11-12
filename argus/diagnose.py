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
    elif event == "tool_slow":
        text = f"(note) The {entry.get('tool')} tool took {entry.get('seconds')} s"
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


def build_report(settings, secrets: Dict[str, str], version: str = "") -> str:
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
             "Settings",
             f"  models: everyday {settings.light_model or settings.model}, main {settings.model}, "
             f"heavy {settings.heavy_model or settings.model} (routing {settings.routing})",
             f"  effort: spoken {settings.voice_effort}, other {settings.effort}, heavy {settings.heavy_effort}",
             f"  phone: {settings.phone_type}, gateway {'on' if settings.phone_enabled else 'off'}; "
             f"mail {'on' if settings.mail_enabled else 'off'}; "
             f"Google {'connected' if secrets.get('GOOGLE_REFRESH_TOKEN') else 'not connected'}; web search {'on' if settings.web_search else 'off'}",
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
              f"{e.get('speech_to_text_s')}, Claude {e.get('claude_s')}, tools {e.get('tools')}"
              for e in timings] or ["  none"]
    lines += ["", "From the app's log (warnings and errors)"]
    lines += ["  " + line for line in log_trouble(logs / "argus.log")] or ["  none"]
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
