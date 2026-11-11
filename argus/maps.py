"""Directions with live traffic, and links that start navigation in Apple Maps, Google Maps or Waze.

Where the traffic numbers come from ([maps] traffic = "auto" asks every service that is set up and compares
their answers; traffic.py lines them up, adds incidents and road news, and picks the best way):
* Google Routes API: needs a Google Maps API key (GOOGLE_MAPS_API_KEY in secrets.env). Live traffic, the
  usual time without traffic, and up to two other routes. A few thousand free requests a month.
* Apple Maps Server API: needs a Maps token from an Apple Developer account (APPLE_MAPS_TOKEN; make a
  long-lasting one with `argusctl maps-token`). Traffic-aware times and other routes; 25,000 free a day.
* Apple MapKit on the Mac: free, no account: the small argus-maps helper that setup.sh builds.
* TomTom: a free key (TOMTOM_API_KEY): a third opinion on the times, and the list of accidents, closures and
  road works on each route.
Waze has no public service to ask (and forbids reading its data automatically), so it is only an app the
navigation link can open. Without any of them Argus still makes the navigation link, and the maps app shows live traffic itself.

Your location is used only for the trip you asked about. It goes to the routing service, never into the
conversation with Claude: the tool says "your current location", and the navigation link leaves the start
empty, so the maps app starts from wherever you are.
"""
from __future__ import annotations

import base64
import concurrent.futures
import http.client
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit

from .llm import connect

if TYPE_CHECKING:
    from .toolbox import ToolOutput

log = logging.getLogger("argus.maps")

GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
APPLE_MAPS_URL = "https://maps-api.apple.com"
GOOGLE_FIELDS = ("routes.duration,routes.staticDuration,routes.distanceMeters,routes.description,"
                 "routes.routeLabels,routes.warnings,routes.polyline.encodedPolyline,"
                 "routes.legs.steps.navigationInstruction.instructions,routes.legs.steps.distanceMeters")
# mode -> (Google travelMode, Apple transportType, Apple Maps link dirflg, Google Maps link travelmode)
MODES: Dict[str, Tuple[str, str, str, str]] = {
    "drive": ("DRIVE", "Automobile", "d", "driving"),
    "walk": ("WALK", "Walking", "w", "walking"),
    "transit": ("TRANSIT", "Transit", "r", "transit"),
    "bike": ("BICYCLE", "Cycling", "", "bicycling"),
}
MODE_WORDS = {"drive": "driving", "walk": "walking", "transit": "by public transport", "bike": "cycling"}
AVOID = ("tolls", "highways", "ferries")
HERE_WORDS = frozenset({"", "here", "current location", "my location", "my current location", "where i am", "me"})
TIMEOUT = 15.0
LABELS = {"FUEL_EFFICIENT": "uses the least fuel", "SHORTER_DISTANCE": "shortest"}


class MapsError(RuntimeError):
    """A failure a person can act on: a refused key, no route, the service is down."""


Requester = Callable[..., Tuple[int, Any]]


def http_json(method: str, url: str, headers: Dict[str, str], body: Optional[Dict[str, Any]] = None,
              timeout: float = TIMEOUT) -> Tuple[int, Any]:
    """One HTTPS request with a JSON reply (and JSON body, if any). Returns (status, parsed reply or {})."""
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    data = json.dumps(body).encode() if body is not None else None
    sent = dict(headers, **{"Accept": "application/json", "User-Agent": "Argus/2"})
    if data is not None:
        sent["Content-Type"] = "application/json"
    conn = connect(url, timeout)
    try:
        conn.request(method, path, body=data, headers=sent)
        resp = conn.getresponse()
        status, raw = resp.status, resp.read(2_000_000)
    except (OSError, http.client.HTTPException) as exc:
        raise MapsError(f"couldn't reach {parts.hostname} ({exc or type(exc).__name__})") from exc
    finally:
        conn.close()
    try:
        return status, (json.loads(raw) if raw.strip() else {})
    except ValueError:
        return status, {"raw": raw[:300].decode("utf-8", "replace")}


# ---- places --------------------------------------------------------------------------------------------
_LATLON = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def parse_latlon(text: Any) -> Optional[Tuple[float, float]]:
    match = _LATLON.match(str(text or ""))
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def clean_location(raw: Any) -> Optional[Dict[str, float]]:
    """{"lat", "lon"[, "accuracy", "speed"]} from what a phone sent ({"lat":..,"lon":..}, {"latitude":..,
    "longitude":..}, or "45.52,-122.68"), or None when it isn't a usable place."""
    lat = lon = None
    extra: Dict[str, Any] = {}
    if isinstance(raw, str):
        found = parse_latlon(raw)
        if found:
            lat, lon = found
    elif isinstance(raw, dict):
        lat = raw.get("lat", raw.get("latitude"))
        lon = raw.get("lon", raw.get("lng", raw.get("longitude")))
        extra = {k: raw.get(k) for k in ("accuracy", "speed")}
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:          # what some phones report when they have no fix at all
        return None
    out: Dict[str, float] = {"lat": round(lat, 6), "lon": round(lon, 6)}
    for key, value in extra.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            out[key] = round(number, 1)
    return out


@dataclass(frozen=True)
class Place:
    text: str = ""                   # an address or a place name
    lat: Optional[float] = None
    lon: Optional[float] = None
    here: bool = False               # where the user is right now (never shown to Claude)

    @classmethod
    def from_text(cls, text: str) -> "Place":
        point = parse_latlon(text)
        return cls(text=text, lat=point[0], lon=point[1]) if point else cls(text=text)

    @classmethod
    def current(cls, location: Dict[str, float]) -> "Place":
        return cls(lat=location["lat"], lon=location["lon"], here=True)

    @property
    def has_point(self) -> bool:
        return self.lat is not None and self.lon is not None

    def query(self) -> str:
        return f"{self.lat:.6f},{self.lon:.6f}" if self.has_point else self.text

    def spoken(self) -> str:
        return "your current location" if self.here else (self.text or self.query())


@dataclass
class Step:
    text: str                            # "Turn right onto Main St"
    meters: Optional[int] = None


@dataclass
class Route:
    seconds: int                         # with traffic, when the service knows it
    usual_seconds: Optional[int] = None  # the same route without traffic
    meters: Optional[int] = None
    via: str = ""
    labels: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    path: List[Tuple[float, float]] = field(default_factory=list, repr=False)   # (lat, lon) along the route
    steps: List[Step] = field(default_factory=list, repr=False)
    source: str = ""                     # which service said so


APP_NAMES = {"apple": "Apple Maps", "google": "Google Maps", "waze": "Waze"}
_LINK_HOSTS = {"maps.apple.com": "Apple Maps", "www.google.com": "Google Maps", "waze.com": "Waze"}


def nav_link(app: str, origin: Place, destination: Place, mode: str = "drive") -> str:
    """A link that opens turn-by-turn directions. From "here", the start is left out: the maps app then
    starts from wherever the phone is (and the location never appears in the conversation)."""
    if app == "waze":                    # Waze only drives, and always starts from where the phone is
        query = {"ll": destination.query()} if destination.has_point else {"q": destination.query()}
        return "https://waze.com/ul?" + urlencode(dict(query, navigate="yes"))
    if app == "google":
        query = {"api": "1", "destination": destination.query(), "travelmode": MODES[mode][3]}
        if not origin.here:
            query["origin"] = origin.query()
        return "https://www.google.com/maps/dir/?" + urlencode(query)
    query = {"daddr": destination.query()}
    if not origin.here:
        query["saddr"] = origin.query()
    if MODES[mode][2]:
        query["dirflg"] = MODES[mode][2]
    return "https://maps.apple.com/?" + urlencode(query)


def nav_links(apps: Sequence[str], origin: Place, destination: Place, mode: str = "drive") -> List[Tuple[str, str]]:
    """(app name, link) for each app that can do this trip. Waze is left out unless it's a drive from here."""
    found = []
    for app in apps:
        if app == "waze" and (mode != "drive" or not origin.here):
            continue
        found.append((APP_NAMES[app], nav_link(app, origin, destination, mode)))
    return found


def link_name(url: str) -> str:
    """Which app a navigation link opens ("" when it isn't one of ours)."""
    return _LINK_HOSTS.get(urlsplit(str(url)).hostname or "", "")


def phone_links(links: Sequence[str]) -> Dict[str, Any]:
    """What the phone gets with a reply: the link to open, and the same trip in each of its navigation apps."""
    choices: Dict[str, str] = {}
    for url in links:
        name = link_name(url)
        if name:
            choices.setdefault(name, url)
    found: Dict[str, Any] = {"open": links[0] if links else None}
    if len(choices) > 1:
        found["links"] = choices
    return found


def minutes(seconds: float) -> str:
    total = max(1, int(round(seconds / 60)))
    if total < 60:
        return f"{total} min"
    hours, mins = divmod(total, 60)
    return f"{hours} h {mins} min" if mins else f"{hours} h"


def distance(meters: Optional[float], units: str = "imperial") -> str:
    if not meters:
        return ""
    if units == "metric":
        km = meters / 1000
        return f"{km:.1f} km" if km < 100 else f"{km:.0f} km"
    miles = meters / 1609.344
    return f"{miles:.1f} mi" if miles < 100 else f"{miles:.0f} mi"


def short_distance(meters: Optional[float], units: str = "imperial") -> str:
    """A step's length: feet or metres when it's short."""
    if not meters or meters < 15:
        return ""
    if units == "metric":
        return f"{int(round(meters / 10) * 10)} m" if meters < 1000 else distance(meters, units)
    return f"{int(max(1, round(meters * 3.28084 / 50)) * 50)} ft" if meters < 160 else distance(meters, units)


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(value: Any) -> Optional[int]:
    """Google writes durations as "1234s"; Apple as plain numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) and value >= 0 else None
    match = re.fullmatch(r"(\d+)(?:\.\d+)?s", str(value or "").strip())
    return int(match.group(1)) if match else None


def _whole(value: Any) -> Optional[int]:
    seconds = _seconds(value)
    return seconds if seconds else None


# ---- a route's shape on the map --------------------------------------------------------------------------
Point = Tuple[float, float]            # (latitude, longitude)
MAX_PATH_POINTS = 3000


def decode_polyline(text: str, precision: int = 5) -> List[Point]:
    """Google's encoded polyline -> points. Stops quietly at damaged input."""
    points: List[Point] = []
    scale = 10 ** precision
    lat = lon = at = 0
    data = str(text or "")
    while at < len(data):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                if at >= len(data):
                    return points
                byte = ord(data[at]) - 63
                at += 1
                if byte < 0:
                    return points
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat, lon = lat + deltas[0], lon + deltas[1]
        point = (lat / scale, lon / scale)
        if not (-90 <= point[0] <= 90 and -180 <= point[1] <= 180):
            return points
        points.append(point)
    return points


def clean_path(raw: Iterable[Any]) -> List[Point]:
    """Points from [lat, lon] pairs or {"latitude", "longitude"} objects; anything else is skipped."""
    points: List[Point] = []
    for item in raw or []:
        try:
            if isinstance(item, dict):
                lat, lon = float(item["latitude"]), float(item["longitude"])
            else:
                lat, lon = float(item[0]), float(item[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            points.append((lat, lon))
    return thin(points)


def thin(points: Sequence[Point], limit: int = MAX_PATH_POINTS) -> List[Point]:
    if len(points) <= limit:
        return list(points)
    stride = math.ceil(len(points) / limit)
    kept = list(points[::stride])
    if kept[-1] != points[-1]:
        kept.append(points[-1])
    return kept


# ---- the services ----------------------------------------------------------------------------------------
class GoogleRoutes:
    name = "Google Maps"

    def __init__(self, key: str, *, url: str = GOOGLE_ROUTES_URL, request: Requester = http_json,
                 units: str = "imperial", language: str = "en-US"):
        self.key, self.url, self.request, self.units, self.language = key, url, request, units, language

    @staticmethod
    def _waypoint(place: Place) -> Dict[str, Any]:
        if place.has_point:
            return {"location": {"latLng": {"latitude": place.lat, "longitude": place.lon}}}
        return {"address": place.text}

    def routes(self, origin: Place, destination: Place, mode: str = "drive", depart: Optional[float] = None,
               avoid: Sequence[str] = ()) -> List[Route]:
        travel = MODES[mode][0]
        body: Dict[str, Any] = {"origin": self._waypoint(origin), "destination": self._waypoint(destination),
                                "travelMode": travel, "languageCode": self.language,
                                "units": "METRIC" if self.units == "metric" else "IMPERIAL"}
        if travel == "DRIVE":
            body["routingPreference"] = "TRAFFIC_AWARE"
            body["computeAlternativeRoutes"] = True
            modifiers = {f"avoid{word.title()}": True for word in avoid if word in AVOID}
            if modifiers:
                body["routeModifiers"] = modifiers
        if depart:
            body["departureTime"] = _utc(depart)
        status, data = self.request("POST", self.url, {"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": GOOGLE_FIELDS},
                                    body)
        if status != 200:
            raise MapsError(self._why(status, data))
        found: List[Route] = []
        for raw in (data.get("routes") or [])[:3] if isinstance(data, dict) else []:
            seconds = _seconds(raw.get("duration"))
            if seconds is None:
                continue
            steps = []
            for leg in raw.get("legs") or []:
                for step in (leg.get("steps") or []) if isinstance(leg, dict) else []:
                    text = ((step.get("navigationInstruction") or {}).get("instructions") or "") if isinstance(step, dict) else ""
                    text = " ".join(str(text).split())[:160]
                    if text:
                        steps.append(Step(text, _whole(step.get("distanceMeters"))))
            found.append(Route(seconds=seconds, usual_seconds=_seconds(raw.get("staticDuration")),
                               meters=_whole(raw.get("distanceMeters")), via=str(raw.get("description") or "")[:80],
                               labels=[LABELS[l] for l in raw.get("routeLabels") or [] if l in LABELS],
                               notes=[str(w)[:120] for w in raw.get("warnings") or []][:3],
                               path=thin(decode_polyline(str((raw.get("polyline") or {}).get("encodedPolyline") or ""))),
                               steps=steps, source=self.name))
        return found

    @staticmethod
    def _why(status: int, data: Any) -> str:
        error = (data.get("error") or {}) if isinstance(data, dict) else {}
        message = " ".join(str(error.get("message") or "").split())[:200]
        if status in (401, 403):
            return f"Google refused the Maps key ({message or status}); check that the Routes API is enabled for it"
        if status == 400:
            return f"Google couldn't plan that trip ({message or 'check the addresses'})"
        if status == 429:
            return "Google Maps' request limit was reached; try again in a minute"
        return f"Google Maps answered HTTP {status}" + (f" ({message})" if message else "")


class AppleMapsServer:
    """Apple Maps Server API. The long-lived Maps auth token is swapped for a 30-minute access token."""
    name = "Apple Maps"

    def __init__(self, auth_token: str, *, base: str = APPLE_MAPS_URL, request: Requester = http_json,
                 clock: Callable[[], float] = time.time, language: str = "en-US"):
        self.auth, self.base, self.request, self.clock, self.language = auth_token, base.rstrip("/"), request, clock, language
        self._access, self._until = "", 0.0
        self._lock = threading.Lock()

    def access_token(self, fresh: bool = False) -> str:
        with self._lock:
            if not fresh and self._access and self.clock() < self._until - 60:
                return self._access
            status, data = self.request("GET", self.base + "/v1/token", {"Authorization": f"Bearer {self.auth}"})
            if status in (401, 403):
                raise MapsError("Apple Maps didn't accept the Maps token (tokens made in the developer portal "
                                "only last 7 days; make a long-lasting one with: argusctl maps-token)")
            if status != 200 or not isinstance(data, dict) or not data.get("accessToken"):
                raise MapsError(f"Apple Maps sign-in failed (HTTP {status})")
            try:
                lifetime = float(data.get("expiresInSeconds") or 1800)
            except (TypeError, ValueError):
                lifetime = 1800.0
            self._access, self._until = str(data["accessToken"]), self.clock() + lifetime
            return self._access

    def routes(self, origin: Place, destination: Place, mode: str = "drive", depart: Optional[float] = None,
               avoid: Sequence[str] = ()) -> List[Route]:
        params = {"origin": origin.query(), "destination": destination.query(), "transportType": MODES[mode][1],
                  "requestsAlternateRoutes": "true", "lang": self.language}
        if depart:
            params["departureDate"] = _utc(depart)
        if "tolls" in avoid:
            params["avoid"] = "Tolls"
        url = f"{self.base}/v1/directions?{urlencode(params)}"
        status, data = self.request("GET", url, {"Authorization": f"Bearer {self.access_token()}"})
        if status == 401:                      # the access token ran out early: one fresh try
            status, data = self.request("GET", url, {"Authorization": f"Bearer {self.access_token(fresh=True)}"})
        if status != 200:
            error = (data.get("error") or {}) if isinstance(data, dict) else {}
            message = " ".join(str(error.get("message") or "").split())[:200]
            raise MapsError(f"Apple Maps answered HTTP {status}" + (f" ({message})" if message else ""))
        found: List[Route] = []
        if not isinstance(data, dict):
            return found
        all_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        shapes = data.get("stepPaths") if isinstance(data.get("stepPaths"), list) else []
        for raw in (data.get("routes") or [])[:3]:
            seconds = _seconds(raw.get("durationSeconds", raw.get("expectedTravelTimeSeconds")))
            if seconds is None:
                continue
            steps, path = [], []
            for number in raw.get("stepIndexes") or []:
                step = all_steps[number] if isinstance(number, int) and 0 <= number < len(all_steps) else None
                if not isinstance(step, dict):
                    continue
                text = " ".join(str(step.get("instructions") or "").split())[:160]
                if text:
                    steps.append(Step(text, _whole(step.get("distanceMeters"))))
                shape = step.get("stepPathIndex")
                if isinstance(shape, int) and 0 <= shape < len(shapes) and isinstance(shapes[shape], list):
                    path.extend(clean_path(shapes[shape]))
            found.append(Route(seconds=seconds, usual_seconds=_seconds(raw.get("staticTravelTimeSeconds")),
                               meters=_whole(raw.get("distanceMeters")), via=str(raw.get("name") or "")[:80],
                               notes=["has tolls"] if raw.get("hasTolls") else [], path=thin(path), steps=steps,
                               source=self.name))
        return found


class MapKitHelper:
    """Apple Maps through MapKit on the Mac: free and needs no account. setup.sh builds the helper
    (macos/argus-maps.swift) into ~/.argus/bin."""
    name = "Apple Maps"

    def __init__(self, exe: Path, *, run: Callable[..., Any] = subprocess.run):
        self.exe, self.run = Path(exe).expanduser(), run

    def available(self) -> bool:
        return self.exe.is_file() and os.access(self.exe, os.X_OK)

    def routes(self, origin: Place, destination: Place, mode: str = "drive", depart: Optional[float] = None,
               avoid: Sequence[str] = ()) -> List[Route]:
        args = [str(self.exe), "--from", origin.query(), "--to", destination.query(), "--mode", mode]
        if depart:
            args += ["--depart", str(int(depart))]
        args += [f"--avoid-{word}" for word in avoid if word in ("tolls", "highways")]
        try:
            proc = self.run(args, capture_output=True, text=True, timeout=40)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MapsError(f"the Mac's maps helper didn't answer ({exc})") from exc
        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError:
            raise MapsError("the Mac's maps helper gave an answer Argus couldn't read") from None
        if not isinstance(data, dict):
            raise MapsError("the Mac's maps helper gave an answer Argus couldn't read")
        if data.get("error"):
            raise MapsError(" ".join(str(data["error"]).split())[:200])
        found: List[Route] = []
        for raw in data.get("routes") or []:
            seconds = _seconds(raw.get("seconds")) if isinstance(raw, dict) else None
            if seconds is not None:
                steps = [Step(" ".join(str(s.get("text") or "").split())[:160], _whole(s.get("meters")))
                         for s in raw.get("steps") or [] if isinstance(s, dict) and str(s.get("text") or "").strip()]
                found.append(Route(seconds=seconds, meters=_whole(raw.get("meters")), via=str(raw.get("name") or "")[:80],
                                   notes=[str(n)[:120] for n in raw.get("notes") or []][:3],
                                   path=clean_path(raw.get("path") or []), steps=steps, source=self.name))
        return found[:3]


# ---- the route tool ---------------------------------------------------------------------------------------
ROUTE_DESCRIPTION = (
    "The best way to drive, walk or ride somewhere: asks every maps service that is set up (Apple Maps, Google "
    "Maps, TomTom) for live-traffic times, lines their routes up, checks accidents, closures and road works on "
    "each one and recent news about those roads, and names the best route with the reason, its turn-by-turn "
    "steps, and a link that opens navigation in the user's maps app (the phone opens it by itself). Use it "
    "whenever the user is going somewhere, asks about traffic, how long it takes, or the best way. origin "
    "'here' is where the user's phone is right now, when the phone shared it.")
ROUTE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["destination"],
    "properties": {
        "destination": {"type": "string", "description": "a full address, or a place name with its city; for home "
                                                         "or work use the saved address (recall)"},
        "origin": {"type": "string", "description": "'here' (the default: where the phone is now), an address, "
                                                    "or 'lat,lon'"},
        "mode": {"type": "string", "enum": list(MODES)},
        "depart_at": {"type": "string", "description": "when to leave, as an ISO local date-time like "
                                                       "2026-11-10T17:30 (default: now)"},
        "avoid": {"type": "array", "items": {"type": "string", "enum": list(AVOID)}},
    },
}


@dataclass
class Comparison:
    """What the maps services said about one trip, lined up (see traffic.py)."""
    options: List[Any]                                   # traffic.Option, the best first
    answered: List[str] = field(default_factory=list)    # the services that gave routes
    failed: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    incidents_from: str = ""                             # "TomTom" when incidents were checked
    news_checked: bool = False


def _listed(words: Sequence[str]) -> str:
    words = list(words)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1] if words else ""


def brief_steps(steps: Sequence[Step], units: str = "imperial", limit: int = 12) -> List[str]:
    """Turn-by-turn steps, shortened for a long trip: the start, the longest stretches, and the end."""
    steps = [s for s in steps if s.text]
    keep = set(range(len(steps)))
    if len(steps) > limit:
        middle = sorted(range(2, len(steps) - 2), key=lambda i: -(steps[i].meters or 0))[:limit - 4]
        keep = {0, 1, len(steps) - 2, len(steps) - 1, *middle}
    lines, skipped = [], 0
    for i, step in enumerate(steps):
        if i not in keep:
            skipped += 1
            continue
        if skipped:
            lines.append(f"({skipped} shorter step{'s' if skipped > 1 else ''})")
            skipped = 0
        far = short_distance(step.meters, units)
        lines.append(f"{i + 1}) {step.text.rstrip('.')}" + (f" ({far})" if far else ""))
    return lines


def past_the_start(steps: Sequence[Step], meters: int = 250) -> List[Step]:
    """The steps after the first `meters` of the trip. From "here", the first turns name the street the user is
    on, which would put where they are into the conversation; the maps app shows those turns anyway."""
    gone = 0
    for i, step in enumerate(steps):
        if gone >= meters:
            return list(steps[i:])
        gone += step.meters or 0
    return list(steps[-1:])


def _offline_news() -> Optional[Callable[[str], bytes]]:
    """ARGUS_NEWS_OFFLINE=<saved feed> (the same switch the news tool has): read it instead of the web. For tests."""
    saved = os.environ.get("ARGUS_NEWS_OFFLINE", "").split(os.pathsep)[0]
    return (lambda url: Path(saved).read_bytes()) if saved else None


STAGE_SECONDS = 18.0            # how long the maps services get to answer, together
EXTRAS_SECONDS = 9.0            # and the incident and news checks after them
SWITCH_APP_SECONDS = 180        # open another navigation app only when the usual one's route is this much worse
SERVICE_NAMES = {"google": "Google Routes API", "apple": "Apple Maps Server API", "mapkit": "MapKit on this Mac",
                 "tomtom": "TomTom"}


class Router:
    def __init__(self, settings, secrets: Dict[str, str], *, request: Requester = http_json,
                 mapkit_exe: Optional[Path] = None, run: Callable[..., Any] = subprocess.run,
                 clock: Callable[[], float] = time.time, fetch: Optional[Callable[[str], bytes]] = None):
        from .traffic import TomTom
        self.settings = settings
        self.clock = clock
        self.fetch = fetch or _offline_news()
        self.services: Dict[str, Any] = {}
        if secrets.get("GOOGLE_MAPS_API_KEY"):
            self.services["google"] = GoogleRoutes(secrets["GOOGLE_MAPS_API_KEY"], request=request,
                                                   units=settings.maps_units)
        if secrets.get("APPLE_MAPS_TOKEN"):
            self.services["apple"] = AppleMapsServer(secrets["APPLE_MAPS_TOKEN"], request=request, clock=clock)
        helper = MapKitHelper(mapkit_exe or settings.bin_dir / "argus-maps", run=run)
        if helper.available():
            self.services["mapkit"] = helper
        if secrets.get("TOMTOM_API_KEY"):
            self.services["tomtom"] = TomTom(secrets["TOMTOM_API_KEY"], request=request)

    def sources(self, mode: str = "drive") -> List[Tuple[str, Any]]:
        """The services asked for a trip, the preferred one first. "auto" asks every one that is set up: Apple
        (its server API, or MapKit on this Mac: the same data, so only one of them), Google and TomTom."""
        choice = self.settings.maps_traffic
        if choice == "off":
            return []
        if choice != "auto":
            return [(choice, self.services[choice])] if choice in self.services else []
        apple = "apple" if "apple" in self.services else "mapkit"
        order = [apple, "google"] if self.settings.maps_link_app == "apple" else ["google", apple]
        return [(key, self.services[key]) for key in order + ["tomtom"]
                if key in self.services and not (key == "tomtom" and mode == "transit")]

    def service(self):
        """The preferred traffic service, or None (navigation links only)."""
        found = self.sources()
        return found[0][1] if found else None

    def describe(self) -> str:
        found = self.sources()
        if not found:
            return "no live-traffic service set up (navigation links only)"
        text = "live traffic from " + _listed([SERVICE_NAMES[key] for key, _ in found])
        return text + ("; incident reports from TomTom" if "tomtom" in self.services else "")

    @staticmethod
    def _together(jobs: Dict[str, Callable[[], Any]], seconds: float) -> Dict[str, Any]:
        """Run the jobs side by side. Each result is the job's answer, or the exception that stopped it."""
        def safely(job: Callable[[], Any]) -> Any:
            try:
                return job()
            except MapsError as exc:
                return exc
            except Exception as exc:                   # one service's surprise must not sink the others
                log.warning("a maps check failed: %s: %s", type(exc).__name__, exc)
                return MapsError("gave an answer Argus couldn't use")
        if len(jobs) <= 1:
            return {name: safely(job) for name, job in jobs.items()}
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="argus-maps")
        futures = {name: pool.submit(safely, job) for name, job in jobs.items()}
        concurrent.futures.wait(futures.values(), timeout=seconds)
        results = {name: (future.result() if future.done() else MapsError("didn't answer in time"))
                   for name, future in futures.items()}
        pool.shutdown(wait=False)
        return results

    def compare(self, origin: Place, destination: Place, mode: str = "drive", depart: Optional[float] = None,
                avoid: Sequence[str] = (), extras: bool = True) -> Comparison:
        """Ask every service, line their routes up, and (extras) check incidents and road news."""
        from . import traffic
        sources = self.sources(mode)
        if not sources:
            raise MapsError("no live-traffic service is set up")
        results = self._together({key: (lambda s=service: s.routes(origin, destination, mode, depart, avoid))
                                  for key, service in sources}, STAGE_SECONDS)
        answers: List[Tuple[str, List[Route]]] = []
        found = Comparison(options=[])
        for key, service in sources:
            outcome = results[key]
            if isinstance(outcome, Exception):
                found.failed[service.name] = str(outcome)
            elif outcome:
                answers.append((service.name, list(outcome)[:3]))
        if not answers:
            if len(found.failed) == 1:
                raise MapsError(next(iter(found.failed.values())))
            if found.failed:
                raise MapsError("; ".join(f"{name}: {why}" for name, why in found.failed.items()))
            return found
        reference = min(answers[0][1], key=lambda r: r.seconds)
        kept = [answers[0]]
        for name, routes in answers[1:]:          # an address two services read differently isn't one trip
            gap = traffic.end_gap(reference, routes[0])
            if gap > max(1500.0, min(4000.0, 0.03 * (reference.meters or 0))):
                found.notes.append(f"{name} put the destination {distance(gap, self.settings.maps_units)} from where "
                                   f"{answers[0][0]} did, so its routes were left out")
                continue
            kept.append((name, routes))
        found.answered = [name for name, _ in kept]
        options = traffic.rank(traffic.group(kept))[:4]
        live = mode == "drive" and (depart is None or depart - self.clock() < 45 * 60)
        if extras and live:
            jobs: Dict[str, Callable[[], Any]] = {}
            shapes = [option.index.points for option in options if option.index is not None]
            tomtom = self.services.get("tomtom")
            if tomtom is not None and shapes:
                jobs["incidents"] = lambda: tomtom.incidents(shapes)
            roads = traffic.news_roads([route for option in options for route in option.routes])
            if self.settings.maps_news and roads:
                jobs["news"] = lambda: traffic.road_news(roads, fetch=self.fetch, now=self.clock())
            checked = self._together(jobs, EXTRAS_SECONDS)
            incidents, headlines = [], []
            if isinstance(checked.get("incidents"), tuple):
                incidents, cut_short = checked["incidents"]
                found.incidents_from = tomtom.name
                if cut_short:
                    found.notes.append("incidents were checked for the first part of the trip only (it's a long one)")
            elif "incidents" in checked:
                found.notes.append(f"incidents couldn't be checked ({checked['incidents']})")
            if isinstance(checked.get("news"), list):
                headlines, found.news_checked = checked["news"], True
            traffic.attach(options, incidents, headlines)
        found.options = traffic.rank(options)[:3]
        return found

    def plan(self, origin: Place, destination: Place, mode: str = "drive", depart: Optional[float] = None,
             avoid: Sequence[str] = ()) -> Tuple[str, List[Route]]:
        """The quick check (times only): (who answered, one Route per way to go, the best first)."""
        found = self.compare(origin, destination, mode, depart, avoid, extras=False)
        return " and ".join(found.answered), [option.as_route() for option in found.options]

    @staticmethod
    def _why(options: Sequence[Any]) -> str:
        if len(options) < 2:
            return ""
        best = options[0]
        quickest = min(options, key=lambda o: o.seconds)
        if quickest is not best:
            reasons = []
            if quickest.reported:
                reasons.append(f"it has {_listed(quickest.reported)} reported on it")
            if quickest.asked > 1 and len(quickest.routes) == 1:
                reasons.append(f"only {quickest.sources[0]} suggests it")
            return (f"Why: route {options.index(quickest) + 1} looks {minutes(best.seconds - quickest.seconds)} quicker, "
                    f"but {' and '.join(reasons) or 'it is less certain'}, so route 1 is the safer choice.")
        saves = min(o.seconds for o in options[1:]) - best.seconds
        text = (f"Why: route 1 is {minutes(saves)} faster than the next best" if saves >= 60
                else "Why: the top routes take about the same time")
        if len(best.routes) > 1:
            text += (f", and {len(best.routes)} services agree on it" +
                     (f" within {minutes(best.spread)}" if best.spread >= 60 else ""))
        return text + "."

    def report(self, found: Comparison, origin: Place, destination: Place, mode: str, depart: Optional[float]) -> str:
        units = self.settings.maps_units
        when = "leaving now"
        if depart:
            when = "leaving " + datetime.fromtimestamp(depart).strftime("%a %d %b %H:%M")
        checked = f"Checked: {_listed(found.answered)}" + (" (live traffic)" if mode == "drive" else "")
        if found.incidents_from:
            checked += f"; incidents from {found.incidents_from}"
        if found.news_checked:
            checked += "; road news"
        lines = [f"{MODE_WORDS[mode].capitalize()} from {origin.spoken()} to {destination.spoken()}, {when}. {checked}."]
        several = len(found.answered) > 1
        for i, option in enumerate(found.options, 1):
            line = (f"{i}. {'BEST: ' if i == 1 and len(found.options) > 1 else ''}"
                    f"{'via ' + option.via if option.via else 'route ' + str(i)}: {minutes(option.seconds)}")
            if several:
                line += " (" + ", ".join(f"{r.source} {minutes(r.seconds)}" for r in option.routes) + ")"
            if option.meters:
                line += ", " + distance(option.meters, units)
            route = option.as_route()
            extra = []
            if option.usual_seconds and option.seconds - option.usual_seconds >= 120:
                extra.append(f"{minutes(option.seconds - option.usual_seconds)} slower than usual because of traffic")
            extra += route.labels + [note.rstrip(".") for note in route.notes]
            lines.append(line + "." + "".join(f" {e[:1].upper()}{e[1:]}." for e in extra))
            shown = [i_ for i_ in option.incidents if i_.allowance or (i_.delay or 0) >= 180][:4]
            for incident in shown:
                delay = f", {minutes(incident.delay)} delay" if incident.delay and incident.delay >= 60 else ""
                lines.append(f"   - {incident.text}{delay} ({incident.source})")
            for headline in option.headlines[:2]:
                age = "" if headline.hours is None else (", under an hour ago" if headline.hours < 1
                                                         else f", {int(headline.hours)} h ago")
                lines.append(f"   - In the news (unverified, may be about another area): \"{headline.title}\" "
                             f"({headline.source}{age})")
        why = self._why(found.options)
        if why:
            lines.append(why)
        for name, reason in found.failed.items():
            lines.append(f"Not included: {name} ({reason}).")
        lines += [f"Note: {note}." for note in found.notes]
        if found.options:
            source, steps = found.options[0].steps
            after = ""
            if origin.here and steps:
                steps, after = past_the_start(steps), ", after the first turns from where the user is"
            if steps:
                lines.append(f"Turn by turn for route 1 ({source}{after}): " +
                             "; ".join(brief_steps(steps, units)) + ".")
        return "\n".join(lines)

    @staticmethod
    def best_app_first(apps: List[Tuple[str, str]], options: Sequence[Any]) -> Tuple[List[Tuple[str, str]], str]:
        """A maps app takes its own first choice when it opens. When the usual app's first choice is a clearly
        worse way than the best one, and another app on the phone would take the best one, that app goes first.
        Returns (apps, a sentence saying so or "")."""
        def own_choice(app: str) -> Optional[int]:
            mine = [(route.seconds, n) for n, option in enumerate(options) for route in option.routes
                    if route.source == app]
            return min(mine)[1] if mine else None

        usual = own_choice(apps[0][0]) if len(apps) > 1 and len(options) > 1 else None
        if not usual or options[usual].score - options[0].score < SWITCH_APP_SECONDS:
            return apps, ""
        for i, (name, _) in enumerate(apps[1:], 1):
            if own_choice(name) == 0:
                return ([apps[i]] + apps[:i] + apps[i + 1:],
                        f"Opening {name} this time, not {apps[0][0]}: route 1 is {name}' own first choice, while "
                        f"{apps[0][0]} would take route {usual + 1}.")
        return apps, ""

    def links(self, origin: Place, destination: Place, mode: str) -> List[Tuple[str, str]]:
        """(app name, link) for each navigation app on the phone, the preferred one first."""
        found = nav_links(self.settings.maps_nav_apps, origin, destination, mode)
        if not found:                                # only Waze, and this isn't a drive from here
            fallback = "apple" if self.settings.phone_type == "iphone" else "google"
            found = nav_links([fallback], origin, destination, mode)
        return found

    def tool(self, args: Dict[str, Any], ctx) -> "ToolOutput":
        from .toolbox import ToolOutput, parse_when
        destination_text = " ".join(str(args.get("destination") or "").split())[:200]
        if not destination_text:
            return ToolOutput.error("say where to")
        mode = str(args.get("mode") or "drive")
        if mode not in MODES:
            return ToolOutput.error(f"mode must be one of {', '.join(MODES)}")
        avoid = args.get("avoid") or []
        if not isinstance(avoid, list) or any(a not in AVOID for a in avoid):
            return ToolOutput.error(f"avoid takes a list of: {', '.join(AVOID)}")
        origin_text = " ".join(str(args.get("origin") or "").split())[:200]
        if origin_text.lower() in HERE_WORDS:
            location = getattr(ctx, "location", None)
            locate = getattr(ctx, "locate", None)
            if not location and locate is not None:
                try:
                    location = locate()
                except Exception as exc:          # GPS off, permission missing: say so, don't crash
                    log.info("couldn't get the phone's location: %s", exc)
                    location = None
            if not location:
                return ToolOutput.error("The user's current location isn't available here (only their phone shares "
                                        "it, with a request). Ask where they're starting from, or use their saved "
                                        "home address.")
            origin = Place.current(location)
        else:
            origin = Place.from_text(origin_text)
        destination = Place.from_text(destination_text)
        depart = None
        if args.get("depart_at"):
            try:
                depart = parse_when(str(args["depart_at"]), getattr(ctx, "timezone", None))
            except ValueError:
                return ToolOutput.error("give depart_at as an ISO local date-time like 2026-11-10T17:30")
            now = self.clock()
            if depart < now - 120:
                return ToolOutput.error("that departure time has already passed")
            if depart <= now + 120:
                depart = None
        apps = self.links(origin, destination, mode)
        app, link = apps[0]
        seen = getattr(ctx, "links", None)

        def share(found_apps: List[Tuple[str, str]]) -> None:
            if isinstance(seen, list):
                seen.extend(url for _, url in found_apps)

        if not self.sources(mode):
            share(apps)
            return ToolOutput(f"No live-traffic service is set up, so there are no times. The navigation link opens "
                              f"{app}, which shows live traffic itself: {link}")
        try:
            found = self.compare(origin, destination, mode, depart, avoid)
        except MapsError as exc:
            share(apps)
            return ToolOutput.error(f"Couldn't get traffic times: {exc}. The navigation link for {app} still works: {link}")
        if not found.options:
            share(apps)
            return ToolOutput.error(f"No {MODE_WORDS[mode]} route was found from {origin.spoken()} to "
                                    f"{destination.spoken()}. Check the address with the user.")
        switched = ""
        if mode == "drive":
            apps, switched = self.best_app_first(apps, found.options)
            app, link = apps[0]
        share(apps)
        ending = (f"\n{switched}" if switched else "") + f"\nNavigation link ({app}): {link}"
        if len(apps) > 1:
            ending += "\nThe phone can also open it in: " + "; ".join(f"{name}: {url}" for name, url in apps[1:])
        best = found.options[0]
        if mode == "drive" and len(found.options) > 1 and best.via:
            ending += (f"\nThe maps app picks its own route when it opens. If it shows a different one, the user "
                       f"should pick the one via {best.via}.")
        return ToolOutput(self.report(found, origin, destination, mode, depart) + ending)

# ---- a long-lasting Apple Maps token (argusctl maps-token) -------------------------------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def der_to_raw(signature: bytes, size: int = 32) -> bytes:
    """An ECDSA signature as OpenSSL writes it (DER: SEQUENCE of two INTEGERs) -> r||s, as JWTs need it."""
    def length(data: bytes, at: int) -> Tuple[int, int]:
        first = data[at]
        if first < 0x80:
            return first, at + 1
        count = first & 0x7F
        return int.from_bytes(data[at + 1:at + 1 + count], "big"), at + 1 + count

    if len(signature) < 8 or signature[0] != 0x30:
        raise ValueError("not a DER signature")
    _, at = length(signature, 1)
    parts = []
    for _ in range(2):
        if signature[at] != 0x02:
            raise ValueError("not a DER signature")
        n, at = length(signature, at + 1)
        value = signature[at:at + n].lstrip(b"\x00")
        if len(value) > size:
            raise ValueError("signature number too long")
        parts.append(value.rjust(size, b"\x00"))
        at += n
    return parts[0] + parts[1]


def make_apple_maps_token(key_file: Path, key_id: str, team_id: str, days: int = 365, *,
                          run: Callable[..., Any] = subprocess.run, now: Optional[float] = None) -> str:
    """A Maps auth token (a JWT signed with your Maps private key, ES256) that lasts `days`. The private key is
    read only by OpenSSL; it isn't copied anywhere."""
    if not re.fullmatch(r"[A-Z0-9]{10}", key_id or "") or not re.fullmatch(r"[A-Z0-9]{10}", team_id or ""):
        raise ValueError("the Key ID and Team ID are 10 letters and digits each (from your Apple Developer account)")
    if not 1 <= days <= 3650:
        raise ValueError("days must be between 1 and 3650")
    key_file = Path(key_file).expanduser()
    if not key_file.is_file():
        raise ValueError(f"no such key file: {key_file}")
    issued = int(now if now is not None else time.time())
    header = _b64url(json.dumps({"alg": "ES256", "kid": key_id, "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps({"iss": team_id, "iat": issued, "exp": issued + days * 86400},
                                separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    proc = run(["openssl", "dgst", "-sha256", "-sign", str(key_file)], input=signing_input, capture_output=True,
               timeout=30)
    if proc.returncode != 0 or not proc.stdout:
        raise ValueError("OpenSSL couldn't sign with that key (is it the .p8 file from Apple?)")
    return f"{header}.{claims}.{_b64url(der_to_raw(proc.stdout))}"


def route_tool(router: Router):
    """The route tool, for any kind of Argus (the Mac, the server, Android)."""
    from .toolbox import Tool
    return Tool("route", ROUTE_DESCRIPTION, ROUTE_SCHEMA, router.tool,
                frozenset({"voice", "phone", "scheduler", "cli", "android"}))
