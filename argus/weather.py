"""Weather where you are and where you're going, and a heads-up before a trip.

* The forecast comes from Open-Meteo (free, no key, worldwide, up to 16 days ahead; data CC BY 4.0) and the
  official warnings (storms, floods, snow, heat) from the US National Weather Service (weather.gov, free).
* "Here" is where your phone is: the location it sent with this request, or (at the Mac) the last place it
  sent in the past few hours. Without either, your home city from the settings, and the answer says so.
* Places are sent rounded to about a kilometre (two decimals). The coordinates never reach Claude: the tool
  says "where you are now".
* watch_trip: "I'm driving to Tahoe Saturday at 8 to ski" -> Argus checks the weather (and the traffic, close
  to leaving) the evening before and an hour before you leave, and tells you out loud at the Mac or on your
  phone whether it still looks good for skiing. Those checks run with only the weather and route tools.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from .maps import HERE_WORDS, MapsError, Point, Requester, http_json, parse_latlon

if TYPE_CHECKING:
    from .toolbox import ToolOutput

log = logging.getLogger("argus.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
ALERTS_URL = "https://api.weather.gov/alerts/active"
MAX_DAYS = 16                      # how far ahead Open-Meteo forecasts
FORECAST_FRESH = 600               # seconds a fetched forecast is reused
RECENT_FOR_WEATHER = 3 * 3600      # at the Mac, the phone's last location counts as "here" for this long
HOURLY = ("temperature_2m,apparent_temperature,precipitation_probability,precipitation,snowfall,weather_code,"
          "wind_speed_10m,wind_gusts_10m")
DAILY = ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,"
         "snowfall_sum,wind_gusts_10m_max,sunrise,sunset")
CURRENT = "temperature_2m,apparent_temperature,weather_code,precipitation,wind_speed_10m,wind_gusts_10m"

# WMO weather codes, as Open-Meteo gives them
CODES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
         51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "heavy freezing drizzle",
         61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
         71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
         81: "heavy rain showers", 82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers",
         95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with heavy hail"}
ROAD_RISK = {45, 48, 56, 57, 66, 67, 71, 73, 75, 77, 85, 86, 65, 82, 95, 96, 99}     # worth a word to a driver
STATES = {"al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado",
          "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
          "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
          "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
          "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
          "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
          "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
          "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas",
          "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
          "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia"}

COUNTRIES = {"usa": "united states", "us": "united states", "america": "united states",
             "united states of america": "united states", "uk": "united kingdom", "england": "united kingdom"}


class WeatherError(RuntimeError):
    """Something a person can act on: a place that wasn't found, a date too far ahead, the service is down."""


def rounded(point: Point) -> Point:
    """About a kilometre: enough for a forecast, without sending exactly where you are."""
    return (round(point[0], 2), round(point[1], 2))


# ---- the phone's last known place (in memory only) ------------------------------------------------------
class RecentPlace:
    """The last location a phone sent, kept in memory (never on disk) so a question at the Mac can use it."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._place: Optional[Dict[str, float]] = None
        self._at = 0.0
        self._lock = threading.Lock()

    def note(self, location: Optional[Dict[str, float]]) -> None:
        if location and "lat" in location and "lon" in location:
            with self._lock:
                self._place, self._at = dict(location), self.clock()

    def get(self, max_age: float) -> Optional[Dict[str, float]]:
        with self._lock:
            if self._place is None or self.clock() - self._at > max_age:
                return None
            return dict(self._place)


# ---- the forecast -------------------------------------------------------------------------------------------
@dataclass
class Hour:
    at: int                              # epoch seconds
    temp: Optional[float]
    feels: Optional[float]
    rain_chance: Optional[int]
    rain: Optional[float]
    snow: Optional[float]
    code: Optional[int]
    wind: Optional[float]
    gusts: Optional[float]

    @property
    def sky(self) -> str:
        return CODES.get(self.code, "") if self.code is not None else ""


@dataclass
class Day:
    at: int                              # local midnight, epoch seconds
    code: Optional[int]
    high: Optional[float]
    low: Optional[float]
    rain_chance: Optional[int]
    rain: Optional[float]
    snow: Optional[float]
    gusts: Optional[float]
    sunrise: Optional[int]
    sunset: Optional[int]


@dataclass
class Forecast:
    offset: int                          # the place's UTC offset, seconds
    now: Optional[Hour]
    hours: List[Hour] = field(default_factory=list)
    days: List[Day] = field(default_factory=list)

    def local(self, epoch: float) -> datetime:
        return datetime.fromtimestamp(epoch + self.offset, timezone.utc).replace(tzinfo=None)

    def hour_at(self, epoch: float) -> Optional[Hour]:
        near = [h for h in self.hours if abs(h.at - epoch) <= 1800]
        return min(near, key=lambda h: abs(h.at - epoch)) if near else None

    def day_of(self, epoch: float) -> Optional[Day]:
        return next((d for d in self.days if d.at <= epoch < d.at + 86400), None)


@dataclass
class Alert:
    event: str
    headline: str
    severity: str
    ends: str = ""


def _num(values: Any, i: int) -> Optional[float]:
    try:
        value = values[i]
    except (IndexError, KeyError, TypeError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: Optional[float]) -> Optional[int]:
    return int(round(value)) if value is not None else None


class Weather:
    def __init__(self, *, units: str = "imperial", request: Requester = http_json,
                 geocode: Optional[Callable[[str], Optional[Tuple[Point, str]]]] = None,
                 clock: Callable[[], float] = time.time, forecast_url: str = FORECAST_URL,
                 geocode_url: str = GEOCODE_URL, alerts_url: str = ALERTS_URL):
        self.units, self.request, self.extra_geocode, self.clock = units, request, geocode, clock
        self.forecast_url, self.geocode_url, self.alerts_url = forecast_url, geocode_url, alerts_url
        self._places: Dict[str, Tuple[Point, str]] = {}                 # a place, once found, isn't looked up again
        self._forecasts: Dict[Point, Tuple[float, int, "Forecast"]] = {}  # (when fetched, days, forecast)
        self._lock = threading.Lock()

    @property
    def degrees(self) -> str:
        return "°F" if self.units == "imperial" else "°C"

    @property
    def speed(self) -> str:
        return "mph" if self.units == "imperial" else "km/h"

    @property
    def depth(self) -> str:
        return "in" if self.units == "imperial" else "mm"

    # ---- finding a place ----
    def locate(self, text: str) -> Tuple[Point, str]:
        """(point, name) for "lat,lon", a city like "Howell, MI", or (with a TomTom key) a street address."""
        key = " ".join(text.lower().split())
        with self._lock:
            if key in self._places:
                return self._places[key]
        found = self._locate(text)
        with self._lock:
            if len(self._places) > 200:
                self._places.clear()
            self._places[key] = found
        return found

    def _locate(self, text: str) -> Tuple[Point, str]:
        point = parse_latlon(text)
        if point:
            return point, text
        if self.extra_geocode is not None and re.search(r"\d", text):      # an address: TomTom reads those
            try:
                found = self.extra_geocode(text)
            except MapsError as exc:
                log.info("address lookup failed: %s", exc)
                found = None
            if found:
                return found
        parts = [p.strip() for p in text.split(",") if p.strip()]
        for start in range(len(parts)):                    # "12 Elm St, Howell, MI" -> "Howell" with "MI"
            name = parts[start]
            if re.search(r"\d", name) and start + 1 < len(parts):
                continue
            found = self._open_meteo_place(name, parts[start + 1:])
            if found:
                return found
        raise WeatherError(f"couldn't find a place called {text}")

    def _open_meteo_place(self, name: str, hints: Sequence[str]) -> Optional[Tuple[Point, str]]:
        query = urlencode({"name": name[:80], "count": 10, "language": "en", "format": "json"})
        status, data = self.request("GET", f"{self.geocode_url}?{query}", {})
        if status != 200 or not isinstance(data, dict):
            raise WeatherError(f"the place lookup answered HTTP {status}")
        results = [r for r in data.get("results") or [] if isinstance(r, dict)]
        wanted = [STATES.get(h.lower(), COUNTRIES.get(h.lower().replace(".", ""), h.lower())) for h in hints]

        def fits(r: Dict[str, Any]) -> bool:
            names = {str(r.get(k) or "").lower() for k in ("admin1", "admin2", "country", "country_code")}
            return all(any(w == n or (len(w) > 3 and w in n) for n in names) for w in wanted)
        for r in [r for r in results if fits(r)] or ([] if wanted else results[:1]):
            try:
                point = (float(r["latitude"]), float(r["longitude"]))
            except (KeyError, TypeError, ValueError):
                continue
            label = ", ".join(str(r[k]) for k in ("name", "admin1", "country") if r.get(k))
            return point, label
        return None

    # ---- the forecast ----
    def forecast(self, point: Point, days: int = 7) -> Forecast:
        """The forecast for a place, kept for 10 minutes (forecasts change hourly at most)."""
        key = rounded(point)
        days = max(1, min(MAX_DAYS, days))
        with self._lock:
            cached = self._forecasts.get(key)
        if cached and cached[1] >= days and self.clock() - cached[0] < FORECAST_FRESH:
            return cached[2]
        found = self._fetch_forecast(key, days)
        with self._lock:
            if len(self._forecasts) > 100:
                self._forecasts.clear()
            self._forecasts[key] = (self.clock(), days, found)
        return found

    def _fetch_forecast(self, point: Point, days: int) -> Forecast:
        lat, lon = rounded(point)
        params = {"latitude": f"{lat:.2f}", "longitude": f"{lon:.2f}", "hourly": HOURLY, "daily": DAILY,
                  "current": CURRENT, "timezone": "auto", "timeformat": "unixtime",
                  "forecast_days": max(1, min(MAX_DAYS, days))}
        if self.units == "imperial":
            params.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
        status, data = self.request("GET", f"{self.forecast_url}?{urlencode(params)}", {})
        if status != 200 or not isinstance(data, dict):
            reason = data.get("reason") if isinstance(data, dict) else ""
            raise WeatherError(f"the forecast service answered HTTP {status}" + (f" ({str(reason)[:120]})" if reason else ""))
        offset = int(data.get("utc_offset_seconds") or 0) if isinstance(data.get("utc_offset_seconds"), (int, float)) else 0
        hourly, daily, current = (data.get(k) if isinstance(data.get(k), dict) else {} for k in ("hourly", "daily", "current"))
        hours = []
        for i, at in enumerate(hourly.get("time") or []):
            if isinstance(at, (int, float)) and not isinstance(at, bool):
                hours.append(Hour(int(at), _num(hourly.get("temperature_2m"), i), _num(hourly.get("apparent_temperature"), i),
                                  _int(_num(hourly.get("precipitation_probability"), i)), _num(hourly.get("precipitation"), i),
                                  _num(hourly.get("snowfall"), i), _int(_num(hourly.get("weather_code"), i)),
                                  _num(hourly.get("wind_speed_10m"), i), _num(hourly.get("wind_gusts_10m"), i)))
        days_ = []
        for i, at in enumerate(daily.get("time") or []):
            if isinstance(at, (int, float)) and not isinstance(at, bool):
                days_.append(Day(int(at), _int(_num(daily.get("weather_code"), i)), _num(daily.get("temperature_2m_max"), i),
                                 _num(daily.get("temperature_2m_min"), i), _int(_num(daily.get("precipitation_probability_max"), i)),
                                 _num(daily.get("precipitation_sum"), i), _num(daily.get("snowfall_sum"), i),
                                 _num(daily.get("wind_gusts_10m_max"), i), _int(_num(daily.get("sunrise"), i)),
                                 _int(_num(daily.get("sunset"), i))))
        now = None
        if isinstance(current.get("time"), (int, float)):
            now = Hour(int(current["time"]), _num([current.get("temperature_2m")], 0),
                       _num([current.get("apparent_temperature")], 0), None, _num([current.get("precipitation")], 0),
                       None, _int(_num([current.get("weather_code")], 0)), _num([current.get("wind_speed_10m")], 0),
                       _num([current.get("wind_gusts_10m")], 0))
        return Forecast(offset=offset, now=now, hours=hours, days=days_)

    def alerts(self, point: Point) -> List[Alert]:
        """Official US warnings in force at a place (weather.gov). Outside the US, or if it fails: none."""
        lat, lon = rounded(point)
        try:
            status, data = self.request("GET", f"{self.alerts_url}?{urlencode({'point': f'{lat:.2f},{lon:.2f}'})}",
                                        {"Accept": "application/geo+json", "User-Agent": "Argus personal assistant"},
                                        timeout=8)
        except MapsError as exc:
            log.info("weather warnings unavailable: %s", exc)
            return []
        if status != 200 or not isinstance(data, dict):
            return []
        found: List[Alert] = []
        for feature in data.get("features") or []:
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(props, dict) or props.get("status", "Actual") != "Actual":
                continue
            severity = str(props.get("severity") or "")
            if severity not in ("Extreme", "Severe", "Moderate"):
                continue
            event = " ".join(str(props.get("event") or "").split())[:60]
            headline = " ".join(str(props.get("headline") or "").split())[:200]
            if event and not any(a.event == event for a in found):
                found.append(Alert(event, headline, severity, str(props.get("ends") or props.get("expires") or "")))
        order = {"Extreme": 0, "Severe": 1, "Moderate": 2}
        return sorted(found, key=lambda a: order[a.severity])[:4]

    # ---- in words ----
    def temp(self, value: Optional[float]) -> str:
        return f"{int(round(value))}{self.degrees}" if value is not None else ""

    def hour_line(self, hour: Hour, fc: Forecast, label: str = "") -> str:
        parts = [label or fc.local(hour.at).strftime("%H:%M") + ":"]
        if hour.temp is not None:
            feels = f" (feels like {self.temp(hour.feels)})" if hour.feels is not None and abs(hour.feels - hour.temp) >= 5 else ""
            parts.append(self.temp(hour.temp) + feels + ",")
        if hour.sky:
            parts.append(hour.sky + ",")
        if hour.rain_chance is not None and hour.rain_chance >= 10:
            parts.append(f"{hour.rain_chance}% chance of rain or snow,")
        if hour.snow:
            parts.append(f"{hour.snow:.1f} {'in' if self.units == 'imperial' else 'cm'} of snow that hour,")
        if hour.wind is not None:
            gusts = f", gusts {int(round(hour.gusts))}" if hour.gusts is not None and hour.gusts - hour.wind >= 10 else ""
            parts.append(f"wind {int(round(hour.wind))}{gusts} {self.speed}")
        return " ".join(parts).rstrip(",") + "."

    def day_line(self, day: Day, fc: Forecast) -> str:
        bits = [fc.local(day.at).strftime("%a %d %b") + ":"]
        if day.low is not None and day.high is not None:
            bits.append(f"{self.temp(day.low)} to {self.temp(day.high)},")
        if day.code is not None:
            bits.append(CODES.get(day.code, "") + ",")
        if day.rain_chance is not None:
            bits.append(f"up to {day.rain_chance}% chance of rain or snow" + ("," if day.rain or day.snow else ""))
        if day.rain:
            bits.append(f"{day.rain:.2f} {self.depth} in all" + ("," if day.snow else ""))
        if day.snow:
            bits.append(f"{day.snow:.1f} {'in' if self.units == 'imperial' else 'cm'} of snow")
        if day.gusts is not None and day.gusts >= 30:
            bits.append(f"(gusts to {int(round(day.gusts))} {self.speed})")
        if day.sunset:
            bits.append(f"; sunset {fc.local(day.sunset).strftime('%H:%M')}")
        return " ".join(b for b in bits if b).replace(" ;", ";").rstrip(",") + "."

    def report(self, point: Point, name: str, start: Optional[float] = None, whole_day: bool = False,
               purpose: str = "") -> str:
        """The forecast in words, for Claude: now, a time (and the hours around it), or a whole day."""
        now = self.clock()
        if start is not None and start - now > (MAX_DAYS - 0.5) * 86400:
            raise WeatherError(f"that's more than {MAX_DAYS} days away, past what forecasts cover; watch_trip can "
                               "check again closer to the time")
        need = MAX_DAYS if start is None else max(2, min(MAX_DAYS, int((start - now) // 86400) + 2))
        warnings: Dict[str, Any] = {}
        fetcher = None
        if start is None or start - now < 4 * 86400:                 # today's warnings, fetched meanwhile
            fetcher = threading.Thread(target=lambda: warnings.update(found=self.alerts(point)), daemon=True)
            fetcher.start()
        fc = self.forecast(point, days=need if start is not None else 3)
        lines = [f"Weather for {name} (Open-Meteo forecast; local times there):"]
        if start is None:
            if fc.now:
                lines.append(self.hour_line(fc.now, fc, "Now:"))
            ahead = [h for h in fc.hours if now < h.at <= now + 12 * 3600]
            lines += ["Next hours: " + " ".join(self.hour_line(h, fc) for h in ahead[2::3][:4])] if ahead else []
            today = fc.day_of(now)
            tomorrow = fc.day_of(now + 86400)
            lines += ["Today " + self.day_line(today, fc)] if today else []
            lines += ["Tomorrow " + self.day_line(tomorrow, fc)] if tomorrow else []
        elif whole_day:
            day = fc.day_of(start + 12 * 3600)
            if day is None:
                raise WeatherError("the forecast doesn't reach that day yet")
            lines.append(self.day_line(day, fc))
            daytime = [h for h in fc.hours if day.at + 8 * 3600 <= h.at <= day.at + 20 * 3600][::3]
            lines += ["Through the day: " + " ".join(self.hour_line(h, fc) for h in daytime)] if daytime else []
        else:
            hour = fc.hour_at(start)
            if hour is None:
                raise WeatherError("the forecast doesn't reach that time yet")
            lines.append(self.hour_line(hour, fc, fc.local(start).strftime("Around %H:%M on %a %d %b:")))
            around = [h for h in fc.hours if start < h.at <= start + 4 * 3600][1::2]
            lines += ["After that: " + " ".join(self.hour_line(h, fc) for h in around)] if around else []
            day = fc.day_of(start)
            lines += ["The whole day " + self.day_line(day, fc)] if day else []
        if fetcher is not None:
            fetcher.join(timeout=8)
            for alert in warnings.get("found") or []:
                ends = ""
                try:
                    ends = " until " + datetime.fromisoformat(alert.ends).strftime("%a %H:%M") if alert.ends else ""
                except ValueError:
                    pass
                lines.append(f"Official warning (US National Weather Service, in force now): {alert.event}{ends}. "
                             f"{alert.headline}".rstrip())
        if start is not None and start - now > 5 * 86400:
            lines.append("Note: forecasts this far ahead change a lot; give the general picture only.")
        if purpose:
            lines.append(f"The user is going for: {purpose[:120]}. Say plainly whether this weather suits it, "
                         "and what to bring or change.")
        return "\n".join(lines)

    def on_the_way(self, point: Point, at: float, label: str) -> str:
        """One line for a route: the weather at a place on the way at the time you'd be there."""
        fc = self.forecast(point, days=max(2, min(MAX_DAYS, int((at - self.clock()) // 86400) + 2)))
        hour = fc.hour_at(at)
        if hour is None:
            return ""
        line = self.hour_line(hour, fc, f"{label} (about {fc.local(at).strftime('%H:%M')} there):")
        if hour.code in ROAD_RISK:
            line += " Take care on the road."
        return line


# ---- the tools ----------------------------------------------------------------------------------------------
WEATHER_DESCRIPTION = (
    "The weather forecast (and official US weather warnings) for a place and time: 'here' (where the user's "
    "phone is), a city, or an address; now, a time, or a whole day, up to 16 days ahead. Use it for every "
    "weather question instead of web search, and whenever the user is going somewhere, for the place and time "
    "they'll be there. Give 'purpose' when you know what they're going for (a hike, the beach, a drive, a "
    "flight, a game outdoors).")
WEATHER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "place": {"type": "string", "description": "'here' (default), a city with its state or country, or an address"},
        "when": {"type": "string", "description": "ISO local date-time at that place (2026-11-25T15:00) or a date "
                                                  "(2026-11-25) for the whole day; empty for now"},
        "purpose": {"type": "string", "description": "what the user is going there for, if known"},
    },
}
TRIP_DESCRIPTION = (
    "Keep an eye on an upcoming trip: Argus checks the weather at the destination (and the live traffic, when "
    "it's close to leaving) the evening before and an hour before the user leaves, and tells them out loud at "
    "the Mac or on their phone whether it still looks good for what they're going for. Use it when the user "
    "mentions plans to go somewhere later (offer first if they didn't ask). action 'list' shows the trips "
    "being watched, 'cancel' stops one (by its number from schedule's list).")
TRIP_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["watch", "list", "cancel"]},
        "destination": {"type": "string", "description": "the place, with its city"},
        "leave_at": {"type": "string", "description": "when they leave, ISO local date-time like 2026-11-25T08:00"},
        "origin": {"type": "string", "description": "where they leave from, if not 'here' / home"},
        "purpose": {"type": "string", "description": "what it's for (skiing, a wedding, a hike...)"},
        "mode": {"type": "string", "enum": ["drive", "walk", "transit", "bike"]},
        "id": {"type": "integer"},
    },
}
TRIP_PREFIX = "trip:"
MAX_TRIPS = 10
EVENING_BEFORE = 19                 # o'clock, the day before leaving


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


class WeatherTools:
    def __init__(self, weather: Weather, *, home: str = "", recent: Optional[RecentPlace] = None,
                 clock: Callable[[], float] = time.time):
        self.weather, self.home, self.recent, self.clock = weather, home, recent, clock

    def where_is_here(self, ctx) -> Tuple[Optional[Point], str]:
        if getattr(ctx, "channel", "") == "guest":       # a guest gets nothing about where the owner is
            return None, ""
        location = getattr(ctx, "location", None)
        if not location and getattr(ctx, "locate", None) is not None:
            try:
                location = ctx.locate()
            except Exception as exc:
                log.info("couldn't get the phone's location: %s", exc)
        if not location and self.recent is not None:
            location = self.recent.get(RECENT_FOR_WEATHER)
        if location:
            return (location["lat"], location["lon"]), "where the user is now"
        return None, ""

    def weather_tool(self, args: Dict[str, Any], ctx) -> "ToolOutput":
        from .toolbox import ToolOutput
        text = _clean(args.get("place"), 200)
        purpose = _clean(args.get("purpose"), 120)
        try:
            if text.lower() in HERE_WORDS:
                point, name = self.where_is_here(ctx)
                note = ""
                if point is None:
                    if not self.home or getattr(ctx, "channel", "") == "guest":
                        return ToolOutput.error("The user's location isn't available (only their phone shares it, "
                                                "with a request) and no home city is set. Ask where they are.")
                    point, name = self.weather.locate(self.home)
                    note = (f"\n(The phone's location isn't available here, so this is for the home city, "
                            f"{self.home}; say so.)")
            else:
                point, name = self.weather.locate(text)
                note = ""
            start, whole_day = None, False
            when = _clean(args.get("when"), 40)
            if when:
                start, whole_day = self._when(when, point)
            return ToolOutput(self.weather.report(point, name, start, whole_day, purpose) + note)
        except (WeatherError, MapsError) as exc:
            return ToolOutput.error(f"No forecast: {exc}.")

    def _when(self, when: str, point: Point) -> Tuple[float, bool]:
        """A local date or date-time at the place -> (epoch, whole day?)."""
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
                local, whole = datetime.fromisoformat(when + "T00:00"), True
            else:
                local, whole = datetime.fromisoformat(when.replace("Z", "+00:00")), False
        except ValueError:
            raise WeatherError("give 'when' as 2026-11-25 or 2026-11-25T15:00") from None
        if local.tzinfo is not None:
            return local.timestamp(), whole
        offset = self.weather.forecast(point, days=MAX_DAYS).offset     # the place's own clock (kept for the report)
        epoch = local.replace(tzinfo=timezone.utc).timestamp() - offset
        if epoch < self.clock() - 3 * 3600 and not (whole and epoch > self.clock() - 86400):
            raise WeatherError("that time has already passed")
        return epoch, whole

    # ---- watching a trip ----
    def trip_tool(self, args: Dict[str, Any], ctx) -> "ToolOutput":
        from .toolbox import ToolOutput, parse_when
        action = args.get("action")
        store = ctx.store
        trips = [t for t in store.tasks() if t["mode"] == "trip"]
        if action == "list":
            if not trips:
                return ToolOutput("no trips being watched")
            return ToolOutput("\n".join(f"#{t['id']} {describe_trip(t['text'])}, check "
                                        f"{datetime.fromtimestamp(t['next_run']).strftime('%a %d %b %H:%M')}"
                                        for t in trips if t["next_run"]))
        if action == "cancel":
            ident = args.get("id")
            if not any(t["id"] == ident for t in trips):
                return ToolOutput.error("no such trip check (use action 'list' for the numbers)")
            store.cancel_task(ident)
            return ToolOutput("cancelled")
        if action != "watch":
            return ToolOutput.error("action must be watch, list or cancel")
        destination = _clean(args.get("destination"), 160)
        if not destination:
            return ToolOutput.error("say where the trip is to")
        try:
            leave = parse_when(_clean(args.get("leave_at"), 40), getattr(ctx, "timezone", None))
        except ValueError:
            return ToolOutput.error("give leave_at as an ISO local date-time like 2026-11-25T08:00")
        now = self.clock()
        if leave <= now + 3600:
            return ToolOutput.error("that's within the hour: check the weather and the route now instead")
        if leave - now > 60 * 86400:
            return ToolOutput.error("that's more than two months away; ask again closer to the time")
        if len(trips) >= MAX_TRIPS:
            return ToolOutput.error(f"already watching {MAX_TRIPS} trip checks; cancel one first")
        mode = args.get("mode") if args.get("mode") in ("drive", "walk", "transit", "bike") else "drive"
        trip = {"to": destination, "from": _clean(args.get("origin"), 160), "leave": int(leave),
                "purpose": _clean(args.get("purpose"), 120), "mode": mode}
        checks: List[Tuple[float, str]] = [(leave - 3600, "an hour before leaving")]
        evening = datetime.fromtimestamp(leave).replace(hour=EVENING_BEFORE, minute=0, second=0, microsecond=0) - timedelta(days=1)
        if evening.timestamp() > now + 600 and leave - evening.timestamp() >= 8 * 3600:
            checks.insert(0, (evening.timestamp(), "the evening before"))
        made = []
        for at, stage in checks:
            text = TRIP_PREFIX + json.dumps(dict(trip, stage=stage), separators=(",", ":"))
            made.append((store.add_task(text, "trip", due=at, origin=getattr(ctx, "channel", "voice")), at, stage))
        return ToolOutput("Watching the trip: " + "; ".join(
            f"#{tid} {stage} ({datetime.fromtimestamp(at).strftime('%a %d %b %H:%M')})" for tid, at, stage in made) +
            ". Each check is spoken at the Mac, or sent to the phone if the user is away.")


def read_trip(text: str) -> Optional[Dict[str, Any]]:
    if not str(text).startswith(TRIP_PREFIX):
        return None
    try:
        trip = json.loads(text[len(TRIP_PREFIX):])
    except ValueError:
        return None
    return trip if isinstance(trip, dict) and trip.get("to") and isinstance(trip.get("leave"), int) else None


def describe_trip(text: str) -> str:
    trip = read_trip(text)
    if trip is None:
        return "a trip check"
    when = datetime.fromtimestamp(trip["leave"]).strftime("%a %d %b %H:%M")
    why = f" for {trip['purpose']}" if trip.get("purpose") else ""
    return f"trip to {trip['to']}{why}, leaving {when} ({trip.get('stage', 'check')})"


def trip_request(trip: Dict[str, Any], user: str) -> str:
    """What a trip check asks Claude (which then has only the weather and route tools)."""
    leave = datetime.fromtimestamp(trip["leave"])
    origin = trip.get("from") or "here"
    purpose = trip.get("purpose") or "not said"
    soon = trip.get("stage") == "an hour before leaving"
    return (f"Trip check ({trip.get('stage', 'before the trip')}): {user} is going to {trip['to']} from "
            f"{'their current location' if origin == 'here' else origin}, leaving {leave.strftime('%a %d %b at %H:%M')}, "
            f"by {trip.get('mode', 'drive')}. They're going for: {purpose}. Use the weather tool for {trip['to']} at the "
            f"time they'll arrive" + (" and the route tool for the live traffic" if soon else "") +
            f". Then tell {user}, in two or three spoken sentences, whether it still looks good for that, anything "
            f"that changed or to watch out for on the way, and what to bring or do differently.")


def weather_tools(tools: WeatherTools):
    from .toolbox import Tool
    channels = frozenset({"voice", "phone", "scheduler", "cli", "android", "trip"})
    return [Tool("weather", WEATHER_DESCRIPTION, WEATHER_SCHEMA, tools.weather_tool, channels | {"guest"}),
            Tool("watch_trip", TRIP_DESCRIPTION, TRIP_SCHEMA, tools.trip_tool,
                 frozenset({"voice", "phone", "cli", "android"}))]


# ---- wiring (the Mac, the server and Android all use this) ------------------------------------------------
def attach(settings, router, tools: Dict[str, Any], *, request: Requester = http_json,
           clock: Callable[[], float] = time.time) -> Tuple[Weather, RecentPlace]:
    """Add the weather and watch_trip tools, and give the route tool the forecast and the phone's last place.
    A TomTom key (if any) also finds street addresses for the forecast."""
    from .maps import Place
    tomtom = router.services.get("tomtom")

    def by_address(text: str) -> Optional[Tuple[Point, str]]:
        return (tomtom.locate(Place.from_text(text)), text) if tomtom is not None else None
    weather = Weather(units=settings.maps_units, request=request, geocode=by_address if tomtom else None, clock=clock)
    recent = RecentPlace(clock=clock)
    router.weather, router.recent = weather, recent
    for tool in weather_tools(WeatherTools(weather, home=settings.city, recent=recent, clock=clock)):
        tools[tool.name] = tool
    return weather, recent


def make_trip_check(agent, store, user: str) -> Callable[[Dict[str, Any]], str]:
    """What the scheduler runs for a trip check: Claude with only the weather and route tools ("trip")."""
    from .agent import Events

    def check(trip: Dict[str, Any]) -> str:
        session = store.session_for("trip", "trips", 60)
        turn = agent.run(session_id=session, channel="trip", content=trip_request(trip, user or "the user"),
                         events=Events())
        return turn.text.strip() or (f"Trip check for {trip['to']}: the weather couldn't be checked "
                                     f"({turn.error or 'no answer'}).")
    return check
