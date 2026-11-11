"""The best way there: routes from several maps services lined up (traffic.py), TomTom's times and incidents,
news about the roads, the ranking and its reasons, and the links for each navigation app."""
import json
import subprocess
import threading
import time
from email.utils import formatdate

import pytest

from argus import maps, traffic
from argus.maps import (MapsError, Place, Route, Router, Step, brief_steps, clean_path, decode_polyline, link_name,
                        nav_link, nav_links, past_the_start, phone_links, short_distance, thin)
from argus.toolbox import Context
from argus.traffic import (Headline, Incident, Option, PathIndex, TomTom, attach, boxes_along, group, news_roads,
                           on_route, rank, road_names, road_news, road_number, same_way, share_a_road)
from fakes import make_settings
from web_fakes import (EAST_WAY, NORTH_WAY, START, JsonService, MapsWorld, apple_reply, encode_polyline,
                       google_route, incident, nudged, routed_to, tomtom_route)

HERE = {"lat": START[0], "lon": START[1]}


def ctx(tmp_path, **kw):
    return Context(channel="phone", workspace=tmp_path, store=None, timezone="America/Los_Angeles", **kw)


# ---- shapes ------------------------------------------------------------------------------------------------
def test_polylines_are_decoded_and_damage_is_survived():
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    assert decode_polyline(encode_polyline(EAST_WAY)) == [(round(a, 5), round(b, 5)) for a, b in EAST_WAY]
    assert decode_polyline("") == [] and decode_polyline(None) == []
    assert decode_polyline("_p~iF~ps|U_ulL") == [(38.5, -120.2)]             # cut off mid-point
    assert decode_polyline("_p~iF~ps|U\x01") == [(38.5, -120.2)]             # not a polyline character
    assert decode_polyline(encode_polyline([(95.0, 10.0)])) == []            # not a place on Earth


def test_paths_are_cleaned_and_thinned():
    assert clean_path([[45.5, -122.6], {"latitude": 45.6, "longitude": -122.5}, [91, 0], ["x", 1], None, [1],
                       {"latitude": 1}, [float("nan"), 2]]) == [(45.5, -122.6), (45.6, -122.5)]
    assert clean_path(None) == []
    long = [(45.0 + i / 1e4, -122.0) for i in range(7001)]
    short = thin(long)
    assert len(short) <= maps.MAX_PATH_POINTS + 1 and short[0] == long[0] and short[-1] == long[-1]
    assert thin(long[:10]) == long[:10]


def test_how_far_a_point_is_from_a_route():
    east = PathIndex(EAST_WAY)
    assert 18_000 < east.length < 19_500 and len(east.points) > len(EAST_WAY)    # long stretches are split up
    on = east.nearest((45.5233, -122.60), 100)
    assert on is not None and 15 < on[0] < 30 and 80 < east.heading(on[1]) < 100  # 22 m off, on the eastbound stretch
    assert east.nearest((45.53, -122.60), 100) is None and east.nearest((45.53, -122.60), 1000) is not None
    assert east.samples(3)[0] == EAST_WAY[0] and east.samples(3)[-1] == pytest.approx(EAST_WAY[-1])
    assert PathIndex([START]).nearest(START, 100) is None and PathIndex([START]).samples(5) == [START]
    assert PathIndex([START, START, START]).length == 0
    assert traffic.angle_between(350, 10) == 20 and traffic.angle_between(90, 270) == 180
    assert traffic.segment_distance(START, START, START) == 0


def test_the_same_road_from_two_services_is_one_way():
    east, also_east, north = PathIndex(EAST_WAY), PathIndex(nudged(EAST_WAY)), PathIndex(NORTH_WAY)
    assert same_way(east, also_east) and same_way(also_east, east)
    assert not same_way(east, north) and not same_way(east, PathIndex([START]))
    detour = EAST_WAY[:2] + [(45.5231, -122.54), (45.59, -122.54)] + EAST_WAY[2:]       # 1.5 km further east for a while
    assert not same_way(east, PathIndex(detour))


def test_boxes_follow_the_route():
    boxes, cut = boxes_along([PathIndex(EAST_WAY).points, PathIndex(nudged(EAST_WAY)).points])
    assert boxes == [(-122.6805, 45.5191, -122.556, 45.594)] and not cut      # the second route adds nothing new
    assert len(boxes_along([PathIndex(EAST_WAY).points, PathIndex([(45.60, -122.70), (45.62, -122.70)]).points])[0]) == 2
    far = PathIndex([(45.0, -122.0), (47.0, -122.0)]).points                  # 220 km north
    boxes, cut = boxes_along([far])
    assert len(boxes) == traffic.MAX_BOXES and cut
    assert all(b[3] - b[1] <= 0.22 and b[1] < b[3] for b in boxes) and boxes[1][1] < boxes[0][3]   # they overlap a little
    assert boxes_along([]) == ([], False)


# ---- road names -----------------------------------------------------------------------------------------------
def test_road_names():
    assert road_names(Route(1, via="I-880 S and CA-237 W")) == ["I-880", "CA-237"]
    assert road_names(Route(1, via="US-101 N/Bayshore Fwy")) == ["US-101", "Bayshore Fwy"]
    steps = [Step("Take I-5 N toward Seattle", 9000), Step("Take exit 307 for OR-99E", 300), Step("Turn left", 50),
             Step("Merge onto Highway 30", 2000)]
    assert road_names(Route(1, steps=steps)) == ["I-5", "Highway 30", "OR-99E"]
    assert road_names(Route(1, via="I-5 N", steps=steps), limit=2) == ["I-5", "Highway 30"]
    assert road_names(Route(1)) == []
    assert (road_number("I-880"), road_number("Highway 101"), road_number("Bayshore Fwy")) == ("880", "101", "")
    assert share_a_road(Route(1, meters=20_000, via="I-5 N"), Route(1, meters=20_500, via="Interstate 5"))
    assert not share_a_road(Route(1, meters=20_000, via="I-5 N"), Route(1, meters=26_000, via="I-5 N"))
    assert not share_a_road(Route(1, meters=20_000, via="I-5 N"), Route(1, meters=20_000, via="I-205 N"))
    assert not share_a_road(Route(1, via="I-5 N"), Route(1, via="I-5 N"))
    assert news_roads([Route(1, via="I-5 N and Burnside St"), Route(1, via="Hawthorne Bridge")]) == ["I-5", "Hawthorne Bridge"]


# ---- TomTom ---------------------------------------------------------------------------------------------------
def test_tomtom_routes_request_and_answer():
    world = MapsWorld(tomtom=[tomtom_route(1560, 24_000, EAST_WAY, [("NE Sandy Blvd", 900), ("I-84", 8000), ("I-205", 14_000)],
                                           usual=1400),
                              {"summary": {"lengthInMeters": 5}}, "junk"])
    tomtom = TomTom("tt-secret-key", base=world.url)
    found = tomtom.routes(Place.current(HERE), Place.from_text("Portland Airport, OR"), "drive", depart=2_000_000_000.0,
                          avoid=["tolls", "ferries"])
    geocode, routing = world.requests
    assert geocode["path"] == "/search/2/geocode/Portland%20Airport%2C%20OR.json"
    assert geocode["query"] == {"limit": "1", "lat": "45.5231", "lon": "-122.6765", "key": "tt-secret-key"}
    assert routing["path"] == "/routing/1/calculateRoute/45.523100,-122.676500:45.590000,-122.590000/json"
    assert routing["query"] == {"travelMode": "car", "traffic": "true", "maxAlternatives": "2", "instructionsType": "text",
                                "language": "en-US", "computeTravelTimeFor": "all", "routeRepresentation": "polyline",
                                "avoid": "tollRoads", "departAt": "2033-05-18T03:33:20+00:00", "key": "tt-secret-key"}
    assert "avoid=tollRoads&avoid=ferries" in maps.http_json.__name__ or len(found) == 1     # both avoids are sent (below)
    route = found[0]
    assert (route.seconds, route.usual_seconds, route.meters, route.via, route.source) == \
        (1560, 1400, 24_000, "I-84 and I-205", "TomTom")
    assert route.path == EAST_WAY
    assert [(s.text, s.meters) for s in route.steps] == [("Keep left onto NE Sandy Blvd.", 900), ("Keep left onto I-84.", 8000),
                                                         ("Keep left onto I-205.", 15_100)]
    tomtom.routes(Place.from_text("45.5,-122.6"), Place.from_text("45.6,-122.5"), "walk")
    assert world.requests[-1]["query"]["travelMode"] == "pedestrian" and "avoid" not in world.requests[-1]["query"]
    assert len(world.requests) == 3                                           # two points: nothing to look up
    with pytest.raises(MapsError, match="no public transport"):
        tomtom.routes(Place.current(HERE), Place.from_text("PDX"), "transit")


def test_tomtom_sends_every_avoid_word():
    seen = []

    def request(method, url, headers, body=None, timeout=15):
        seen.append(url)
        return 200, {"routes": []}
    TomTom("k", request=request).routes(Place.current(HERE), Place.from_text("45.6,-122.5"), avoid=["tolls", "highways", "ferries"])
    assert "avoid=tollRoads&avoid=motorways&avoid=ferries" in seen[0] and seen[0].startswith("https://api.tomtom.com/routing/1/")


@pytest.mark.parametrize("status,reply,expected", [
    (403, {"detailedError": {"message": "Forbidden tt-secret-key"}}, "TomTom refused the key"),
    (429, {}, "free daily limit"),
    (400, {"detailedError": {"message": "Engine error for tt-secret-key: no route"}}, r"HTTP 400 \(Engine error for \.\.\.: no route\)"),
    (500, {"error": "boom"}, r"HTTP 500 \(boom\)"),
    (200, [1, 2], "HTTP 200"),
])
def test_tomtom_errors_never_show_the_key(status, reply, expected):
    service = JsonService(lambda request: (status, reply))
    with pytest.raises(MapsError, match=expected) as caught:
        TomTom("tt-secret-key", base=service.url).routes(Place.current(HERE), Place.from_text("45.6,-122.5"))
    assert "tt-secret-key" not in str(caught.value)
    with pytest.raises(MapsError) as caught:
        TomTom("tt-secret-key", base="http://127.0.0.1:9").routes(Place.current(HERE), Place.from_text("45.6,-122.5"))
    assert "tt-secret-key" not in str(caught.value)


def test_tomtom_says_when_it_cannot_find_a_place():
    service = JsonService(lambda request: (200, {"results": [{"position": {"lat": "north"}}, {}]}))
    with pytest.raises(MapsError, match="TomTom couldn't find Atlantis"):
        TomTom("k", base=service.url).routes(Place.current(HERE), Place.from_text("Atlantis"))


CRASH = incident("a1", 1, [(45.55, -122.6765), (45.56, -122.6765)], "Accident", "I-5", delay=420, start="Going St", end="Alberta St")
OTHER_SIDE = incident("a2", 1, [(45.57, -122.6766), (45.56, -122.6766)], "Accident", "I-5")
WORKS = incident("w1", 9, [(45.5231, -122.60), (45.5231, -122.59)], "Roadworks", "I-84", start="NE 60th Ave")
ELSEWHERE = incident("x1", 8, [(45.40, -122.50), (45.41, -122.50)], "Closed", "OR-224")


def test_tomtom_incidents():
    world = MapsWorld(incidents=[CRASH, CRASH, OTHER_SIDE, WORKS, ELSEWHERE, "junk", {"properties": 1},
                                 incident("r1", 4, [START], "Rain"),
                                 {"geometry": {"type": "LineString", "coordinates": []}, "properties": {"iconCategory": 6}},
                                 {"geometry": {"type": "Point", "coordinates": [1]}, "properties": {"iconCategory": 1}},
                                 {"geometry": {"type": "Point", "coordinates": [-122.6, 45.5]}, "properties": {"iconCategory": [1]}},
                                 incident("p1", 14, [(45.5231, -122.58)], "", delay=30)])
    tomtom = TomTom("tt-key", base=world.url)
    found, cut = tomtom.incidents([PathIndex(EAST_WAY).points, PathIndex(NORTH_WAY).points])
    assert not cut and [i.ident for i in found] == ["a1", "a2", "w1", "x1", "p1"]
    asked = world.asked("/traffic/services/5/incidentDetails")
    assert len(asked) == 1 and asked[0]["query"]["bbox"] == "-122.68050,45.51910,-122.55600,45.59400"
    assert asked[0]["query"]["categoryFilter"] == "1,2,3,5,6,7,8,9,11,14" and asked[0]["query"]["timeValidityFilter"] == "present"
    assert asked[0]["query"]["fields"].startswith("{incidents{type,geometry{type,coordinates},properties{id,iconCategory")
    crash, _, works, _, stalled = found
    assert (crash.kind, crash.text, crash.delay, crash.allowance) == \
        ("accident", "Accident on I-5, from Going St to Alberta St", 420, 240)
    assert works.text == "Roadworks on I-84, at NE 60th Ave" and works.delay is None
    assert stalled.text == "Broken-down vehicle" and stalled.points == [(45.5231, -122.58)]
    east, north = PathIndex(EAST_WAY), PathIndex(NORTH_WAY)
    assert on_route(north, crash) and not on_route(east, crash)
    assert not on_route(north, found[1])                      # the same road, the other direction
    assert on_route(east, works) and not on_route(north, works)
    assert on_route(east, stalled) and not on_route(east, found[3]) and not on_route(east, Incident("x", "x"))
    long_jam = Incident("traffic jam", "Jam", points=[(45.50 + k / 100, -122.6765) for k in range(12)])
    assert on_route(north, long_jam)                          # most of it lies on the route
    assert not on_route(north, Incident("traffic jam", "Jam", points=[(45.3 + k / 20, -122.6765) for k in range(12)]))


def test_tomtom_incidents_when_the_service_struggles():
    broken = MapsWorld(fail={"incidents"})
    with pytest.raises(MapsError, match="HTTP 500"):
        TomTom("k", base=broken.url).incidents([PathIndex(EAST_WAY).points])
    answers = iter([(500, {}), (200, {"incidents": [WORKS]})])
    flaky = JsonService(lambda request: next(answers))
    found, cut = TomTom("k", base=flaky.url).incidents([PathIndex([(45.0, -122.0), (45.3, -122.0)]).points])
    assert [i.ident for i in found] == ["w1"] and not cut      # one box failed, the other answered
    assert TomTom("k", base=flaky.url).incidents([]) == ([], False)


# ---- news about the roads -------------------------------------------------------------------------------------
def feed(*items):
    body = "".join(f"<item><title>{title} - {source}</title><source>{source}</source><link>https://example.com/x</link>"
                   f"<pubDate>{formatdate(1_800_000_000 - hours * 3600, usegmt=source != 'KATU')}</pubDate></item>" for title, source, hours in items)
    return f"<rss><channel><title>search</title>{body}</channel></rss>".encode()


def test_road_news_keeps_only_headlines_about_these_roads():
    asked = []

    def fetch(url):
        asked.append(url)
        return feed(("Crash blocks two lanes of I-5 near Rose Quarter", "KGW", 2),
                    ("Crash blocks two lanes of I-5 near Rose Quarter", "KOIN", 1),          # the same story again
                    ("5 things to know about the stock market crash", "Money", 1),           # a 5, but not the road
                    ("Interstate 5 bridge funding debated in Salem", "OPB", 3),              # the road, but no incident
                    ("Overnight closure planned on I-205 at Glenn Jackson Bridge", "ODOT", 0.5),
                    ("Semi overturns on northbound 205 near Airport Way", "KATU", 5),
                    ("Hawthorne Bridge closed for inspection this weekend", "Oregonian", 30),  # older than a day
                    ("Construction on Highway 26 to last all summer", "KPTV", 4))            # not one of our roads
    found = road_news(["I-5", "I-205", "Hawthorne Bridge", "OR-99E"], fetch=fetch, now=1_800_000_000)
    assert [(h.title, h.source, h.roads) for h in found] == [
        ("Crash blocks two lanes of I-5 near Rose Quarter", "KGW", ["I-5"]),
        ("Overnight closure planned on I-205 at Glenn Jackson Bridge", "ODOT", ["I-205"]),
        ("Semi overturns on northbound 205 near Airport Way", "KATU", ["I-205"])]
    assert found[0].hours == pytest.approx(2) and found[1].hours == pytest.approx(0.5)
    assert len(asked) == 1 and asked[0].startswith("https://news.google.com/rss/search?q=%28%22I-5%22+OR+%22I-205%22+OR+"
                                                   "%22Hawthorne+Bridge%22%29+%28crash+OR+accident+OR+closure")
    assert "when%3A1d" in asked[0] and "OR-99E" not in asked[0] and "45." not in asked[0]     # three roads, nothing else
    assert road_news([], fetch=fetch) == [] and len(asked) == 1
    named = road_news(["Hawthorne Bridge"], fetch=lambda url: feed(("Hawthorne Bridge closed after crash", "KGW", 1)), now=1_800_000_000)
    assert named[0].roads == ["Hawthorne Bridge"]
    undated = b"<rss><channel><item><title>I-5 closed by crash</title></item></channel></rss>"
    assert road_news(["I-5"], fetch=lambda url: undated)[0].hours is None
    assert len(road_news(["I-5"], fetch=lambda url: feed(*[(f"Crash number {n} on I-5", "KGW", 1) for n in range(9)]),
                         now=1_800_000_000)) == 4


# ---- lining the services up -----------------------------------------------------------------------------------
def test_routes_are_grouped_by_where_they_run():
    apple = [Route(1440, meters=24_000, via="I-205", path=EAST_WAY, steps=[Step("Merge onto I-205", 20_000)]),
             Route(1320, meters=19_800, via="I-5", path=NORTH_WAY, notes=["has tolls"])]
    google = [Route(1380, 1300, 19_800, "I-5 N", path=nudged(NORTH_WAY), labels=["uses the least fuel"], notes=["has tolls"]),
              Route(1500, 1450, 24_000, "I-205 N", path=nudged(EAST_WAY)),
              Route(1900, meters=24_100, via="I-205 N again", path=nudged(EAST_WAY, 0.0005))]     # its slower twin is dropped
    tomtom = [Route(1560, meters=24_000, via="I-84 and I-205", path=EAST_WAY)]
    options = group([("Apple Maps", apple), ("Google Maps", google), ("TomTom", tomtom)])
    assert [(o.via, o.sources, o.seconds, o.spread) for o in options] == [
        ("I-5", ["Apple Maps", "Google Maps"], 1350, 60),
        ("I-205", ["Apple Maps", "Google Maps", "TomTom"], 1500, 120)]
    north, east = options
    assert north.usual_seconds == 1270 and east.usual_seconds == 1450 and north.meters == 19_800   # 80 s and 50 s slower
    assert east.steps == ("Apple Maps", apple[0].steps) and north.steps == ("", [])
    assert north.as_route().notes == ["has tolls"] and north.as_route().labels == ["uses the least fuel"]
    assert north.as_route().source == "Apple Maps, Google Maps"
    assert [o.via for o in rank(options)] == ["I-5", "I-205"]                 # nothing in the way: the quicker one
    crash = Incident("accident", "Accident on I-5", allowance=240, points=[(45.55, -122.6765), (45.56, -122.6765)])
    jam = Incident("traffic jam", "Queue on I-5", delay=300, points=[(45.56, -122.6765), (45.57, -122.6765)])
    attach(options, [crash, jam], [Headline("Crash on I-5", "KGW", 1, ["I-5"]), Headline("Works on I-205", "ODOT", 2, ["I-205"])])
    assert north.incidents == [crash, jam] and east.incidents == [] and north.reported == ["an accident"]
    assert [h.title for h in north.headlines] == ["Crash on I-5"] and [h.title for h in east.headlines] == ["Works on I-205"]
    assert (north.allowance, north.score, east.score) == (240, 1590, 1500)
    assert [o.via for o in rank(options)] == ["I-205", "I-5"]                 # 2.5 min quicker isn't worth an accident


def test_grouping_without_shapes_and_the_allowances():
    answers = [("Apple Maps", [Route(1200, meters=20_000, via="I-5 N"), Route(1500, meters=26_000, via="I-205 N")]),
               ("Google Maps", [Route(1260, meters=20_300, via="I-5 N and OR-99E"), Route(1100, meters=21_000, via="US-30")])]
    options = group(answers)
    assert [(o.via, o.sources) for o in options] == [("I-5 N", ["Apple Maps", "Google Maps"]), ("I-205 N", ["Apple Maps"]),
                                                     ("US-30", ["Google Maps"])]
    assert [o.allowance for o in options] == [0, 120, 120]                    # a way only one of two services suggests
    assert [o.via for o in rank(options)] == ["US-30", "I-5 N", "I-205 N"]    # 1220 beats 1230
    alone = group([("Google Maps", [Route(900, via="A"), Route(950, via="B")])])
    assert [o.allowance for o in alone] == [0, 0] and alone[0].sources == ["Google Maps"]
    many = Option([Route(600, source="TomTom")], incidents=[Incident("road closed", "x", allowance=480)] * 3)
    assert many.allowance == traffic.MAX_ALLOWANCE and many.reported == ["a closure"]
    assert traffic.end_gap(Route(1, path=EAST_WAY), Route(1, path=NORTH_WAY)) == 0
    assert traffic.end_gap(Route(1, path=EAST_WAY), Route(1)) == 0
    assert 2000 < traffic.end_gap(Route(1, path=EAST_WAY), Route(1, path=[START, (45.61, -122.59)])) < 2400


# ---- the whole check, through the route tool ------------------------------------------------------------------
KEYS = {"GOOGLE_MAPS_API_KEY": "AIza-key", "APPLE_MAPS_TOKEN": "apple-jwt", "TOMTOM_API_KEY": "tt-secret-key"}


def world(**changes):
    setup = dict(google=[google_route(1500, 24_000, "I-205 N", nudged(EAST_WAY), usual=1300),      # 200 s slower than usual
                         google_route(1380, 19_800, "I-5 N", nudged(NORTH_WAY), warnings=["This route has tolls."])],
                 apple=apple_reply((1440, 24_000, "I-205", EAST_WAY), (1320, 19_800, "I-5", NORTH_WAY)),
                 tomtom=[tomtom_route(1560, 24_000, EAST_WAY, [("I-84", 9000), ("I-205", 15_000)], usual=1400),
                         tomtom_route(1400, 19_800, NORTH_WAY, [("I-5", 19_800)])],
                 incidents=[CRASH, OTHER_SIDE, WORKS, ELSEWHERE])
    return MapsWorld(**dict(setup, **changes))


def router(tmp_path, service, *, keys=KEYS, fetch=None, **settings):
    s = make_settings(tmp_path, **settings)
    return Router(s, keys, request=routed_to(service), mapkit_exe=tmp_path / "no-helper", fetch=fetch,
                  clock=lambda: 1_800_000_000.0)


def test_route_tool_compares_every_service_and_explains_its_choice(tmp_path):
    w = world()
    news = lambda url: feed(("Crash blocks two lanes of I-5 near Rose Quarter", "KGW", 2))       # noqa: E731
    r = router(tmp_path, w, fetch=news, maps_news=True, maps_apps=["google", "waze"])
    assert r.describe() == ("live traffic from Apple Maps Server API, Google Routes API and TomTom; "
                            "incident reports from TomTom")
    c = ctx(tmp_path, location=HERE)
    out = r.tool({"destination": "Portland Airport, OR"}, c)
    assert not out.is_error
    lines = out.content.split("\n")
    assert lines[0] == ("Driving from your current location to Portland Airport, OR, leaving now. Checked: Apple Maps, "
                        "Google Maps and TomTom (live traffic); incidents from TomTom; road news.")
    assert lines[1] == ("1. BEST: via I-205: 25 min (Apple Maps 24 min, Google Maps 25 min, TomTom 26 min), 14.9 mi. "
                        "3 min slower than usual because of traffic.")
    assert lines[2] == "   - Roadworks on I-84, at NE 60th Ave (TomTom)"
    assert lines[3] == "2. via I-5: 23 min (Apple Maps 22 min, Google Maps 23 min, TomTom 23 min), 12.3 mi. This route has tolls."
    assert lines[4] == "   - Accident on I-5, from Going St to Alberta St, 7 min delay (TomTom)"
    assert lines[5] == ("   - In the news (unverified, may be about another area): \"Crash blocks two lanes of I-5 near "
                        "Rose Quarter\" (KGW, 2 h ago)")
    assert lines[6] == ("Why: route 2 looks 2 min quicker, but it has an accident reported on it, so route 1 is the "
                        "safer choice.")
    assert lines[7] == ("Turn by turn for route 1 (Apple Maps, after the first turns from where the user is): "
                        "1) Arrive at the airport (0.2 mi).")
    apple, google, waze = c.links
    assert lines[8] == f"Navigation link (Apple Maps): {apple}" and apple == "https://maps.apple.com/?daddr=Portland+Airport%2C+OR&dirflg=d"
    assert lines[9] == f"The phone can also open it in: Google Maps: {google}; Waze: {waze}"
    assert waze == "https://waze.com/ul?q=Portland+Airport%2C+OR&navigate=yes"
    assert lines[10] == ("The maps app picks its own route when it opens. If it shows a different one, the user should "
                         "pick the one via I-205.")
    assert len(lines) == 11 and "45.52" not in out.content and "tt-secret-key" not in out.content
    # where the location went: to the three maps services, never to the news search
    assert w.asked("/v1/directions")[0]["query"]["origin"] == "45.523100,-122.676500"
    assert w.asked("/routing/1/calculateRoute/45.523100,-122.676500:")
    assert phone_links(c.links) == {"open": apple, "links": {"Apple Maps": apple, "Google Maps": google, "Waze": waze}}


def test_the_quick_check_skips_incidents_and_news(tmp_path):
    w = world()
    r = router(tmp_path, w, fetch=lambda url: pytest.fail("the quick check reads no news"), maps_news=True)
    names, routes = r.plan(Place.current(HERE), Place.from_text("Portland Airport, OR"))
    assert names == "Apple Maps and Google Maps and TomTom"
    assert [(x.via, x.seconds) for x in routes] == [("I-5", 1380), ("I-205", 1500)]
    assert not w.asked("/traffic/")


def test_later_trips_and_other_ways_of_travelling_skip_the_live_checks(tmp_path):
    w = world()
    r = router(tmp_path, w, fetch=lambda url: pytest.fail("no news for a later trip"), maps_news=True)
    out = r.tool({"destination": "Portland Airport, OR", "depart_at": "2027-01-16T08:00"}, ctx(tmp_path, location=HERE))
    assert "leaving Sat 16 Jan 08:00. Checked: Apple Maps, Google Maps and TomTom (live traffic)." in out.content
    assert "1. BEST: via I-5: 23 min" in out.content and not w.asked("/traffic/")
    assert "Why: route 1 is 2 min faster than the next best, and 3 services agree on it within 1 min." in out.content
    walk = r.tool({"destination": "Portland Airport, OR", "mode": "walk", "origin": "12 Elm St, Portland"}, ctx(tmp_path))
    assert "Walking from 12 Elm St, Portland to Portland Airport, OR, leaving now. Checked: Apple Maps, Google Maps and TomTom." in walk.content
    assert "waze" not in walk.content.lower() and not w.asked("/traffic/")
    transit = r.tool({"destination": "Portland Airport, OR", "mode": "transit"}, ctx(tmp_path, location=HERE))
    assert "Checked: Apple Maps and Google Maps." in transit.content           # TomTom has no public transport


def test_one_service_failing_or_disagreeing_is_said_not_hidden(tmp_path):
    w = world(fail={"google", "incidents"})
    out = router(tmp_path, w).tool({"destination": "Portland Airport, OR"}, ctx(tmp_path, location=HERE))
    assert not out.is_error and "Checked: Apple Maps and TomTom (live traffic)." in out.content
    assert "Not included: Google Maps (Google Maps answered HTTP 500 (Internal))." in out.content
    assert "Note: incidents couldn't be checked (TomTom answered HTTP 500)." in out.content
    assert "1. BEST: via I-5: 23 min (Apple Maps 22 min, TomTom 23 min)" in out.content
    # TomTom reads the address as somewhere 5 km away: its routes are for another trip, so they're left out
    elsewhere = [(45.5231, -122.6765), (45.60, -122.6765), (45.635, -122.59)]
    w = world(tomtom=[tomtom_route(900, 15_000, elsewhere, [("I-5", 15_000)])], incidents=[])
    out = router(tmp_path, w).tool({"destination": "Portland Airport, OR"}, ctx(tmp_path, location=HERE))
    assert "Checked: Apple Maps and Google Maps (live traffic); incidents from TomTom." in out.content
    assert "Note: TomTom put the destination 3.1 mi from where Apple Maps did, so its routes were left out." in out.content
    assert "15 min" not in out.content
    # everything down: the error names each service, and the link still works
    w = world(fail={"google", "apple", "tomtom"})
    out = router(tmp_path, w).tool({"destination": "Portland Airport, OR"}, ctx(tmp_path, location=HERE))
    assert out.is_error and "Apple Maps: Apple Maps answered HTTP 503; Google Maps: Google Maps answered HTTP 500 (Internal); " \
        "TomTom: TomTom refused the key" in out.content and "still works: https://maps.apple.com/" in out.content
    w = world(google=[], apple={"routes": []}, tomtom=[])
    out = router(tmp_path, w).tool({"destination": "Atlantis"}, ctx(tmp_path, location=HERE))
    assert out.is_error and "No driving route was found" in out.content


def test_news_trouble_and_long_trips_are_handled(tmp_path):
    def down(url):
        raise OSError("no network")
    out = router(tmp_path, world(), fetch=down, maps_news=True).tool({"destination": "Portland Airport, OR"},
                                                                     ctx(tmp_path, location=HERE))
    assert not out.is_error and "incidents from TomTom." in out.content and "road news" not in out.content
    far = [(45.0, -122.0), (47.0, -122.0)]
    w = MapsWorld(google=[google_route(8000, 222_000, "I-5 N", far)], incidents=[])
    r = router(tmp_path, w, keys={"GOOGLE_MAPS_API_KEY": "k", "TOMTOM_API_KEY": "t"}, maps_traffic="google")
    out = r.tool({"destination": "47.0,-122.0", "origin": "45.0,-122.0"}, ctx(tmp_path))
    assert "1. via I-5 N: 2 h 13 min, 138 mi." in out.content and "BEST" not in out.content and "Why" not in out.content
    assert "Note: incidents were checked for the first part of the trip only (it's a long one)." in out.content
    assert len(w.asked("/traffic/")) == traffic.MAX_BOXES and not w.asked("/routing/")       # TomTom wasn't asked for routes


def test_which_services_are_asked(tmp_path):
    w = world()
    names = lambda r, mode="drive": [key for key, _ in r.sources(mode)]            # noqa: E731
    assert names(router(tmp_path, w)) == ["apple", "google", "tomtom"]
    assert names(router(tmp_path, w, phone_type="android")) == ["google", "apple", "tomtom"]
    assert names(router(tmp_path, w, maps_app="waze")) == ["google", "apple", "tomtom"]
    assert names(router(tmp_path, w), "transit") == ["apple", "google"]
    assert names(router(tmp_path, w, maps_traffic="tomtom")) == ["tomtom"] and names(router(tmp_path, w, maps_traffic="off")) == []
    assert names(router(tmp_path, w, maps_traffic="mapkit")) == []
    only = router(tmp_path, w, keys={"TOMTOM_API_KEY": "t"})
    assert names(only) == ["tomtom"] and only.describe() == "live traffic from TomTom; incident reports from TomTom"
    assert isinstance(only.service(), TomTom)
    out = only.tool({"destination": "Portland Airport, OR", "mode": "transit"}, ctx(tmp_path, location=HERE))
    assert "No live-traffic service is set up" in out.content
    out = only.tool({"destination": "Portland Airport, OR"}, ctx(tmp_path, location=HERE))
    assert "1. BEST: via I-84 and I-205: 26 min, 14.9 mi. 3 min slower than usual because of traffic." in out.content
    assert "2. via I-5: 23 min, 12.3 mi." in out.content and "(TomTom 23 min)" not in out.content
    helper = tmp_path / "argus-maps"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    both = Router(make_settings(tmp_path), KEYS, request=routed_to(w), mapkit_exe=helper)
    assert names(both) == ["apple", "google", "tomtom"]                      # Apple's server and MapKit are the same data
    mac = Router(make_settings(tmp_path), {"TOMTOM_API_KEY": "t"}, request=routed_to(w), mapkit_exe=helper)
    assert names(mac) == ["mapkit", "tomtom"]
    assert mac.describe() == "live traffic from MapKit on this Mac and TomTom; incident reports from TomTom"


def test_the_mac_helper_gives_shapes_and_steps(tmp_path):
    helper = tmp_path / "argus-maps"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    reply = {"routes": [{"name": "I-205 N", "seconds": 1440, "meters": 24_000, "notes": [], "path": [list(p) for p in EAST_WAY],
                         "steps": [{"text": "Turn right onto NE Sandy Blvd", "meters": 320}, {"text": " "}, "junk"]}]}
    run = lambda args, **kw: subprocess.CompletedProcess(args, 0, json.dumps(reply), "")      # noqa: E731
    route = maps.MapKitHelper(helper, run=run).routes(Place.current(HERE), Place.from_text("PDX"))[0]
    assert route.path == EAST_WAY and [(s.text, s.meters) for s in route.steps] == [("Turn right onto NE Sandy Blvd", 320)]
    assert route.source == "Apple Maps"
    w = world()
    mac = Router(make_settings(tmp_path), {"GOOGLE_MAPS_API_KEY": "k"}, request=routed_to(w), mapkit_exe=helper, run=run)
    found = mac.compare(Place.current(HERE), Place.from_text("Portland Airport, OR"))
    assert [(o.via, o.sources, o.score) for o in found.options] == [("I-205 N", ["Apple Maps", "Google Maps"], 1470),
                                                                    ("I-5 N", ["Google Maps"], 1380 + 120)]


def test_checks_run_side_by_side_and_slow_ones_are_dropped():
    started = threading.Event()

    def slow():
        started.set()
        time.sleep(0.5)
        return "late"

    def broken():
        raise KeyError("surprise")
    results = Router._together({"quick": lambda: "ok", "slow": slow, "broken": broken,
                                "refused": lambda: (_ for _ in ()).throw(MapsError("refused"))}, 0.1)
    assert results["quick"] == "ok" and started.is_set()
    assert str(results["slow"]) == "didn't answer in time" and str(results["broken"]) == "gave an answer Argus couldn't use"
    assert str(results["refused"]) == "refused"
    assert Router._together({}, 1) == {} and Router._together({"one": lambda: 1}, 1) == {"one": 1}


def test_why_lines():
    def option(seconds, *sources, incidents=(), asked=1):
        return Option([Route(seconds + n * 30, source=s) for n, s in enumerate(sources)], incidents=list(incidents), asked=asked)
    assert Router._why([option(600, "Google Maps")]) == ""
    assert Router._why([option(600, "Google Maps"), option(900, "Google Maps")]) == "Why: route 1 is 5 min faster than the next best."
    assert Router._why([option(600, "A"), option(620, "A")]) == "Why: the top routes take about the same time."
    assert Router._why([option(600, "A", "B"), option(900, "A")]) == \
        "Why: route 1 is 5 min faster than the next best, and 2 services agree on it."
    lone = option(500, "TomTom", asked=3)
    assert Router._why([option(600, "A", "B", "C", asked=3), lone]) == \
        "Why: route 2 looks 2 min quicker, but only TomTom suggests it, so route 1 is the safer choice."
    risky = option(500, "A", "B", incidents=[Incident("road closed", "x", allowance=480), Incident("road works", "y", allowance=60)])
    assert "but it has a closure and road works reported on it, so" in Router._why([option(600, "A", "B"), risky])


# ---- links, steps, small things ----------------------------------------------------------------------------
def test_the_first_turns_from_here_stay_out_of_the_conversation(tmp_path):
    steps = [Step("Head north on Elm St", 120), Step("Turn right onto 5th Ave", 200), Step("Merge onto I-205 N", 20000),
             Step("Arrive", 50)]
    assert [s.text for s in past_the_start(steps)] == ["Merge onto I-205 N", "Arrive"]
    assert [s.text for s in past_the_start(steps[:2])] == ["Turn right onto 5th Ave"]      # a very short trip
    assert past_the_start([]) == []
    w = world()
    r = router(tmp_path, w, maps_news=False)
    said = r.tool({"destination": "Portland Airport, OR", "origin": "12 Elm St, Portland"}, ctx(tmp_path)).content
    assert "Turn by turn for route 1 (Apple Maps): 1) Merge onto I-205 (14.7 mi); 2) Arrive at the airport (0.2 mi)." in said


def test_the_route_tool_opens_the_app_that_takes_the_best_route(tmp_path):
    # Apple's own first choice is I-5 (23 min by its count), but Google sees 38 min there; both like I-205
    w = world(google=[google_route(1500, 24_000, "I-205 N", nudged(EAST_WAY)), google_route(2300, 19_800, "I-5 N", nudged(NORTH_WAY))],
              apple=apple_reply((1560, 24_000, "I-205", EAST_WAY), (1400, 19_800, "I-5", NORTH_WAY)))
    keys = {k: v for k, v in KEYS.items() if k != "TOMTOM_API_KEY"}
    r = router(tmp_path, w, keys=keys, maps_news=False, maps_apps=["google"])
    c = ctx(tmp_path, location=HERE)
    said = r.tool({"destination": "Portland Airport, OR"}, c).content
    assert "1. BEST: via I-205: 26 min (Apple Maps 26 min, Google Maps 25 min)" in said
    assert ("Opening Google Maps this time, not Apple Maps: route 1 is Google Maps' own first choice, while Apple Maps "
            "would take route 2.\nNavigation link (Google Maps): https://www.google.com/maps/dir/") in said
    assert [link_name(url) for url in c.links] == ["Google Maps", "Apple Maps"]
    assert phone_links(c.links)["open"].startswith("https://www.google.com/maps/dir/")


def test_apple_answers_that_make_no_sense_are_skipped(tmp_path):
    odd = {"routes": [{"name": "I-205", "durationSeconds": 1500, "stepIndexes": [0, 1, 7, "x"]}],
           "steps": ["not a step", {"instructions": "Arrive", "stepPathIndex": 5}], "stepPaths": "none"}
    apple = maps.AppleMapsServer("jwt", request=routed_to(world(apple=odd)))
    (route,) = apple.routes(Place.from_text("A"), Place.from_text("B"))
    assert [s.text for s in route.steps] == ["Arrive"] and route.path == []
    assert maps.AppleMapsServer("jwt", request=routed_to(world(apple=["a list"]))).routes(Place.from_text("A"), Place.from_text("B")) == []


def test_the_app_that_takes_the_best_route_opens_first():
    def option(*routes, incidents=()):
        return Option([Route(seconds, source=source) for source, seconds in routes], incidents=list(incidents), asked=2)
    apps = [("Apple Maps", "https://maps.apple.com/?daddr=x"), ("Google Maps", "https://www.google.com/maps/dir/?api=1"),
            ("Waze", "https://waze.com/ul?q=x")]
    # Apple's own first choice is route 2, which has a closure on it; Google's first choice is route 1
    closed = Incident("road closed", "x", allowance=480)
    options = [option(("Google Maps", 1500), ("Apple Maps", 1560)), option(("Apple Maps", 1400), ("Google Maps", 1700),
                                                                           incidents=[closed])]
    order, why = Router.best_app_first(apps, options)
    assert [name for name, _ in order] == ["Google Maps", "Apple Maps", "Waze"]
    assert why == ("Opening Google Maps this time, not Apple Maps: route 1 is Google Maps's own first choice, while "
                   "Apple Maps would take route 2.").replace("Maps's", "Maps'")
    # no switch: the usual app already takes the best route, the difference is small, or nothing else would take it
    assert Router.best_app_first(apps, [options[0], option(("Apple Maps", 1900), ("Google Maps", 1400))]) == (apps, "")
    close = [option(("Google Maps", 1500), ("Apple Maps", 1560)), option(("Apple Maps", 1500), ("Google Maps", 1700))]
    assert Router.best_app_first(apps, close) == (apps, "")
    only_tomtom = [option(("TomTom", 1200)), option(("Apple Maps", 1900), ("Google Maps", 1950))]
    assert Router.best_app_first(apps, only_tomtom) == (apps, "")
    assert Router.best_app_first(apps[:1], options) == (apps[:1], "") and Router.best_app_first(apps, options[:1]) == (apps, "")
    assert Router.best_app_first(apps[1:], options) == (apps[1:], "")          # Waze has no times to compare


def test_links_for_every_navigation_app():
    here, home, airport = Place.current(HERE), Place.from_text("12 Elm St"), Place.from_text("Portland Airport, OR")
    assert nav_link("waze", here, airport) == "https://waze.com/ul?q=Portland+Airport%2C+OR&navigate=yes"
    assert nav_link("waze", here, Place.from_text("45.59,-122.59")) == "https://waze.com/ul?ll=45.590000%2C-122.590000&navigate=yes"
    assert [name for name, _ in nav_links(["apple", "google", "waze"], here, airport)] == ["Apple Maps", "Google Maps", "Waze"]
    assert [name for name, _ in nav_links(["waze", "google"], home, airport)] == ["Google Maps"]     # Waze starts from here only
    assert [name for name, _ in nav_links(["waze", "apple"], here, airport, "walk")] == ["Apple Maps"]
    assert link_name("https://waze.com/ul?q=x") == "Waze" and link_name("https://evil.example/maps") == "" and link_name("") == ""
    assert phone_links([]) == {"open": None}
    assert phone_links(["https://maps.apple.com/?daddr=x"]) == {"open": "https://maps.apple.com/?daddr=x"}
    two = phone_links(["https://www.google.com/maps/dir/?api=1&destination=a", "https://waze.com/ul?q=a",
                       "https://www.google.com/maps/dir/?api=1&destination=b", "https://example.com/other"])
    assert two == {"open": "https://www.google.com/maps/dir/?api=1&destination=a",
                   "links": {"Google Maps": "https://www.google.com/maps/dir/?api=1&destination=a", "Waze": "https://waze.com/ul?q=a"}}


def test_waze_as_the_main_app_falls_back_when_it_cannot_do_the_trip(tmp_path):
    r = Router(make_settings(tmp_path, maps_app="waze"), {}, mapkit_exe=tmp_path / "none")
    c = ctx(tmp_path, location=HERE)
    out = r.tool({"destination": "PDX"}, c)
    assert c.links == ["https://waze.com/ul?q=PDX&navigate=yes"] and "The navigation link opens Waze" in out.content
    c = ctx(tmp_path)
    out = r.tool({"destination": "PDX", "origin": "12 Elm St", "mode": "walk"}, c)
    assert c.links == ["https://maps.apple.com/?daddr=PDX&saddr=12+Elm+St&dirflg=w"] and "opens Apple Maps" in out.content
    android = Router(make_settings(tmp_path, maps_app="waze", phone_type="android"), {}, mapkit_exe=tmp_path / "none")
    assert android.links(Place.from_text("A"), Place.from_text("B"), "drive")[0][0] == "Google Maps"


def test_steps_are_shortened_for_long_trips():
    assert short_distance(None) == "" and short_distance(10) == "" and short_distance(30) == "100 ft"
    assert short_distance(120) == "400 ft" and short_distance(400) == "0.2 mi"
    assert short_distance(120, "metric") == "120 m" and short_distance(2500, "metric") == "2.5 km"
    few = [Step("Head north on 1st Ave.", 200), Step("", 5), Step("Arrive", None)]
    assert brief_steps(few) == ["1) Head north on 1st Ave (0.1 mi)", "2) Arrive"]
    many = [Step(f"Step {n}", 100 * n) for n in range(1, 31)]
    lines = brief_steps(many, limit=8)
    assert lines[:4] == ["1) Step 1 (350 ft)", "2) Step 2 (0.1 mi)", "(22 shorter steps)", "25) Step 25 (1.6 mi)"]
    assert lines[-1] == "30) Step 30 (1.9 mi)" and len(lines) == 9
    assert sum(1 for line in lines if not line.startswith("(")) == 8


def test_maps_settings(tmp_path):
    from argus import config
    path = tmp_path / "config.toml"
    path.write_text('[maps]\napp = "waze"\napps = ["apple", "google"]\ntraffic = "tomtom"\nnews = false\n')
    s = config.load(path)
    assert (s.maps_app, s.maps_apps, s.maps_traffic, s.maps_news) == ("waze", ["apple", "google"], "tomtom", False)
    assert s.maps_nav_apps == ["waze", "apple", "google"]
    assert config.Settings().maps_nav_apps == ["apple"] and config.Settings().maps_news is True
    assert config.Settings(phone_type="android", maps_apps=["google", "waze"]).maps_nav_apps == ["google", "waze"]
    for text, message in (('[maps]\napps = ["bing"]\n', "maps.apps takes"), ('[maps]\napps = "waze"\n', "maps.apps must be a list"),
                          ('[maps]\nnews = "yes"\n', "maps.news must be true or false")):
        path.write_text(text)
        with pytest.raises(ValueError, match=message.replace(".", r"\.")):
            config.load(path)


def test_saved_news_is_read_instead_of_the_web(tmp_path, monkeypatch):
    saved = tmp_path / "feed.xml"
    saved.write_bytes(feed(("Crash closes I-5 north of downtown", "KGW", 1)))
    monkeypatch.setenv("ARGUS_NEWS_OFFLINE", str(saved))
    r = Router(make_settings(tmp_path, maps_news=True), {"GOOGLE_MAPS_API_KEY": "k"}, request=routed_to(world()),
               mapkit_exe=tmp_path / "none", clock=lambda: 1_800_000_000.0)
    found = r.compare(Place.current(HERE), Place.from_text("Portland Airport, OR"))
    assert found.news_checked and [h.source for o in found.options for h in o.headlines] == ["KGW"]
    monkeypatch.delenv("ARGUS_NEWS_OFFLINE")
    assert maps._offline_news() is None
