"""Append-only activity log (~/.argus/logs/audit.jsonl, readable only by you).

It records what Argus was asked and did: requests, tool use, approvals, blocks. It never records
audio, and never records speech that wasn't addressed to Argus.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class Audit:
    def __init__(self, path: Path, max_bytes: int = 5_000_000, backups: int = 3):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()

    def _rotate(self) -> None:
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except FileNotFoundError:
            return
        for i in range(self.backups - 1, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{i}")
            if older.exists():
                os.replace(older, self.path.with_name(f"{self.path.name}.{i + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    def log(self, event: str, **fields: Any) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
        record.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                pass  # the log must never take Argus down

    def tail(self, n: int = 20) -> list:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
