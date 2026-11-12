"""Argus's own storage: one SQLite file (~/.argus/argus.db, owner-only) for conversations, the
things you asked it to remember, scheduled tasks and small settings."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, channel TEXT NOT NULL, key TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_lookup ON sessions(channel, key, updated);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT, fact TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, mode TEXT NOT NULL,
  due REAL, cron TEXT, next_run REAL, created REAL NOT NULL, done INTEGER NOT NULL DEFAULT 0,
  last_run REAL, origin TEXT NOT NULL DEFAULT 'voice');
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sightings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, thing TEXT NOT NULL, place TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lesson TEXT NOT NULL, source TEXT NOT NULL,
  created REAL NOT NULL, used REAL, uses INTEGER NOT NULL DEFAULT 0);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        if new:
            os.chmod(self.path, 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self._db.close()

    def _q(self, sql: str, args: Tuple = ()) -> List[Tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ---- conversations -----------------------------------------------------------------------------
    def session_for(self, channel: str, key: str, idle_minutes: float) -> str:
        """The current conversation for (channel, key), or a new one after `idle_minutes` of quiet."""
        now = time.time()
        with self._lock:
            row = self._q("SELECT id, updated FROM sessions WHERE channel=? AND key=? ORDER BY updated DESC LIMIT 1",
                          (channel, key))
            if row and now - row[0][1] <= idle_minutes * 60:
                self._q("UPDATE sessions SET updated=? WHERE id=?", (now, row[0][0]))
                return row[0][0]
            return self.new_session(channel, key)

    def new_session(self, channel: str, key: str) -> str:
        sid = f"{channel}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        now = time.time()
        self._q("INSERT INTO sessions(id, channel, key, created, updated) VALUES (?,?,?,?,?)", (sid, channel, key, now, now))
        return sid

    def history(self, session_id: str, limit: int = 80) -> List[Dict[str, Any]]:
        rows = self._q("SELECT role, content FROM (SELECT id, role, content FROM messages WHERE session_id=? "
                       "ORDER BY id DESC LIMIT ?) ORDER BY id", (session_id, limit))
        return [{"role": r, "content": json.loads(c)} for r, c in rows]

    def append(self, session_id: str, role: str, content: Any) -> None:
        now = time.time()
        with self._lock:
            self._q("INSERT INTO messages(session_id, role, content, created) VALUES (?,?,?,?)",
                    (session_id, role, json.dumps(content), now))
            self._q("UPDATE sessions SET updated=? WHERE id=?", (now, session_id))

    def replace_history(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
                now = time.time()
                for m in messages:
                    self._db.execute("INSERT INTO messages(session_id, role, content, created) VALUES (?,?,?,?)",
                                     (session_id, m["role"], json.dumps(m["content"]), now))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def prune_sessions(self, older_than_days: float = 30) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            old = [r[0] for r in self._q("SELECT id FROM sessions WHERE updated < ?", (cutoff,))]
            for sid in old:
                self._q("DELETE FROM messages WHERE session_id=?", (sid,))
                self._q("DELETE FROM sessions WHERE id=?", (sid,))
        return len(old)

    # ---- things to remember ------------------------------------------------------------------------
    def remember(self, fact: str) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO memory(fact, created) VALUES (?,?)", (fact.strip(), time.time()))
            return int(cur.lastrowid)

    def forget(self, fact_id: int) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM memory WHERE id=?", (int(fact_id),)).rowcount > 0

    def facts(self, query: str = "", limit: int = 50) -> List[Tuple[int, str]]:
        if query:
            like = f"%{query.strip().lower()}%"
            rows = self._q("SELECT id, fact FROM memory WHERE lower(fact) LIKE ? ORDER BY id DESC LIMIT ?", (like, limit))
        else:
            rows = self._q("SELECT id, fact FROM memory ORDER BY id DESC LIMIT ?", (limit,))
        return [(int(i), f) for i, f in rows]

    # ---- lessons (rules learned from corrections and mistakes; see lessons.py) -------------------------
    def add_lesson(self, lesson: str, source: str = "you") -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO lessons(lesson, source, created) VALUES (?,?,?)",
                                   (lesson.strip(), source, time.time()))
            return int(cur.lastrowid)

    def update_lesson(self, lesson_id: int, lesson: str, source: str) -> None:
        with self._lock:
            self._db.execute("UPDATE lessons SET lesson=?, source=?, created=? WHERE id=?",
                             (lesson.strip(), source, time.time(), int(lesson_id)))

    def lessons(self, limit: int = 200) -> List[Tuple[int, str, str]]:
        rows = self._q("SELECT id, lesson, source FROM lessons ORDER BY id DESC LIMIT ?", (limit,))
        return [(int(i), text, source) for i, text, source in rows]

    def forget_lesson(self, lesson_id: int) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM lessons WHERE id=?", (int(lesson_id),)).rowcount > 0

    def clear_lessons(self) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM lessons").rowcount

    def used_lessons(self, ids: List[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany("UPDATE lessons SET used=?, uses=uses+1 WHERE id=?",
                                 [(time.time(), int(i)) for i in ids])

    def prune_lessons(self, keep: int) -> int:
        """Past `keep` lessons, drop the ones Argus wrote itself that were least recently useful."""
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
            extra = total - keep
            if extra <= 0:
                return 0
            return self._db.execute(
                "DELETE FROM lessons WHERE id IN (SELECT id FROM lessons WHERE source='self' "
                "ORDER BY COALESCE(used, created) LIMIT ?)", (extra,)).rowcount

    # ---- scheduled tasks ---------------------------------------------------------------------------
    def add_task(self, text: str, mode: str, *, due: Optional[float] = None, cron: Optional[str] = None,
                 next_run: Optional[float] = None, origin: str = "voice") -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO tasks(text, mode, due, cron, next_run, created, origin) VALUES (?,?,?,?,?,?,?)",
                (text, mode, due, cron, next_run if next_run is not None else due, time.time(), origin))
            return int(cur.lastrowid)

    def due_tasks(self, now: float) -> List[Dict[str, Any]]:
        rows = self._q("SELECT id, text, mode, due, cron, next_run, origin FROM tasks "
                       "WHERE done=0 AND next_run IS NOT NULL AND next_run <= ? ORDER BY next_run", (now,))
        return [dict(zip(("id", "text", "mode", "due", "cron", "next_run", "origin"), r)) for r in rows]

    def finish_task(self, task_id: int, next_run: Optional[float]) -> None:
        if next_run is None:
            self._q("UPDATE tasks SET done=1, last_run=? WHERE id=?", (time.time(), task_id))
        else:
            self._q("UPDATE tasks SET next_run=?, last_run=? WHERE id=?", (next_run, time.time(), task_id))

    def tasks(self) -> List[Dict[str, Any]]:
        rows = self._q("SELECT id, text, mode, due, cron, next_run FROM tasks WHERE done=0 ORDER BY next_run")
        return [dict(zip(("id", "text", "mode", "due", "cron", "next_run"), r)) for r in rows]

    def cancel_task(self, task_id: int) -> bool:
        with self._lock:
            return self._db.execute("UPDATE tasks SET done=1 WHERE id=? AND done=0", (int(task_id),)).rowcount > 0

    def task_named(self, text: str) -> Optional[int]:
        row = self._q("SELECT id FROM tasks WHERE done=0 AND text=? LIMIT 1", (text,))
        return int(row[0][0]) if row else None

    # ---- seen through the glasses (sightings.py) ---------------------------------------------------
    def add_sighting(self, thing: str, place: str, at: float) -> None:
        self._q("INSERT INTO sightings(at, thing, place) VALUES (?,?,?)", (at, thing, place))

    def sightings(self, limit: int = 600) -> List[Tuple[float, str, str]]:
        return [(r[0], r[1], r[2]) for r in
                self._q("SELECT at, thing, place FROM sightings ORDER BY at DESC, id DESC LIMIT ?", (limit,))]

    def prune_sightings(self, older_than: float, keep: int) -> None:
        self._q("DELETE FROM sightings WHERE at < ?", (older_than,))
        self._q("DELETE FROM sightings WHERE id NOT IN (SELECT id FROM sightings ORDER BY at DESC, id DESC LIMIT ?)",
                (keep,))

    # ---- small settings ----------------------------------------------------------------------------
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return row[0][0] if row else default

    def set(self, key: str, value: str) -> None:
        self._q("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
