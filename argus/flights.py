"""How a flight has been running: delays and cancellations over the last two weeks, and today's status.

Data: FlightAware AeroAPI (your own key in secrets.env as FLIGHTAWARE_API_KEY; the Personal plan includes a
few dollars of free use a month, and one question costs about a cent). Without a key the tool isn't offered
and Argus looks on the web instead. Only the flight number is sent.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from .maps import MapsError, http_json

try:
    from zoneinfo import ZoneInfo
except ImportError:                                  # pragma: no cover
    ZoneInfo = None                                  # type: ignore

AEROAPI_URL = "https://aeroapi.flightaware.com/aeroapi"
RECENT_DAYS = 10            # /flights/{ident} reaches this far back; older days come from /history/flights/{ident}
MAX_DAYS = 14
LATE = 15 * 60              # a flight counts as late from 15 minutes (the usual airline measure)
_IDENT = re.compile(r"^[A-Z0-9]{2,3}\d{1,4}[A-Z]?$")


class FlightError(RuntimeError):
    pass


def clean_ident(text: Any) -> str:
    """"UA 328", "ua-328" or "United UA328" -> "UA328"."""
    words = re.sub(r"[^A-Za-z0-9 ]", " ", str(text or "")).upper().split()
    for candidate in ["".join(words)] + ["".join(words[i:i + 2]) for i in range(len(words))] + words:
        if _IDENT.match(candidate) and not candidate.isdigit():
            return candidate
    raise FlightError("that doesn't look like a flight number (like UA 328 or AI 174)")


def _when(value: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() if value else None
    except ValueError:
        return None


@dataclass
class Leg:
    ident: str
    origin: str
    destination: str
    zone: str
    scheduled_out: Optional[float]
    actual_out: Optional[float]
    scheduled_in: Optional[float]
    actual_in: Optional[float]
    departure_delay: Optional[int]      # seconds; negative = early
    arrival_delay: Optional[int]
    status: str
    cancelled: bool
    diverted: bool

    @property
    def flown(self) -> bool:
        return self.actual_in is not None or self.cancelled

    @property
    def lateness(self) -> Optional[int]:
        return self.arrival_delay if self.arrival_delay is not None else self.departure_delay


def _airport(data: Any) -> Tuple[str, str]:
    data = data if isinstance(data, dict) else {}
    return str(data.get("code_iata") or data.get("code") or "?"), str(data.get("timezone") or "")


def _leg(raw: Dict[str, Any]) -> Leg:
    origin, zone = _airport(raw.get("origin"))
    destination, _ = _airport(raw.get("destination"))

    def seconds(key: str) -> Optional[int]:
        value = raw.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return Leg(str(raw.get("ident_iata") or raw.get("ident") or ""), origin, destination, zone,
               _when(raw.get("scheduled_out") or raw.get("scheduled_off")),
               _when(raw.get("actual_out") or raw.get("actual_off")),
               _when(raw.get("scheduled_in") or raw.get("scheduled_on")),
               _when(raw.get("actual_in") or raw.get("actual_on")),
               seconds("departure_delay"), seconds("arrival_delay"), str(raw.get("status") or ""),
               bool(raw.get("cancelled")), bool(raw.get("diverted")))


class FlightAware:
    def __init__(self, key: str, *, base: str = AEROAPI_URL, request: Callable[..., Tuple[int, Any]] = http_json,
                 clock: Callable[[], float] = time.time):
        self.key, self.base, self.request, self.clock = key, base.rstrip("/"), request, clock

    def _get(self, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            status, data = self.request("GET", f"{self.base}{path}?{urlencode(params)}", {"x-apikey": self.key})
        except MapsError as exc:
            raise FlightError(str(exc)) from exc
        if status == 200 and isinstance(data, dict):
            return [f for f in data.get("flights") or [] if isinstance(f, dict)]
        if status in (401, 403):
            raise FlightError("FlightAware refused the key (check FLIGHTAWARE_API_KEY)")
        if status == 429:
            raise FlightError("FlightAware's limit was reached; try again in a minute")
        detail = ""
        if isinstance(data, dict):
            detail = " ".join(str(data.get("detail") or data.get("title") or "").split())[:160].replace(self.key, "...")
        raise FlightError(f"FlightAware answered HTTP {status}" + (f" ({detail})" if detail else ""))

    def legs(self, ident: str, days: int = MAX_DAYS) -> Tuple[List[Leg], str]:
        """This flight number's flights over the last `days` days (and the next two), oldest first, plus a note
        when the older days couldn't be read."""
        days = max(1, min(int(days), MAX_DAYS))
        now = self.clock()

        def stamp(at: float) -> str:
            return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        path = f"/flights/{quote(ident, safe='')}"
        recent = self._get(path, {"ident_type": "designator", "max_pages": 2,
                                  "start": stamp(now - min(days, RECENT_DAYS) * 86400 + 600)})
        note = ""
        older: List[Dict[str, Any]] = []
        if days > RECENT_DAYS:
            try:
                older = self._get("/history" + path, {"ident_type": "designator", "max_pages": 2,
                                                      "start": stamp(now - days * 86400),
                                                      "end": stamp(now - RECENT_DAYS * 86400 + 600)})
            except FlightError as exc:
                note = f"Only the last {RECENT_DAYS} days could be read ({exc})."
        legs, seen = [], set()
        for raw in older + recent:
            leg = _leg(raw)
            key = (leg.origin, leg.scheduled_out)
            if leg.scheduled_out is not None and key not in seen:
                seen.add(key)
                legs.append(leg)
        return sorted(legs, key=lambda leg: leg.scheduled_out or 0), note


def _minutes(seconds: int) -> str:
    minutes = round(abs(seconds) / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def _delay(seconds: Optional[int], verb: str) -> str:
    if seconds is None:
        return ""
    if abs(seconds) < 5 * 60:
        return f"{verb} on time"
    return f"{verb} {_minutes(seconds)} {'late' if seconds > 0 else 'early'}"


def _local(at: float, zone: str, fmt: str) -> str:
    tz = None
    if zone and ZoneInfo is not None:
        try:
            tz = ZoneInfo(zone)
        except Exception:
            tz = None
    return datetime.fromtimestamp(at, tz or timezone.utc).strftime(fmt) + ("" if tz else " UTC")


def report(ident: str, legs: List[Leg], days: int, note: str = "", origin: str = "") -> str:
    if origin:
        legs = [leg for leg in legs if leg.origin.upper() == origin.upper()]
    if not legs:
        return (f"No flights found for {ident}" + (f" from {origin.upper()}" if origin else "") +
                f" in the last {days} days. Check the flight number (airline code plus number).")
    lines = [f"Flight {ident}, last {days} days (times are local to the departure airport):"]
    for leg in legs:
        day = _local(leg.scheduled_out, leg.zone, "%a %d %b %H:%M")
        route = f"{leg.origin} to {leg.destination}"
        if leg.cancelled:
            what = "CANCELLED"
        elif leg.actual_in is not None or leg.actual_out is not None:
            parts = [p for p in (_delay(leg.departure_delay, "left"), _delay(leg.arrival_delay, "arrived")) if p]
            what = ", ".join(parts) or (leg.status or "flew")
            if leg.actual_in is None:
                what += " (in the air or not yet arrived)"
            if leg.diverted:
                what += ", DIVERTED to another airport"
        else:
            what = f"not flown yet ({leg.status or 'scheduled'})"
            if leg.departure_delay and leg.departure_delay >= LATE:
                what += f", expected to leave {_minutes(leg.departure_delay)} late"
        lines.append(f"- {day}, {route}: {what}")
    done = [leg for leg in legs if leg.flown]
    if done:
        cancelled = sum(leg.cancelled for leg in done)
        flew = [leg for leg in done if not leg.cancelled and leg.lateness is not None]
        late = [leg for leg in flew if leg.lateness >= LATE]
        summary = f"Summary: {len(done)} flights, {cancelled} cancelled, {len(late)} arrived 15 minutes or more late"
        if flew:
            average = sum(max(leg.lateness, 0) for leg in flew) / len(flew)
            worst = max(flew, key=lambda leg: leg.lateness)
            summary += f"; average delay {_minutes(int(average))}"
            if worst.lateness >= LATE:
                summary += f", worst {_minutes(worst.lateness)} on {_local(worst.scheduled_out, worst.zone, '%a %d %b')}"
        lines.append(summary + ".")
    if len({(leg.origin, leg.destination) for leg in legs}) > 1 and not origin:
        lines.append("This number flies more than one route; ask with the departure airport to see just one.")
    if note:
        lines.append(note)
    lines.append("Source: FlightAware.")
    return "\n".join(lines)


TOOL_DESCRIPTION = (
    "How a flight has been running: every day's delay or cancellation for a flight number over the last days "
    "(up to 14), plus today's and tomorrow's status. Use it when the user asks whether their flight is usually "
    "late or was cancelled recently, or how today's flight is doing.")
TOOL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["flight"], "properties": {
    "flight": {"type": "string", "description": "Airline code and number, like UA 328 or AI 174."},
    "days": {"type": "integer", "minimum": 1, "maximum": MAX_DAYS, "description": "How many days back (default 14)."},
    "origin": {"type": "string", "description": "Departure airport code (like SFO), when the number flies several routes."}}}


def attach(secrets: Dict[str, str], tools: Dict[str, Any], *, request=http_json) -> Optional[FlightAware]:
    """Adds the flight_history tool when a FlightAware key is saved."""
    from .toolbox import Tool, ToolOutput
    key = (secrets.get("FLIGHTAWARE_API_KEY") or "").strip()
    if not key:
        return None
    service = FlightAware(key, request=request)

    def run(args: Dict[str, Any], ctx) -> ToolOutput:
        try:
            ident = clean_ident(args.get("flight"))
            days = args.get("days")
            days = MAX_DAYS if not isinstance(days, int) or isinstance(days, bool) else max(1, min(days, MAX_DAYS))
            legs, note = service.legs(ident, days)
        except FlightError as exc:
            return ToolOutput.error(f"Couldn't get the flight's history: {exc}. You can look it up with web_search instead.")
        return ToolOutput(report(ident, legs, days, note, str(args.get("origin") or "").strip()[:4]))

    tools["flight_history"] = Tool("flight_history", TOOL_DESCRIPTION, TOOL_SCHEMA, run,
                                   frozenset({"voice", "phone", "cli", "android", "scheduler"}))
    return service
