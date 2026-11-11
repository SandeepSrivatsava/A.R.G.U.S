"""The best way there, checked against more than one maps service.

maps.py asks each maps service for its routes. This module lines those answers up and looks at what's in the way:

* The same road suggested by two services is one way to go, not two: routes are matched by where they actually
  run (their shape on the map), so "I-880 S" from Apple and "I-880 S and CA-237 W" from Google count as one,
  and each way gets every service's time for it.
* TomTom (a free key, TOMTOM_API_KEY) is a third opinion on the times, and the one service with a public list
  of incidents: accidents, closures, road works and jams, matched to each way by position and direction.
* News headlines from the last day about the roads on the way (Google News search). Unverified: they're
  shown as headlines, and never change the ranking.
* Ranking: the middle of the services' live times, plus a small allowance for reported incidents (traffic
  around a fresh accident usually gets worse before the maps catch up) and for a way only one service suggests.

Waze isn't here on purpose: it has no public routing or traffic service, and its terms forbid reading its data
automatically. Waze is one of the apps the navigation link can open (maps.nav_link), and its drivers' reports
also show up in Google Maps.
"""
from __future__ import annotations

import bisect
import logging
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, quote_plus, urlencode

from .maps import (AVOID, MODES, MapsError, Place, Point, Requester, Route, Step, _seconds, _utc, _whole,
                   clean_path, decode_polyline, http_json, thin)

log = logging.getLogger("argus.traffic")

TOMTOM_URL = "https://api.tomtom.com"


# ---- shapes on the map (reading them is in maps.py) --------------------------------------------------------
def _offset(origin: Point, point: Point) -> Tuple[float, float]:
    """Metres east and north of `origin` (flat-earth maths: fine over the few km this is used for)."""
    return ((point[1] - origin[1]) * 111_320.0 * math.cos(math.radians(origin[0])),
            (point[0] - origin[0]) * 110_540.0)


def meters_between(a: Point, b: Point) -> float:
    x, y = _offset(a, b)
    return math.hypot(x, y)


def segment_distance(point: Point, a: Point, b: Point) -> float:
    ax, ay = _offset(point, a)
    bx, by = _offset(point, b)
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
    return math.hypot(ax + t * dx, ay + t * dy)


def bearing(a: Point, b: Point) -> float:
    x, y = _offset(a, b)
    return math.degrees(math.atan2(x, y)) % 360


def angle_between(one: float, other: float) -> float:
    return abs((one - other + 180) % 360 - 180)


class PathIndex:
    """A route's shape, arranged so "how far is this point from the route?" is quick to answer."""
    CELL = 0.005                    # degrees: about 550 m

    def __init__(self, path: Sequence[Point]):
        points: List[Point] = []
        for point in thin(path):
            if points:
                gap = meters_between(points[-1], point)
                if gap < 1:
                    continue
                pieces = math.ceil(gap / 1000)          # no segment longer than 1 km
                last = points[-1]
                for k in range(1, pieces):
                    points.append((last[0] + (point[0] - last[0]) * k / pieces,
                                   last[1] + (point[1] - last[1]) * k / pieces))
            points.append(point)
        self.points = points
        self.along = [0.0]
        self.cells: Dict[Tuple[int, int], List[int]] = {}
        for i in range(len(points) - 1):
            a, b = points[i], points[i + 1]
            self.along.append(self.along[-1] + meters_between(a, b))
            for y in range(math.floor(min(a[0], b[0]) / self.CELL), math.floor(max(a[0], b[0]) / self.CELL) + 1):
                for x in range(math.floor(min(a[1], b[1]) / self.CELL), math.floor(max(a[1], b[1]) / self.CELL) + 1):
                    self.cells.setdefault((y, x), []).append(i)
        self.length = self.along[-1]

    def nearest(self, point: Point, within: float) -> Optional[Tuple[float, int]]:
        """(metres, segment number) of the closest stretch within `within` metres, or None."""
        if len(self.points) < 2:
            return None
        ry = int(within / (self.CELL * 110_540.0)) + 1
        rx = int(within / (self.CELL * 111_320.0 * max(0.1, math.cos(math.radians(point[0]))))) + 1
        cy, cx = math.floor(point[0] / self.CELL), math.floor(point[1] / self.CELL)
        seen = set()
        best: Optional[Tuple[float, int]] = None
        for y in range(cy - ry, cy + ry + 1):
            for x in range(cx - rx, cx + rx + 1):
                for i in self.cells.get((y, x), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    away = segment_distance(point, self.points[i], self.points[i + 1])
                    if away <= within and (best is None or away < best[0]):
                        best = (away, i)
        return best

    def samples(self, count: int) -> List[Point]:
        """`count` points spread evenly along the route."""
        if len(self.points) < 2:
            return list(self.points)
        found = []
        for k in range(count):
            target = self.length * k / (count - 1)
            i = min(len(self.points) - 2, max(0, bisect.bisect_right(self.along, target) - 1))
            span = self.along[i + 1] - self.along[i]
            t = 0.0 if span <= 0 else (target - self.along[i]) / span
            a, b = self.points[i], self.points[i + 1]
            found.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        return found

    def share_near(self, other: "PathIndex", within: float, count: int = 30) -> float:
        points = self.samples(count)
        return sum(1 for p in points if other.nearest(p, within)) / len(points) if points else 0.0

    def heading(self, segment: int) -> float:
        return bearing(self.points[segment], self.points[segment + 1])


def same_way(a: PathIndex, b: PathIndex) -> bool:
    """Do two routes run along the same roads (at least 85% of each lies on the other)?"""
    if a.length <= 0 or b.length <= 0:
        return False
    within = min(5000.0, max(250.0, 0.01 * min(a.length, b.length)))
    return min(a.share_near(b, within), b.share_near(a, within)) >= 0.85


# ---- road names -----------------------------------------------------------------------------------------
_NUMBERED = re.compile(r"\b(?:(?:I|US|SR|[A-Z]{2})[- ]\d{1,3}[A-Z]?|(?:Interstate|Highway|Hwy|Route|Rte)\s+\d{1,3})\b")
_COMPASS = re.compile(r"\s+(?:N|S|E|W|NB|SB|EB|WB|North|South|East|West)(?:bound)?$", re.I)
_BIG_ROAD = re.compile(r"\b(?:Bridge|Tunnel|Fwy|Freeway|Pkwy|Parkway|Expy|Expressway|Tpke|Turnpike|Thruway)\b", re.I)


def road_names(route: Route, limit: int = 4) -> List[str]:
    """The main roads of a route, like ["I-880", "CA-237"], from its name, then from its directions."""
    names: List[str] = []

    def add(name: str) -> None:
        name = _COMPASS.sub("", " ".join(name.split())).strip(" .,;")
        if len(name) >= 3 and name.lower() not in (n.lower() for n in names):
            names.append(name[:40])

    for part in re.split(r"\s+and\s+|[,;/&]", route.via or ""):
        add(part)
    if len(names) < limit:
        ranked: Dict[str, int] = {}
        for step in route.steps:
            for found in _NUMBERED.findall(step.text):
                ranked[found] = ranked.get(found, 0) + (step.meters or 0)
        for name in sorted(ranked, key=lambda n: -ranked[n]):
            add(name)
    return names[:limit]


def road_number(name: str) -> str:
    match = re.search(r"\d{1,3}", name) if _NUMBERED.search(name) else None
    return match.group(0) if match else ""


def share_a_road(a: Route, b: Route) -> bool:
    """A fallback for routes that came without a shape: same main road and about the same length."""
    def keys(route: Route) -> set:
        return {road_number(n) or n.lower() for n in road_names(route)}
    if not a.meters or not b.meters or abs(a.meters - b.meters) > 0.06 * max(a.meters, b.meters):
        return False
    return bool(keys(a) & keys(b))


# ---- TomTom: a third opinion on the times, and the incidents ---------------------------------------------
# iconCategory -> (what it is, seconds of allowance when ranking)
INCIDENT_KINDS: Dict[int, Tuple[str, int]] = {
    1: ("accident", 240), 2: ("fog", 120), 3: ("dangerous conditions", 120), 5: ("ice", 180), 6: ("traffic jam", 0),
    7: ("lane closed", 60), 8: ("road closed", 480), 9: ("road works", 60), 11: ("flooding", 180),
    14: ("broken-down vehicle", 60),
}
PHRASES = {"accident": "an accident", "road closed": "a closure", "lane closed": "a closed lane",
           "broken-down vehicle": "a broken-down vehicle"}
MAX_ALLOWANCE = 720
INCIDENT_FIELDS = ("{incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,"
                   "events{description,code,iconCategory},from,to,length,delay,roadNumbers}}}")
MAX_BOXES = 10


@dataclass
class Incident:
    kind: str
    text: str
    delay: Optional[int] = None            # seconds, when the service says
    allowance: int = 0
    points: List[Point] = field(default_factory=list, repr=False)
    ident: str = ""
    source: str = "TomTom"


def boxes_along(paths: Sequence[Sequence[Point]], limit: int = MAX_BOXES) -> Tuple[List[Tuple[float, float, float, float]], bool]:
    """Small boxes (min_lon, min_lat, max_lon, max_lat) that follow the routes, for asking about incidents
    without fetching a whole region. Returns (boxes, True if the routes were too long to cover fully)."""
    boxes: List[Tuple[float, float, float, float]] = []
    pad = 0.004
    cut_short = False

    def close(bounds: List[float]) -> None:
        nonlocal cut_short
        box = (round(bounds[1] - pad, 5), round(bounds[0] - pad, 5), round(bounds[3] + pad, 5), round(bounds[2] + pad, 5))
        slack = pad / 2                                 # a box that adds next to nothing isn't worth a request
        if any(b[0] <= box[0] + slack and b[1] <= box[1] + slack and b[2] >= box[2] - slack and b[3] >= box[3] - slack
               for b in boxes):
            return
        if len(boxes) >= limit:
            cut_short = True
            return
        boxes.append(box)

    for path in paths:
        bounds: Optional[List[float]] = None           # min_lat, min_lon, max_lat, max_lon
        previous: Optional[Point] = None
        for lat, lon in path:
            if bounds is None:
                bounds = [lat, lon, lat, lon]
            else:
                grown = [min(bounds[0], lat), min(bounds[1], lon), max(bounds[2], lat), max(bounds[3], lon)]
                if grown[2] - grown[0] <= 0.2 and grown[3] - grown[1] <= 0.25:
                    bounds = grown
                else:
                    close(bounds)
                    start = previous or (lat, lon)
                    bounds = [min(start[0], lat), min(start[1], lon), max(start[0], lat), max(start[1], lon)]
            previous = (lat, lon)
        if bounds is not None:
            close(bounds)
    return boxes, cut_short


class TomTom:
    name = "TomTom"

    def __init__(self, key: str, *, base: str = TOMTOM_URL, request: Requester = http_json, language: str = "en-US"):
        self.key, self.base, self.request, self.language = key, base.rstrip("/"), request, language

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        query = urlencode(dict(params, key=self.key), doseq=True)
        status, data = self.request("GET", f"{self.base}{path}?{query}", {})
        if status == 200 and isinstance(data, dict):
            return data
        detail = ""
        if isinstance(data, dict):
            error = data.get("detailedError") or data.get("error") or {}
            detail = error.get("message", "") if isinstance(error, dict) else str(error)
            detail = " ".join(str(detail or data.get("errorText") or "").split())[:200].replace(self.key, "...")
        if status in (401, 403):
            raise MapsError("TomTom refused the key (check TOMTOM_API_KEY, and that the Routing and Traffic APIs "
                            "are switched on for it)")
        if status == 429:
            raise MapsError("TomTom's free daily limit was reached")
        raise MapsError(f"TomTom answered HTTP {status}" + (f" ({detail})" if detail else ""))

    def locate(self, place: Place, near: Optional[Place] = None) -> Point:
        if place.has_point:
            return (place.lat, place.lon)                                    # type: ignore[return-value]
        params: Dict[str, Any] = {"limit": 1}
        if near is not None and near.has_point:
            params.update(lat=near.lat, lon=near.lon)
        data = self._get(f"/search/2/geocode/{quote(place.text, safe='')}.json", params)
        for found in data.get("results") or []:
            spot = clean_path([[(found.get("position") or {}).get("lat"), (found.get("position") or {}).get("lon")]])
            if spot:
                return spot[0]
        raise MapsError(f"TomTom couldn't find {place.text}")

    def routes(self, origin: Place, destination: Place, mode: str = "drive", depart: Optional[float] = None,
               avoid: Sequence[str] = ()) -> List[Route]:
        travel = {"drive": "car", "walk": "pedestrian", "bike": "bicycle"}.get(mode)
        if travel is None:
            raise MapsError("TomTom has no public transport directions")
        start = self.locate(origin, destination)
        end = self.locate(destination, Place(lat=start[0], lon=start[1]))
        params: Dict[str, Any] = {"travelMode": travel, "traffic": "true", "maxAlternatives": 2,
                                  "instructionsType": "text", "language": self.language,
                                  "computeTravelTimeFor": "all", "routeRepresentation": "polyline"}
        words = {"tolls": "tollRoads", "highways": "motorways", "ferries": "ferries"}
        if any(word in words for word in avoid):
            params["avoid"] = [words[word] for word in avoid if word in words]
        if depart:
            params["departAt"] = _utc(depart)[:-1] + "+00:00"      # TomTom documents an offset, not "Z"
        data = self._get(f"/routing/1/calculateRoute/{start[0]:.6f},{start[1]:.6f}:{end[0]:.6f},{end[1]:.6f}/json",
                         params)
        found: List[Route] = []
        for raw in (data.get("routes") or [])[:3]:
            if not isinstance(raw, dict):
                continue
            summary = raw.get("summary") or {}
            seconds = _seconds(summary.get("travelTimeInSeconds"))
            if seconds is None:
                continue
            path = clean_path(p for leg in raw.get("legs") or [] if isinstance(leg, dict) for p in leg.get("points") or [])
            meters = _whole(summary.get("lengthInMeters"))
            steps, via = self._guidance((raw.get("guidance") or {}).get("instructions") or [], meters or 0)
            found.append(Route(seconds=seconds, usual_seconds=_seconds(summary.get("historicTrafficTravelTimeInSeconds")),
                               meters=meters, via=via, path=path, steps=steps, source=self.name))
        return found

    @staticmethod
    def _guidance(instructions: Sequence[Any], total: int) -> Tuple[List[Step], str]:
        """Turn-by-turn steps, and the two roads that make up most of the trip (TomTom doesn't name its routes)."""
        steps: List[Step] = []
        roads: Dict[str, int] = {}
        usable = [i for i in instructions if isinstance(i, dict)]
        for n, item in enumerate(usable):
            offset = _seconds(item.get("routeOffsetInMeters")) or 0
            after = _seconds(usable[n + 1].get("routeOffsetInMeters")) if n + 1 < len(usable) else total
            length = max(0, (after if after is not None else offset) - offset)
            text = " ".join(str(item.get("message") or "").split())[:160]
            if text:
                steps.append(Step(text, length or None))
            numbers = [str(r) for r in item.get("roadNumbers") or [] if r]
            name = (numbers[0] if numbers else str(item.get("street") or "")).strip()[:40]
            if name:
                roads[name] = roads.get(name, 0) + length
        main = sorted(roads, key=lambda r: -roads[r])[:2]
        return steps, " and ".join(r for r in roads if r in main)

    def incidents(self, paths: Sequence[Sequence[Point]]) -> Tuple[List[Incident], bool]:
        """Current incidents around the routes. Returns (incidents, True if only the first part was covered)."""
        boxes, cut_short = boxes_along(paths)
        found: Dict[str, Incident] = {}
        problems: List[MapsError] = []
        for box in boxes:
            try:
                data = self._get("/traffic/services/5/incidentDetails",
                                 {"bbox": ",".join(f"{value:.5f}" for value in box), "fields": INCIDENT_FIELDS,
                                  "language": self.language, "timeValidityFilter": "present",
                                  "categoryFilter": ",".join(str(k) for k in INCIDENT_KINDS)})
            except MapsError as exc:
                problems.append(exc)
                continue
            for raw in data.get("incidents") or []:
                incident = self._incident(raw)
                if incident is not None:
                    found.setdefault(incident.ident or f"{incident.text}{incident.points[:1]}", incident)
        if problems and len(problems) == len(boxes):
            raise problems[0]
        return list(found.values()), cut_short

    @staticmethod
    def _incident(raw: Any) -> Optional[Incident]:
        if not isinstance(raw, dict):
            return None
        props, shape = raw.get("properties") or {}, raw.get("geometry") or {}
        if not isinstance(props, dict) or not isinstance(shape, dict):
            return None
        category = props.get("iconCategory")
        kind = INCIDENT_KINDS.get(category) if isinstance(category, int) and not isinstance(category, bool) else None
        if kind is None:
            return None
        coords = shape.get("coordinates") or []
        if shape.get("type") == "Point":
            coords = [coords]
        points = clean_path([c[1], c[0]] for c in coords if isinstance(c, (list, tuple)) and len(c) >= 2)
        if not points:
            return None
        events = [e for e in props.get("events") or [] if isinstance(e, dict)]
        what = " ".join(str(events[0].get("description") or "").split())[:80] if events else ""
        text = what or kind[0].capitalize()
        roads = [str(r) for r in props.get("roadNumbers") or [] if r][:2]
        if roads:
            text += " on " + "/".join(roads)
        start, end = (" ".join(str(props.get(k) or "").split())[:60] for k in ("from", "to"))
        if start and end and start != end:
            text += f", from {start} to {end}"
        elif start or end:
            text += f", at {start or end}"
        delay = _seconds(props.get("delay"))
        return Incident(kind=kind[0], text=text, delay=delay or None, allowance=kind[1], points=points,
                        ident=str(props.get("id") or ""))


def on_route(index: PathIndex, incident: Incident, within: float = 100.0) -> bool:
    """Is this incident on the route, in the direction of travel (not on the other side of the freeway)?"""
    points = incident.points
    if not points:
        return False
    probes = points if len(points) <= 5 else [points[round(k * (len(points) - 1) / 4)] for k in range(5)]
    hits = [hit for hit in (index.nearest(p, within) for p in probes) if hit]
    if len(hits) < max(1, math.ceil(0.6 * len(probes))):
        return False
    if len(points) >= 2 and meters_between(points[0], points[-1]) > 50:
        if angle_between(bearing(points[0], points[-1]), index.heading(hits[len(hits) // 2][1])) > 100:
            return False
    return True


# ---- news about the roads -----------------------------------------------------------------------------------
NEWS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
NEWS_WORDS = ("crash", "accident", "collision", "wreck", "pileup", "pile-up", "overturn", "closed", "closure",
              "closes", "closing", "shut down", "shuts down", "blocked", "construction", "roadwork", "road work",
              "lane", "sig-alert", "sigalert", "fatal", "detour", "delays")


@dataclass
class Headline:
    title: str
    source: str = ""
    hours: Optional[float] = None          # how long ago
    roads: List[str] = field(default_factory=list)


def news_roads(routes: Sequence[Route], limit: int = 3) -> List[str]:
    """The roads worth a news search: numbered highways, and named bridges, tunnels and freeways."""
    found: List[str] = []
    for route in routes:
        for name in road_names(route):
            if (road_number(name) or _BIG_ROAD.search(name)) and name not in found:
                found.append(name)
    return found[:limit]


def _mentions(title: str, road: str) -> bool:
    """Does a headline name this road? "I-5", "Interstate 5", "Highway 101", or "on 101" / "northbound 880"."""
    number = road_number(road)
    if not number:
        return road.lower() in title.lower()
    named = rf"\b(?:I|Interstate|US|U\.S\.|Highway|Hwy|Route|Rte|SR|State Route|[A-Z]{{2}})[- ]?{number}\b"
    spoken = rf"\b(?:on|the|(?:north|south|east|west)bound|NB|SB|EB|WB)\s+{number}\b(?!\s*(?:%|percent|people|years|cars))"
    return re.search(named, title) is not None or (len(number) >= 2 and re.search(spoken, title, re.I) is not None)


def road_news(roads: Sequence[str], *, fetch: Optional[Callable[[str], bytes]] = None, now: Optional[float] = None,
              hours: float = 24.0, limit: int = 4) -> List[Headline]:
    """Headlines from the last day that name one of these roads together with a crash, closure or road works.
    Only the road names are sent (to Google News): never where you are or where you're going."""
    from . import news
    roads = [r for r in roads if r][:3]
    if not roads:
        return []
    quoted = " OR ".join(f'"{road}"' for road in roads)
    query = f"({quoted}) (crash OR accident OR closure OR closed OR construction OR roadwork) when:1d"
    data = (fetch or news.fetch)(NEWS_URL.format(query=quote_plus(query)))
    stamp = now if now is not None else datetime.now(timezone.utc).timestamp()
    found: List[Headline] = []
    seen = set()
    for article in news.parse_feed(data, "Google News"):
        title = article.title[:160]
        low = title.lower()
        named = [road for road in roads if _mentions(title, road)]
        if not named or not any(word in low for word in NEWS_WORDS) or news.story_key(title) in seen:
            continue
        age = None
        try:
            published = datetime.fromisoformat(article.published or "")
            if published.tzinfo is None:                  # feeds that give no time zone mean UTC
                published = published.replace(tzinfo=timezone.utc)
            age = (stamp - published.timestamp()) / 3600
        except (TypeError, ValueError):
            pass
        if age is not None and not -1 <= age <= hours:
            continue
        seen.add(news.story_key(title))
        found.append(Headline(title=title, source=article.source[:40], hours=age, roads=named))
        if len(found) >= limit:
            break
    return found


# ---- lining the services up ------------------------------------------------------------------------------
ONE_SOURCE_ALLOWANCE = 120


@dataclass
class Option:
    """One way to go, with each service's route for it."""
    routes: List[Route]
    incidents: List[Incident] = field(default_factory=list)
    headlines: List[Headline] = field(default_factory=list)
    asked: int = 1                                    # how many services answered in all
    index: Optional[PathIndex] = field(default=None, repr=False)

    @property
    def seconds(self) -> int:
        return int(round(statistics.median(r.seconds for r in self.routes)))

    @property
    def usual_seconds(self) -> Optional[int]:
        """The time on a normal day: each service's own "slower than usual", the middle of those."""
        slower = [r.seconds - r.usual_seconds for r in self.routes if r.usual_seconds]
        return self.seconds - int(round(statistics.median(slower))) if slower else None

    @property
    def meters(self) -> Optional[int]:
        return next((r.meters for r in self.routes if r.meters), None)

    @property
    def via(self) -> str:
        return next((r.via for r in self.routes if r.via), "")

    @property
    def sources(self) -> List[str]:
        return [r.source for r in self.routes]

    @property
    def spread(self) -> int:
        return max(r.seconds for r in self.routes) - min(r.seconds for r in self.routes)

    @property
    def allowance(self) -> int:
        """Seconds added when ranking: for incidents reported on the way, and for a way only one service
        suggests when several were asked."""
        risk = min(MAX_ALLOWANCE, sum(i.allowance for i in self.incidents))
        return risk + (ONE_SOURCE_ALLOWANCE if self.asked > 1 and len(self.routes) == 1 else 0)

    @property
    def score(self) -> int:
        return self.seconds + self.allowance

    @property
    def steps(self) -> Tuple[str, List[Step]]:
        route = next((r for r in self.routes if r.steps), None)
        return (route.source, route.steps) if route else ("", [])

    @property
    def reported(self) -> List[str]:
        """What's reported on the way, in words: ["an accident", "road works"]."""
        return list(dict.fromkeys(PHRASES.get(i.kind, i.kind) for i in self.incidents if i.allowance))

    def as_route(self) -> Route:
        notes = list(dict.fromkeys(n for r in self.routes for n in r.notes))
        labels = list(dict.fromkeys(l for r in self.routes for l in r.labels))
        return Route(seconds=self.seconds, usual_seconds=self.usual_seconds, meters=self.meters, via=self.via,
                     labels=labels, notes=notes, source=", ".join(self.sources))


def group(answers: Sequence[Tuple[str, Sequence[Route]]]) -> List[Option]:
    """answers: (service name, its routes), the preferred service first. Routes along the same roads become
    one Option; a service's second route along the same roads is dropped (its faster one stays)."""
    options: List[Option] = []
    for name, routes in answers:
        for route in sorted(routes, key=lambda r: r.seconds):
            route.source = route.source or name
            index = PathIndex(route.path) if len(route.path) >= 2 else None
            match = None
            for option in options:
                if index is not None and option.index is not None:
                    same = same_way(index, option.index)
                else:
                    same = share_a_road(route, option.routes[0])
                if same:
                    match = option
                    break
            if match is None:
                options.append(Option(routes=[route], index=index))
            elif route.source not in match.sources:
                match.routes.append(route)
                if match.index is None or (index is not None and len(index.points) > len(match.index.points)):
                    match.index = index
    for option in options:
        option.asked = len(answers)
    return options


def rank(options: Sequence[Option]) -> List[Option]:
    return sorted(options, key=lambda o: (o.score, -len(o.routes), o.seconds))


def end_gap(reference: Route, other: Route) -> float:
    """How far apart two services put the destination (0 when either has no shape)."""
    if not reference.path or not other.path:
        return 0.0
    return meters_between(reference.path[-1], other.path[-1])


def attach(options: Sequence[Option], incidents: Sequence[Incident], headlines: Sequence[Headline]) -> None:
    for option in options:
        if option.index is not None:
            option.incidents = sorted((i for i in incidents if on_route(option.index, i)),
                                      key=lambda i: (-i.allowance, -(i.delay or 0)))
        keys = {road_number(n) or n.lower() for r in option.routes for n in road_names(r)}
        option.headlines = [h for h in headlines if any((road_number(n) or n.lower()) in keys for n in h.roads)]


__all__ = ["AVOID", "MODES", "Headline", "Incident", "Option", "PathIndex", "TomTom", "attach", "boxes_along",
           "decode_polyline", "end_gap", "group", "news_roads", "on_route", "rank", "road_names", "road_news",
           "same_way"]
