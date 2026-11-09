"""Runs reminders and scheduled tasks when they're due, and keeps the built-in morning jobs in sync
with the settings. Results are spoken at the Mac (if you're there) and sent to your phone."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .cron import Cron, CronError
from .store import Store

log = logging.getLogger("argus.scheduler")

HEADLINES_JOB = "Morning headlines"
BRIEFING_JOB = ("Morning briefing: today's weather where I live, the 5 most important headlines (use get_news), "
                "and anything I asked you to remind me about today. Under 150 words, friendly, no preamble.")


def sync_default_jobs(store: Store, headlines_cron: str, briefing_cron: str, tz: Optional[str]) -> None:
    for text, mode, cron in ((HEADLINES_JOB, "news", headlines_cron), (BRIEFING_JOB, "task", briefing_cron)):
        existing = store.task_named(text)
        key = f"default_job:{mode}"
        if existing is not None and store.get(key) == cron:
            continue
        if existing is not None:
            store.cancel_task(existing)
        if cron:
            try:
                first = Cron(cron, tz=tz).next_after(time.time())
            except CronError as exc:
                log.warning("bad schedule %r for %s: %s", cron, mode, exc)
                continue
            store.add_task(text, mode, cron=cron, next_run=first, origin="settings")
        store.set(key, cron)


class Scheduler:
    def __init__(self, store: Store, *, deliver: Callable[[str], None], run_task: Callable[[str], str],
                 headlines: Callable[[], str], tz: Optional[str] = None, interval: float = 20.0):
        self.store = store
        self.deliver = deliver
        self.run_task = run_task
        self.headlines = headlines
        self.tz = tz
        self.interval = interval
        self._stop = threading.Event()

    def tick(self, now: Optional[float] = None) -> int:
        now = now or time.time()
        fired = 0
        for task in self.store.due_tasks(now):
            late = now - (task["next_run"] or now)
            try:
                if task["mode"] == "reminder":
                    note = f" (this was due {int(late // 60)} minutes ago)" if late > 300 else ""
                    self.deliver(f"Reminder: {task['text']}{note}")
                elif task["mode"] == "news":
                    self.deliver(self.headlines())
                else:
                    self.deliver(self.run_task(task["text"]))
            except Exception:
                log.exception("scheduled item #%s failed", task["id"])
            nxt = None
            if task["cron"]:
                try:
                    nxt = Cron(task["cron"], tz=self.tz).next_after(now)   # no backlog after the Mac was asleep
                except CronError:
                    nxt = None
            self.store.finish_task(task["id"], nxt)
            fired += 1
        return fired

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(self.interval)

    def start(self) -> None:
        threading.Thread(target=self.run, name="argus-scheduler", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
