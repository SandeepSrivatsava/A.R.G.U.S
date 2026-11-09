"""A small cron-expression scheduler (minute hour day-of-month month day-of-week), in local time."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Set

_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12, "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int) -> Set[int]:
    values: Set[int] = set()
    for part in text.lower().split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"bad step in {text!r}")
            step = int(step_text)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _num(a), _num(b)
        else:
            start = end = _num(part)
            if step > 1:
                end = hi
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise CronError(f"{text!r} is out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return values


def _num(text: str) -> int:
    text = text.strip()
    if text in _NAMES:
        return _NAMES[text]
    if not text.isdigit():
        raise CronError(f"{text!r} isn't a number")
    return int(text)


class Cron:
    def __init__(self, expr: str, tz: Optional[str] = None):
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("use 5 fields: minute hour day-of-month month day-of-week")
        self.expr = expr
        self.minutes, self.hours, self.days, self.months, dows = (
            _field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES))
        self.dows = {d % 7 for d in dows}
        self.any_day = parts[2] == "*"
        self.any_dow = parts[4] == "*"
        self.tz = None
        if tz:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(tz)

    def _day_ok(self, d: datetime) -> bool:
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.dows
        if self.any_day and self.any_dow:
            return True
        if self.any_day:
            return dow
        if self.any_dow:
            return dom
        return dom or dow   # classic cron: either restriction matches

    def next_after(self, ts: float) -> float:
        t = datetime.fromtimestamp(ts, self.tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(60 * 24 * 370):
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            return t.timestamp()
        raise CronError("that schedule never runs")
