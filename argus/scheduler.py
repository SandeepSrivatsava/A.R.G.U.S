"""Runs reminders and scheduled tasks when they're due, and keeps the built-in morning jobs in sync
with the settings. Results are spoken at the Mac (if you're there) and sent to your phone."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from .cron import Cron, CronError
from .store import Store

log = logging.getLogger("argus.scheduler")

HEADLINES_JOB = "Morning headlines"
BRIEFING_JOB = ("Morning briefing: today's weather (the weather tool, for 'here'); if you have the calendar tool, today's "
                "events and anything to leave early for; if you have mail_search, anything important that arrived since "
                "yesterday evening (bookings, replies from people, deliveries; skip newsletters and ads); the 3 most "
                "important headlines (get_news); and anything I asked you to remind me about today. Look things up "
                "together. Under 150 words, like a friend catching me up, no preamble.")
RETIRED_BRIEFINGS = ("Morning briefing: today's weather (the weather tool, for 'here'), the 5 most important headlines "
                     "(use get_news), and anything I asked you to remind me about today. Under 150 words, friendly, no "
                     "preamble.",)


def sync_default_jobs(store: Store, headlines_cron: str, briefing_cron: str, tz: Optional[str]) -> None:
    for old in RETIRED_BRIEFINGS:                     # the briefing's wording changed: replace the old job
        retired = store.task_named(old)
        if retired is not None:
            store.cancel_task(retired)
            store.set("default_job:task", "")
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
                 headlines: Callable[[], str], tz: Optional[str] = None, interval: float = 20.0,
                 trip_check: Optional[Callable[[Dict[str, Any]], str]] = None,
                 follow_check: Optional[Callable[[Dict[str, Any]], str]] = None):
        self.store = store
        self.follow_check = follow_check
        self.trip_check = trip_check
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
            again: Optional[float] = None
            try:
                if task["mode"] == "reminder":
                    note = f" (this was due {int(late // 60)} minutes ago)" if late > 300 else ""
                    self.deliver(f"Reminder: {task['text']}{note}")
                elif task["mode"] == "news":
                    self.deliver(self.headlines())
                elif task["mode"] == "followup":
                    from .followups import due
                    again = due(task, now, self.follow_check, self.deliver, self.tz)
                elif task["mode"] == "trip":
                    from .weather import read_trip
                    trip = read_trip(task["text"])
                    if trip is not None and self.trip_check is not None and trip["leave"] > now - 1800:
                        self.deliver(self.trip_check(trip))
                else:
                    self.deliver(self.run_task(task["text"]))
            except Exception as exc:
                log.exception("scheduled item #%s failed", task["id"])
                if task["mode"] not in ("reminder", "followup"):
                    try:
                        self.deliver(f"A scheduled job couldn't run ({task['text'][:60]}): {exc}")
                    except Exception:
                        pass
            nxt = None
            if task["cron"]:
                try:
                    nxt = Cron(task["cron"], tz=self.tz).next_after(now)   # no backlog after the Mac was asleep
                except CronError:
                    nxt = None
            self.store.finish_task(task["id"], nxt if nxt is not None else again)
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
