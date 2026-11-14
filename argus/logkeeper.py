"""Every log Argus writes, kept in one folder you can open (default ~/Downloads/Logs), one sub-folder per day,
and deleted by itself after 30 days.

Argus keeps writing its logs in ~/.argus/logs as before; every 10 minutes (and when it stops) whatever is new in
each of them is added to today's folder, e.g. Logs/2026-11-12/audit.jsonl. Day folders older than `keep_days`
are deleted; nothing else in the folder is ever touched. Folders are readable by you alone (700, files 600).

The activity log holds what you asked Argus (never keys or what it didn't think was for it), so this folder
is as private as your Downloads folder: don't share it whole; `argusctl report` makes a file that's safe to send.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

log = logging.getLogger("argus.logkeeper")

FILES = ("audit.jsonl", "calls.log", "argus.log", "voice.out.log", "voice.err.log", "crash.log", "threads.log", "launchctl.err")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
INTERVAL = 600.0


class LogKeeper:
    def __init__(self, source: Path, folder: Path, keep_days: int = 30, *, clock: Callable[[], float] = time.time,
                 interval: float = INTERVAL):
        self.source, self.folder = Path(source), Path(folder).expanduser()
        self.keep_days, self.clock, self.interval = keep_days, clock, interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._told = False

    def _offsets(self) -> Dict[str, int]:
        try:
            data = json.loads((self.folder / ".offsets.json").read_text())
            return {k: int(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def sync(self) -> Optional[Path]:
        """Add what's new in each log to today's folder, then delete old days. The day folder, or None."""
        with self._lock:
            try:
                return self._sync()
            except OSError as exc:              # e.g. macOS hasn't allowed Argus into Downloads yet
                if not self._told:
                    log.warning("couldn't keep the logs in %s: %s", self.folder, exc)
                    self._told = True
                return None

    def _sync(self) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)
        day = self.folder / time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        day.mkdir(exist_ok=True)
        os.chmod(day, 0o700)
        offsets = self._offsets()
        for name in FILES:
            path = self.source / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            start = offsets.get(name, 0)
            if size < start:                     # the log was rotated or emptied: start from its beginning
                start = 0
            if size > start:
                with open(path, "rb") as src:
                    src.seek(start)
                    data = src.read(size - start)
                fd = os.open(day / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
            offsets[name] = size
        tmp = self.folder / ".offsets.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(offsets, fh)
        os.replace(tmp, self.folder / ".offsets.json")
        self.prune()
        return day

    def prune(self) -> int:
        """Delete day folders older than keep_days. Only folders named like 2026-11-12."""
        cutoff = time.strftime("%Y-%m-%d", time.localtime(self.clock() - self.keep_days * 86400))
        gone = 0
        for entry in self.folder.iterdir():
            if entry.is_dir() and not entry.is_symlink() and _DAY.fullmatch(entry.name) and entry.name < cutoff:
                shutil.rmtree(entry)
                gone += 1
        return gone

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                self.sync()
                self._stop.wait(self.interval)
        threading.Thread(target=loop, name="argus-logkeeper", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.sync()


def from_settings(settings) -> Optional[LogKeeper]:
    folder = str(getattr(settings, "logs_folder", "") or "").strip()
    if not folder:
        return None
    return LogKeeper(settings.audit_path.parent, Path(folder), int(getattr(settings, "logs_keep_days", 30)))
