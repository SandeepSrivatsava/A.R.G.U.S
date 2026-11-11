"""Stand-ins for the outside services the new features talk to: a JSON web service (Google Routes, Apple Maps,
ntfy) on 127.0.0.1, and the phone's Termux:API commands."""
from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit


class JsonService(ThreadingHTTPServer):
    """Answers every request with handler(request) -> (status, payload). Keeps every request it saw."""
    daemon_threads = True

    def __init__(self, handler: Callable[[Dict[str, Any]], Tuple[int, Any]]):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.handler = handler
        self.requests: List[Dict[str, Any]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    server: JsonService

    def log_message(self, *args):
        return

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        parts = urlsplit(self.path)
        request = {"method": self.command, "path": parts.path, "query": {k: v[0] for k, v in parse_qs(parts.query).items()},
                   "headers": {k.lower(): v for k, v in self.headers.items()},
                   "body": json.loads(raw) if raw.strip() else None}
        self.server.requests.append(request)
        status, payload = self.server.handler(request)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _serve


class FakeTermux:
    """Termux:API commands, scripted: answers[name] is a value (printed as JSON), a callable(args, stdin)
    returning that, or an exception to raise. Every call is recorded as (args, stdin)."""

    def __init__(self, **answers: Any):
        self.answers: Dict[str, Any] = {k.replace("_", "-"): v for k, v in answers.items()}
        self.calls: List[Tuple[List[str], Optional[str]]] = []

    def names(self) -> List[str]:
        return [args[0] for args, _ in self.calls]

    def __call__(self, args, input=None, capture_output=True, text=True, timeout=None, **kw):
        self.calls.append((list(args), input))
        answer = self.answers.get(args[0], "")
        if callable(answer) and not isinstance(answer, type):
            answer = answer(list(args), input)
        if isinstance(answer, BaseException) or (isinstance(answer, type) and issubclass(answer, BaseException)):
            raise answer
        if isinstance(answer, subprocess.CompletedProcess):
            return answer
        out = answer if isinstance(answer, str) else json.dumps(answer)
        return subprocess.CompletedProcess(args, 0, out, "")


def routed_to(service: JsonService, path: str = ""):
    """An http_json stand-in that sends every request to `service` (keeping the path when `path` is "")."""
    from argus.maps import http_json

    def request(method, url, headers, body=None, timeout=15):
        rest = path or "/" + (url.split("/", 3)[3] if url.count("/") >= 3 else "")
        return http_json(method, service.url + rest, headers, body, timeout)
    return request


# ---- a small world of maps services: the same two ways to the airport, as each service describes them ----------
def encode_polyline(points) -> str:
    """Google's polyline encoding (the reverse of argus.maps.decode_polyline)."""
    out, last = [], (0, 0)
    for lat, lon in points:
        now = (round(lat * 1e5), round(lon * 1e5))
        for value in (now[0] - last[0], now[1] - last[1]):
            value = ~(value << 1) if value < 0 else value << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        last = now
    return "".join(out)


START, END = (45.5231, -122.6765), (45.59, -122.59)
EAST_WAY = [START, (45.5231, -122.56), (45.59, -122.56), END]         # "I-205": east, then north
NORTH_WAY = [START, (45.59, -122.6765), END]                           # "I-5": north, then east


def nudged(path, by=0.0003):
    """The same road as another service draws it: every point about 30 m off."""
    return [path[0]] + [(lat + by, lon - by) for lat, lon in path[1:-1]] + [path[-1]]


def google_route(seconds, meters, name, path, usual=None, **more):
    return dict({"duration": f"{seconds}s", "staticDuration": f"{usual or seconds}s", "distanceMeters": meters,
                 "description": name, "polyline": {"encodedPolyline": encode_polyline(path)},
                 "legs": [{"steps": [{"navigationInstruction": {"instructions": f"Take {name}"}, "distanceMeters": meters - 400},
                                     {"navigationInstruction": {"instructions": "Turn right onto Airport Way"},
                                      "distanceMeters": 400}]}]}, **more)


def apple_reply(*routes):
    """routes: (seconds, meters, name, path). Steps and their shapes are shared lists, as Apple sends them."""
    steps, shapes, out = [], [], []
    for seconds, meters, name, path in routes:
        out.append({"name": name, "durationSeconds": seconds, "distanceMeters": meters, "stepIndexes": [len(steps), len(steps) + 1]})
        steps += [{"instructions": f"Merge onto {name}", "distanceMeters": meters - 300, "stepPathIndex": len(shapes)},
                  {"instructions": "Arrive at the airport", "distanceMeters": 300, "stepPathIndex": len(shapes) + 1}]
        shapes += [[{"latitude": a, "longitude": b} for a, b in path[:-1]], [{"latitude": a, "longitude": b} for a, b in path[-2:]]]
    return {"routes": out, "steps": steps, "stepPaths": shapes}


def tomtom_route(seconds, meters, path, roads, usual=None):
    instructions, offset = [], 0
    for name, length in roads:
        instructions.append({"routeOffsetInMeters": offset, "message": f"Keep left onto {name}.", "roadNumbers": [name]})
        offset += length
    return {"summary": {"travelTimeInSeconds": seconds, "lengthInMeters": meters,
                        "historicTrafficTravelTimeInSeconds": usual or seconds},
            "legs": [{"points": [{"latitude": a, "longitude": b} for a, b in path]}],
            "guidance": {"instructions": instructions}}


def incident(ident, category, points, what, road="", delay=None, start="", end=""):
    line = len(points) > 1
    return {"type": "Feature",
            "geometry": {"type": "LineString" if line else "Point",
                         "coordinates": [[lon, lat] for lat, lon in points] if line else [points[0][1], points[0][0]]},
            "properties": {"id": ident, "iconCategory": category, "events": [{"description": what, "iconCategory": category}],
                           "roadNumbers": [road] if road else [], "delay": delay, "from": start, "to": end}}


class MapsWorld(JsonService):
    """Google Routes, Apple Maps Server API and TomTom behind one address (use with routed_to)."""

    def __init__(self, *, google=None, apple=None, tomtom=None, incidents=(), geocode=END, fail=()):
        self.google, self.apple, self.tomtom = google, apple, tomtom
        self.incidents, self.geocode, self.fail = list(incidents), geocode, set(fail)
        super().__init__(self.answer)

    def answer(self, request):
        path = request["path"]
        if path == "/directions/v2:computeRoutes":
            return (500, {"error": {"message": "Internal"}}) if "google" in self.fail else (200, {"routes": self.google or []})
        if path == "/v1/token":
            return 200, {"accessToken": "access", "expiresInSeconds": 1800}
        if path == "/v1/directions":
            return (503, {}) if "apple" in self.fail else (200, self.apple or {"routes": []})
        if path.startswith("/search/2/geocode/"):
            return 200, {"results": [{"position": {"lat": self.geocode[0], "lon": self.geocode[1]}}]}
        if path.startswith("/routing/1/calculateRoute/"):
            return (403, {"detailedError": {"message": "Forbidden"}}) if "tomtom" in self.fail else (200, {"routes": self.tomtom or []})
        if path == "/traffic/services/5/incidentDetails":
            return (500, {}) if "incidents" in self.fail else (200, {"incidents": self.incidents})
        return 404, {}

    def asked(self, start: str):
        return [r for r in self.requests if r["path"].startswith(start)]
