"""Directions with live traffic, and a link that starts navigation in Apple Maps or Google Maps.

Where the traffic numbers come from ([maps] traffic = "auto" picks the same maps company as your navigation
links when it's set up: Apple for an iPhone, Google for Android; otherwise whichever is set up):
* Google Routes API: needs a Google Maps API key (GOOGLE_MAPS_API_KEY in secrets.env). Live traffic, the
  usual time without traffic, and up to two other routes. A few thousand free requests a month.
* Apple Maps Server API: needs a Maps token from an Apple Developer account (APPLE_MAPS_TOKEN; make a
  long-lasting one with `argusctl maps-token`). Traffic-aware times and other routes; 25,000 free a day.
* Apple MapKit on the Mac: free, no account: the small argus-maps helper that setup.sh builds.
Without any of them Argus still makes the navigation link, and the maps app shows live traffic itself.

Your location is used only for the trip you asked about. It goes to the routing service, never into the
conversation with Claude: the tool says "your current location", and the navigation link leaves the start
empty, so the maps app starts from wherever you are.
"""
from __future__ import annotations

import base64
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
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit

from .llm import connect

if TYPE_CHECKING:
    from .toolbox import ToolOutput

log = logging.getLogger("argus.maps")

GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
APPLE_MAPS_URL = "https://maps-api.apple.com"
GOOGLE_FIELDS = ("routes.duration,routes.staticDuration,routes.distanceMeters,routes.description,"
                 "routes.routeLabels,routes.warnings")
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
class Route:
    seconds: int                         # with traffic, when the service knows it
    usual_seconds: Optional[int] = None  # the same route without traffic
    meters: Optional[int] = None
    via: str = ""
    labels: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def nav_link(app: str, origin: Place, destination: Place, mode: str = "drive") -> str:
    """A link that opens turn-by-turn directions. From "here", the start is left out: the maps app then
    starts from wherever the phone is (and the location never appears in the conversation)."""
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
            found.append(Route(seconds=seconds, usual_seconds=_seconds(raw.get("staticDuration")),
                               meters=_whole(raw.get("distanceMeters")), via=str(raw.get("description") or "")[:80],
                               labels=[LABELS[l] for l in raw.get("routeLabels") or [] if l in LABELS],
                               notes=[str(w)[:120] for w in raw.get("warnings") or []][:3]))
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
        for raw in (data.get("routes") or [])[:3] if isinstance(data, dict) else []:
            seconds = _seconds(raw.get("durationSeconds", raw.get("expectedTravelTimeSeconds")))
            if seconds is None:
                continue
            found.append(Route(seconds=seconds, usual_seconds=_seconds(raw.get("staticTravelTimeSeconds")),
                               meters=_whole(raw.get("distanceMeters")), via=str(raw.get("name") or "")[:80],
                               notes=["has tolls"] if raw.get("hasTolls") else []))
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
                found.append(Route(seconds=seconds, meters=_whole(raw.get("meters")), via=str(raw.get("name") or "")[:80],
                                   notes=[str(n)[:120] for n in raw.get("notes") or []][:3]))
        return found[:3]


# ---- the route tool ---------------------------------------------------------------------------------------
ROUTE_DESCRIPTION = (
    "Directions with live traffic: how long a trip takes right now (or when leaving at a set time), up to three "
    "routes with the fastest first, and a link that opens navigation in the user's maps app (the phone opens it "
    "by itself). Use it whenever the user is going somewhere, asks about traffic, how long it takes, or the best "
    "way. origin 'here' is where the user's phone is right now, when the phone shared it.")
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


class Router:
    def __init__(self, settings, secrets: Dict[str, str], *, request: Requester = http_json,
                 mapkit_exe: Optional[Path] = None, run: Callable[..., Any] = subprocess.run,
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self.clock = clock
        self.services: Dict[str, Any] = {}
        if secrets.get("GOOGLE_MAPS_API_KEY"):
            self.services["google"] = GoogleRoutes(secrets["GOOGLE_MAPS_API_KEY"], request=request,
                                                   units=settings.maps_units)
        if secrets.get("APPLE_MAPS_TOKEN"):
            self.services["apple"] = AppleMapsServer(secrets["APPLE_MAPS_TOKEN"], request=request, clock=clock)
        helper = MapKitHelper(mapkit_exe or settings.bin_dir / "argus-maps", run=run)
        if helper.available():
            self.services["mapkit"] = helper

    def service(self):
        """The traffic service in use, or None (navigation links only)."""
        choice = self.settings.maps_traffic
        if choice == "off":
            return None
        if choice != "auto":
            return self.services.get(choice)
        order = ("apple", "mapkit", "google") if self.settings.maps_link_app == "apple" else ("google", "apple", "mapkit")
        return next((self.services[name] for name in order if name in self.services), None)

    def describe(self) -> str:
        service = self.service()
        if service is None:
            return "no live-traffic service set up (navigation links only)"
        where = {"google": "Google Routes API", "apple": "Apple Maps Server API", "mapkit": "MapKit on this Mac"}
        name = next(n for n, s in self.services.items() if s is service)
        return f"live traffic from {where[name]}"

    def plan(self, origin: Place, destination: Place, mode: str = "drive", depart: Optional[float] = None,
             avoid: Sequence[str] = ()) -> Tuple[str, List[Route]]:
        service = self.service()
        if service is None:
            raise MapsError("no live-traffic service is set up")
        routes = sorted(service.routes(origin, destination, mode, depart, avoid), key=lambda r: r.seconds)
        return service.name, routes

    def report(self, name: str, routes: List[Route], origin: Place, destination: Place, mode: str,
               depart: Optional[float]) -> str:
        when = "leaving now"
        if depart:
            when = "leaving " + datetime.fromtimestamp(depart).strftime("%a %d %b %H:%M")
        lines = [f"{MODE_WORDS[mode].capitalize()} from {origin.spoken()} to {destination.spoken()}, {when} "
                 f"({name}{', with live traffic' if mode == 'drive' else ''}):"]
        for i, route in enumerate(routes, 1):
            parts = [f"{i}. {'via ' + route.via if route.via else 'route ' + str(i)}: {minutes(route.seconds)}"]
            if route.usual_seconds and route.seconds - route.usual_seconds >= 120:
                parts.append(f"({minutes(route.seconds - route.usual_seconds)} slower than usual because of traffic)")
            if route.meters:
                parts.append(distance(route.meters, self.settings.maps_units))
            extras = "; ".join((["fastest"] if i == 1 and len(routes) > 1 else []) + route.labels +
                               [note.rstrip(".") for note in route.notes])
            lines.append(" ".join(parts) + (f". {extras[:1].upper()}{extras[1:]}." if extras else "."))
        return "\n".join(lines)

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
        link = nav_link(self.settings.maps_link_app, origin, destination, mode)
        links = getattr(ctx, "links", None)
        if isinstance(links, list):
            links.append(link)
        app = "Apple Maps" if self.settings.maps_link_app == "apple" else "Google Maps"
        if self.service() is None:
            return ToolOutput(f"No live-traffic service is set up, so there are no times. The navigation link opens "
                              f"{app}, which shows live traffic itself: {link}")
        try:
            name, routes = self.plan(origin, destination, mode, depart, avoid)
        except MapsError as exc:
            return ToolOutput.error(f"Couldn't get traffic times: {exc}. The navigation link for {app} still works: {link}")
        if not routes:
            return ToolOutput.error(f"No {MODE_WORDS[mode]} route was found from {origin.spoken()} to "
                                    f"{destination.spoken()}. Check the address with the user.")
        return ToolOutput(self.report(name, routes, origin, destination, mode, depart) +
                          f"\nNavigation link ({app}): {link}")


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
