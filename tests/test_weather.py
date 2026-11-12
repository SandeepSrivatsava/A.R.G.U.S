"""Weather where you are and where you're going (weather.py): the forecast and official warnings against a
stand-in service, places, "here" from the phone (or its last place at the Mac), the weather on a route, and
the trip watch that speaks up the evening before and an hour before leaving."""
import json
import time
from datetime import datetime

import pytest

from argus import maps, weather
from argus.maps import Place, Route, Router
from argus.scheduler import Scheduler
from argus.store import Store
from argus.toolbox import Context, schedule
from argus.weather import (Alert, RecentPlace, Weather, WeatherError, WeatherTools, describe_trip, make_trip_check,
                           read_trip, rounded, trip_request)
from fakes import make_settings
from web_fakes import EAST_WAY, END, START, JsonService, MapsWorld, apple_reply, routed_to

NOW = 1_800_000_000          # Fri 15 Jan 2027, 08:00 UTC = 00:00 in Portland (UTC-8)
OFFSET = -8 * 3600
DAY0 = NOW                   # local midnight there
HERE = {"lat": 45.52314, "lon": -122.67651}


def open_meteo(hours=72, rainy_from=None, code_at=None, days=4):
    """A forecast as Open-Meteo sends it (timeformat=unixtime): hourly from local midnight."""
    times = [DAY0 + 3600 * i for i in range(hours)]
    code = [(code_at or {}).get(i, 2 if (rainy_from is None or i < rainy_from) else 63) for i in range(hours)]
    return {"utc_offset_seconds": OFFSET, "timezone": "America/Los_Angeles",
            "current": {"time": NOW + 900, "temperature_2m": 41.4, "apparent_temperature": 35.0, "weather_code": 3,
                        "precipitation": 0.0, "wind_speed_10m": 6.2, "wind_gusts_10m": 20.1},
            "hourly": {"time": times, "temperature_2m": [40 + (i % 24) / 2 for i in range(hours)],
                       "apparent_temperature": [40 + (i % 24) / 2 for i in range(hours)],
                       "precipitation_probability": [70 if c == 63 else 5 for c in code],
                       "precipitation": [0.05 if c == 63 else 0 for c in code], "snowfall": [0.0] * hours,
                       "weather_code": code, "wind_speed_10m": [8.0] * hours, "wind_gusts_10m": [12.0] * hours},
            "daily": {"time": [DAY0 + 86400 * d for d in range(days)], "weather_code": [61, 3, 0, 73][:days],
                      "temperature_2m_max": [52.0, 49.5, 55, 33][:days], "temperature_2m_min": [38.2, 36, 40, 25][:days],
                      "precipitation_probability_max": [80, 20, 0, 90][:days],
                      "precipitation_sum": [0.31, 0, 0, 0.1][:days], "snowfall_sum": [0, 0, 0, 2.4][:days],
                      "wind_gusts_10m_max": [35.0, 12, 10, 40][:days],
                      "sunrise": [DAY0 + 86400 * d + 7.8 * 3600 for d in range(days)],
                      "sunset": [DAY0 + 86400 * d + 16.75 * 3600 for d in range(days)]}}


PLACES = {"Howell": [{"name": "Howell", "latitude": 40.17, "longitude": -74.2, "admin1": "New Jersey", "country": "United States",
                      "country_code": "US"},
                     {"name": "Howell", "latitude": 42.6073, "longitude": -83.9294, "admin1": "Michigan",
                      "country": "United States", "country_code": "US"}],
          "Tahoe City": [{"name": "Tahoe City", "latitude": 39.17, "longitude": -120.14, "admin1": "California",
                          "country": "United States", "country_code": "US"}]}
WARNING = {"features": [
    {"properties": {"status": "Actual", "severity": "Severe", "event": "Winter Storm Warning",
                    "headline": "Winter Storm Warning issued for the Sierra", "ends": "2027-01-16T10:00:00-08:00"}},
    {"properties": {"status": "Actual", "severity": "Minor", "event": "Frost Advisory", "headline": "frost"}},
    {"properties": {"status": "Test", "severity": "Extreme", "event": "Test Message", "headline": "ignore"}},
    {"properties": {"status": "Actual", "severity": "Severe", "event": "Winter Storm Warning", "headline": "again"}}]}


class WeatherWorld(JsonService):
    """Open-Meteo (forecast, places) and weather.gov behind one address."""

    def __init__(self, forecast=None, alerts=None, fail=()):
        self.forecast, self.alerts, self.fail = forecast or open_meteo(), alerts, set(fail)
        super().__init__(self.answer)

    def answer(self, request):
        if request["path"] == "/v1/forecast":
            return (400, {"error": True, "reason": "Latitude must be in range"}) if "forecast" in self.fail else (200, self.forecast)
        if request["path"] == "/v1/search":
            return (500, {}) if "places" in self.fail else (200, {"results": PLACES.get(request["query"]["name"], [])})
        if request["path"] == "/alerts/active":
            return (503, {}) if "alerts" in self.fail else (200, self.alerts or {"features": []})
        return 404, {}

    def asked(self, path):
        return [r for r in self.requests if r["path"] == path]


def make_weather(world, *, units="imperial", geocode=None, clock=lambda: NOW):
    return Weather(units=units, request=routed_to(world), geocode=geocode, clock=clock)


def ctx(tmp_path, store=None, **kw):
    return Context(channel=kw.pop("channel", "voice"), workspace=tmp_path, store=store,
                   timezone="America/Los_Angeles", **kw)


# ---- the forecast in words -----------------------------------------------------------------------------------
def test_now_today_and_tomorrow(tmp_path):
    world = WeatherWorld()
    text = make_weather(world).report((45.52314, -122.67651), "where the user is now")
    lines = text.split("\n")
    assert lines[0] == "Weather for where the user is now (Open-Meteo forecast; local times there):"
    assert lines[1] == "Now: 41°F (feels like 35°F), overcast, wind 6, gusts 20 mph."
    assert lines[2].startswith("Next hours: 03:00: 42°F, partly cloudy, wind 8 mph. 06:00: 43°F")
    assert lines[3] == ("Today Fri 15 Jan: 38°F to 52°F, light rain, up to 80% chance of rain or snow, 0.31 in in all "
                        "(gusts to 35 mph); sunset 16:45.")
    assert lines[4].startswith("Tomorrow Sat 16 Jan: 36°F to 50°F, overcast, up to 20% chance of rain or snow;")
    sent = world.asked("/v1/forecast")[0]["query"]
    assert (sent["latitude"], sent["longitude"]) == ("45.52", "-122.68")        # about a kilometre, not the spot
    assert sent["temperature_unit"] == "fahrenheit" and sent["timeformat"] == "unixtime" and sent["timezone"] == "auto"
    assert world.asked("/alerts/active")[0]["query"] == {"point": "45.52,-122.68"}
    assert world.asked("/alerts/active")[0]["headers"]["user-agent"] == "Argus personal assistant"
    assert world.asked("/alerts/active")[0]["headers"]["accept"] == "application/geo+json"
    assert "45.523" not in text and "122.676" not in text


def test_a_time_a_whole_day_warnings_and_metric(tmp_path):
    world = WeatherWorld(forecast=open_meteo(rainy_from=34), alerts=WARNING)
    w = make_weather(world)
    at = DAY0 + 34 * 3600                                       # Sat 10:00 there
    text = w.report((39.17, -120.14), "Tahoe City, California, United States", at, purpose="skiing")
    assert "Around 10:00 on Sat 16 Jan: 45°F, rain, 70% chance of rain or snow, wind 8 mph." in text
    assert "After that: 12:00:" in text and "The whole day Sat 16 Jan:" in text
    assert ("Official warning (US National Weather Service, in force now): Winter Storm Warning until Sat 10:00. "
            "Winter Storm Warning issued for the Sierra") in text
    assert text.count("Official warning") == 1                  # minor, test and repeated ones are left out
    assert text.endswith("The user is going for: skiing. Say plainly whether this weather suits it, and what to "
                         "bring or change.")
    day = w.report((39.17, -120.14), "Tahoe", DAY0 + 3 * 86400, whole_day=True)
    assert "Mon 18 Jan: 25°F to 33°F, snow, up to 90% chance of rain or snow, 0.10 in in all, 2.4 in of snow" in day
    assert "(gusts to 40 mph)" in day and "Through the day:" not in day        # the stand-in has 3 days of hours
    metric = make_weather(WeatherWorld(), units="metric")
    world = WeatherWorld()
    metric = make_weather(world, units="metric").report((1, 2), "x")
    assert "°C" in metric and "km/h" in metric
    assert "temperature_unit" not in world.asked("/v1/forecast")[0]["query"]      # metric is Open-Meteo's default


def test_far_and_failing_forecasts():
    w = make_weather(WeatherWorld())
    with pytest.raises(WeatherError, match="more than 16 days away"):
        w.report((1, 2), "x", NOW + 17 * 86400)
    with pytest.raises(WeatherError, match="doesn't reach that time"):
        w.report((1, 2), "x", NOW + 6 * 86400)
    with pytest.raises(WeatherError, match="doesn't reach that day"):
        w.report((1, 2), "x", NOW + 8 * 86400, whole_day=True)
    with pytest.raises(WeatherError, match=r"HTTP 400 \(Latitude must be in range\)"):
        make_weather(WeatherWorld(fail={"forecast"})).forecast((1, 2))
    assert make_weather(WeatherWorld(fail={"alerts"})).alerts((1, 2)) == []
    assert make_weather(WeatherWorld()).alerts((1, 2)) == []
    down = Weather(request=lambda *a, **k: (_ for _ in ()).throw(maps.MapsError("offline")))
    assert down.alerts((1, 2)) == []
    odd = WeatherWorld(forecast={"hourly": {"time": ["x", NOW, True], "temperature_2m": ["hot"]}, "daily": [],
                                 "utc_offset_seconds": "soon"})
    fc = make_weather(odd).forecast((1, 2))
    assert fc.offset == 0 and fc.now is None and len(fc.hours) == 1 and fc.hours[0].temp is None and fc.days == []


def test_a_week_out_says_forecasts_change():
    world = WeatherWorld(forecast=open_meteo(hours=24 * 8, days=4))
    text = make_weather(world).report((1, 2), "x", DAY0 + 6 * 86400 + 12 * 3600)
    assert "forecasts this far ahead change a lot" in text and "Official warning" not in text
    assert world.asked("/alerts/active") == []                  # today's warnings say nothing about next week


# ---- places -----------------------------------------------------------------------------------------------------
def test_places_are_found_by_city_state_or_address():
    world = WeatherWorld()
    w = make_weather(world)
    assert w.locate("45.5,-122.6") == ((45.5, -122.6), "45.5,-122.6")
    assert w.locate("Howell, MI") == ((42.6073, -83.9294), "Howell, Michigan, United States")
    assert w.locate("Howell, Michigan, USA")[1] == "Howell, Michigan, United States"
    assert w.locate("Howell")[1] == "Howell, New Jersey, United States"          # no hint: the first match
    assert w.locate("12 Elm St, Howell, MI")[1] == "Howell, Michigan, United States"
    with pytest.raises(WeatherError, match="couldn't find a place called Howell, Texas"):
        w.locate("Howell, Texas")
    with pytest.raises(WeatherError, match="HTTP 500"):
        make_weather(WeatherWorld(fail={"places"})).locate("Howell")
    by_tomtom = make_weather(world, geocode=lambda text: ((42.6, -83.9), text))
    assert by_tomtom.locate("12 Elm St, Howell, MI") == ((42.6, -83.9), "12 Elm St, Howell, MI")
    assert by_tomtom.locate("Howell, MI")[1] == "Howell, Michigan, United States"     # no number: Open-Meteo

    def broken(text):
        raise maps.MapsError("TomTom refused the key")
    assert make_weather(world, geocode=broken).locate("9 Main St, Tahoe City")[1].startswith("Tahoe City")
    assert make_weather(world, geocode=lambda t: None).locate("9 Main St, Tahoe City")[1].startswith("Tahoe City")
    assert rounded((45.52314, -122.67651)) == (45.52, -122.68)


# ---- the weather tool -------------------------------------------------------------------------------------------
def test_here_is_the_phone_then_its_last_place_then_home(tmp_path):
    world = WeatherWorld()
    clock = [NOW]
    recent = RecentPlace(clock=lambda: clock[0])
    tools = WeatherTools(make_weather(world), home="Howell, MI", recent=recent, clock=lambda: NOW)
    out = tools.weather_tool({}, ctx(tmp_path, location=HERE))
    assert not out.is_error and out.content.startswith("Weather for where the user is now")
    assert "45.52" not in out.content and "home city" not in out.content
    asked = []
    assert not tools.weather_tool({"place": "here"}, ctx(tmp_path, locate=lambda: asked.append(1) or HERE)).is_error
    assert asked
    home = tools.weather_tool({"place": "my location"}, ctx(tmp_path))                 # nothing known yet
    assert "Weather for Howell, Michigan, United States" in home.content
    assert "(The phone's location isn't available here, so this is for the home city, Howell, MI; say so.)" in home.content
    recent.note(HERE)                                                                # the phone asked something
    clock[0] += 2 * 3600
    assert tools.weather_tool({}, ctx(tmp_path)).content.startswith("Weather for where the user is now")
    clock[0] += 2 * 3600                                                             # four hours later: too old
    assert "home city" in tools.weather_tool({}, ctx(tmp_path)).content
    recent.note(None)
    recent.note({"lat": 1.0})
    assert recent.get(10**9) == HERE

    def broken():
        raise RuntimeError("GPS off")
    assert "home city" in tools.weather_tool({}, ctx(tmp_path, locate=broken)).content
    homeless = WeatherTools(make_weather(world))
    assert "no home city is set" in homeless.weather_tool({}, ctx(tmp_path)).content


def test_guests_get_neither_where_the_owner_is_nor_the_home_city(tmp_path):
    recent = RecentPlace()
    recent.note(HERE)
    tools = WeatherTools(make_weather(WeatherWorld()), home="Howell, MI", recent=recent, clock=lambda: NOW)
    out = tools.weather_tool({}, ctx(tmp_path, channel="guest", location=HERE))
    assert out.is_error and "Ask where they are" in out.content
    assert not tools.weather_tool({"place": "Tahoe City"}, ctx(tmp_path, channel="guest")).is_error


def test_times_are_read_in_the_places_own_clock(tmp_path):
    world = WeatherWorld(forecast=open_meteo(rainy_from=34))
    tools = WeatherTools(make_weather(world), home="Howell, MI", clock=lambda: NOW)
    out = tools.weather_tool({"place": "Tahoe City, CA", "when": "2027-01-16T10:00", "purpose": "skiing"}, ctx(tmp_path))
    assert "Around 10:00 on Sat 16 Jan: 45°F, rain" in out.content and "going for: skiing" in out.content
    zoned = tools.weather_tool({"place": "Tahoe City", "when": "2027-01-16T18:00:00Z"}, ctx(tmp_path))
    assert "Around 10:00 on Sat 16 Jan" in zoned.content
    whole = tools.weather_tool({"place": "Tahoe City", "when": "2027-01-15"}, ctx(tmp_path))
    assert "Fri 15 Jan: 38°F to 52°F" in whole.content and "Through the day: 08:00:" in whole.content
    for when, message in (("next week", "give 'when' as"), ("2027-01-14T08:00", "already passed"),
                          ("2027-03-01", "more than 16 days away")):
        out = tools.weather_tool({"place": "Tahoe City", "when": when}, ctx(tmp_path))
        assert out.is_error and message in out.content, when
    assert "couldn't find a place called Atlantis" in tools.weather_tool({"place": "Atlantis"}, ctx(tmp_path)).content


# ---- watching a trip -------------------------------------------------------------------------------------------
def test_watch_a_trip_then_list_and_cancel(tmp_path):
    store = Store(tmp_path / "a.db")
    now = time.time()
    tools = WeatherTools(make_weather(WeatherWorld()), clock=lambda: now)
    c = ctx(tmp_path, store=store)
    leave = datetime.fromtimestamp(now + 3 * 86400).replace(hour=8, minute=0, second=0, microsecond=0)
    out = tools.trip_tool({"action": "watch", "destination": "Tahoe City, CA", "leave_at": leave.isoformat(),
                           "purpose": "skiing"}, c)
    assert not out.is_error and "the evening before" in out.content and "an hour before leaving" in out.content
    trips = [t for t in store.tasks() if t["mode"] == "trip"]
    assert len(trips) == 2
    evening, hour_before = sorted(trips, key=lambda t: t["next_run"])
    assert datetime.fromtimestamp(evening["next_run"]).hour == 19
    assert hour_before["next_run"] == pytest.approx(leave.timestamp() - 3600)
    trip = read_trip(hour_before["text"])
    assert trip == {"to": "Tahoe City, CA", "from": "", "leave": int(leave.timestamp()), "purpose": "skiing",
                    "mode": "drive", "stage": "an hour before leaving"}
    assert "trip to Tahoe City, CA for skiing, leaving" in tools.trip_tool({"action": "list"}, c).content
    listed = schedule({"action": "list"}, c).content
    assert "[trip] trip to Tahoe City, CA for skiing" in listed and '{"to"' not in listed
    assert tools.trip_tool({"action": "cancel", "id": evening["id"]}, c).content == "cancelled"
    assert tools.trip_tool({"action": "cancel", "id": 999}, c).is_error
    soon = (datetime.fromtimestamp(now + 5 * 3600)).isoformat(timespec="minutes")
    out = tools.trip_tool({"action": "watch", "destination": "Gresham", "leave_at": soon, "mode": "fly"}, c)
    made = [t for t in store.tasks() if t["mode"] == "trip" and "Gresham" in t["text"]]
    assert len(made) <= 2 and read_trip(made[-1]["text"])["mode"] == "drive"


@pytest.mark.parametrize("args,message", [
    ({"action": "dance"}, "action must be"), ({"action": "watch"}, "say where"),
    ({"action": "watch", "destination": "x", "leave_at": "soon"}, "ISO local date-time"),
    ({"action": "watch", "destination": "x", "leave_at": "2020-01-01T08:00"}, "within the hour"),
    ({"action": "watch", "destination": "x", "leave_at": "2099-01-01T08:00"}, "more than two months away"),
])
def test_trip_watch_checks_its_input(tmp_path, args, message):
    store = Store(tmp_path / "a.db")
    out = WeatherTools(make_weather(WeatherWorld())).trip_tool(args, ctx(tmp_path, store=store))
    assert out.is_error and message in out.content


def test_trip_watch_has_a_limit(tmp_path):
    store = Store(tmp_path / "a.db")
    for n in range(weather.MAX_TRIPS):
        store.add_task(weather.TRIP_PREFIX + "{}", "trip", due=time.time() + 9999)
    leave = datetime.fromtimestamp(time.time() + 86400 * 2).isoformat(timespec="minutes")
    out = WeatherTools(make_weather(WeatherWorld())).trip_tool(
        {"action": "watch", "destination": "x", "leave_at": leave}, ctx(tmp_path, store=store))
    assert out.is_error and "already watching" in out.content
    listed = WeatherTools(make_weather(WeatherWorld())).trip_tool({"action": "list"}, ctx(tmp_path, store=store))
    assert listed.content.count("a trip check") == weather.MAX_TRIPS


def test_trip_texts():
    assert read_trip("Reminder: call home") is None and read_trip("trip:{not json") is None
    assert read_trip('trip:{"to": "x"}') is None and describe_trip("trip:[]") == "a trip check"
    trip = {"to": "Tahoe City", "from": "", "leave": int(datetime(2027, 1, 16, 8, 0).timestamp()), "purpose": "",
            "mode": "drive", "stage": "an hour before leaving"}
    asked = trip_request(trip, "Sandeep")
    assert asked.startswith("Trip check (an hour before leaving): Sandeep is going to Tahoe City from their current "
                            "location, leaving Sat 16 Jan at 08:00, by drive. They're going for: not said.")
    assert "and the route tool for the live traffic" in asked
    evening = trip_request(dict(trip, stage="the evening before", **{"from": "Howell, MI"}, purpose="skiing"), "Sandeep")
    assert "from Howell, MI" in evening and "route tool" not in evening and "going for: skiing" in evening


def test_the_scheduler_speaks_trip_checks(tmp_path):
    store = Store(tmp_path / "a.db")
    said, checked = [], []
    sched = Scheduler(store, deliver=said.append, run_task=lambda t: "x", headlines=lambda: "h",
                      trip_check=lambda trip: checked.append(trip) or f"Still good for {trip['purpose']}.")
    now = time.time()
    good = {"to": "Tahoe", "leave": int(now + 3600), "purpose": "skiing", "stage": "an hour before leaving"}
    store.add_task(weather.TRIP_PREFIX + json.dumps(good), "trip", due=now - 1)
    store.add_task(weather.TRIP_PREFIX + json.dumps(dict(good, leave=int(now - 7200))), "trip", due=now - 1)   # long gone
    store.add_task("trip:junk", "trip", due=now - 1)
    assert sched.tick(now) == 3
    assert said == ["Still good for skiing."] and len(checked) == 1
    assert [t for t in store.tasks() if t["mode"] == "trip"] == []
    store.add_task(weather.TRIP_PREFIX + json.dumps(good), "trip", due=now - 1)
    assert Scheduler(store, deliver=said.append, run_task=str, headlines=str).tick(now) == 1   # no checker: skipped


def test_a_trip_check_runs_on_its_own_channel(tmp_path):
    store = Store(tmp_path / "a.db")
    seen = []

    class FakeAgent:
        def run(self, **kw):
            seen.append(kw)

            class Turn:
                text, error = "  Clear roads and fresh snow: go.  ", None
            return Turn()
    check = make_trip_check(FakeAgent(), store, "Sandeep")
    assert check({"to": "Tahoe", "leave": int(time.time() + 3600), "purpose": "skiing"}) == "Clear roads and fresh snow: go."
    assert seen[0]["channel"] == "trip" and "Trip check" in seen[0]["content"]

    class Empty(FakeAgent):
        def run(self, **kw):
            class Turn:
                text, error = "", "Claude didn't answer"
            return Turn()
    assert "couldn't be checked (Claude didn't answer)" in make_trip_check(Empty(), store, "")({"to": "Tahoe", "leave": 1})


def test_trip_checks_get_only_the_weather_and_route_tools(tmp_path):
    from argus.toolbox import build_tools
    s = make_settings(tmp_path)
    tools = build_tools(mac=False, glasses=False, commands=False)
    r = Router(s, {}, mapkit_exe=tmp_path / "none")
    tools["route"] = maps.route_tool(r)
    weather.attach(s, r, tools)
    assert sorted(name for name, tool in tools.items() if "trip" in tool.channels) == ["route", "weather"]
    assert "watch_trip" in tools and "guest" in tools["weather"].channels and "guest" not in tools["watch_trip"].channels
    from argus.prompts import system_blocks
    text = "\n".join(b["text"] for b in system_blocks(user="Sandeep", city="Howell, MI", timezone="America/Detroit",
                                                      workspace="/w", channel="trip"))
    assert "a trip check running by itself" in text and "Use the weather tool (and the route tool when asked)" in text
    voice = "\n".join(b["text"] for b in system_blocks(user="Sandeep", city="x", timezone="y", workspace="/w", channel="voice"))
    assert "use the weather tool for every weather question" in voice and "watch_trip" in voice


# ---- the weather on a route, and "here" at the Mac ----------------------------------------------------------------
def test_routes_say_the_weather_where_you_arrive_and_halfway(tmp_path):
    long_way = [START, (45.5231, -121.0), (45.59, -120.5), END]
    maps_world = MapsWorld(apple=apple_reply((5400, 160_000, "I-84", long_way)))
    weather_world = WeatherWorld(forecast=open_meteo(code_at={9: 71}), alerts=WARNING)
    s = make_settings(tmp_path)
    leave = NOW + 7 * 3600 + 600                                  # 07:10 there; 90 minutes on the road
    r = Router(s, {"APPLE_MAPS_TOKEN": "jwt"}, request=routed_to(maps_world), mapkit_exe=tmp_path / "none",
               clock=lambda: leave)
    r.weather = make_weather(weather_world, clock=lambda: leave)
    out = r.tool({"destination": "Hood River, OR", "origin": "12 Elm St, Portland"}, ctx(tmp_path))
    text = out.content
    assert "Weather when you arrive (about 08:40 there): 44°F, light snow, wind 8 mph. Take care on the road." in text
    assert "Weather halfway (about 07:55 there): 44°F, partly cloudy, wind 8 mph." in text
    assert "Official warning at the destination (US National Weather Service): Winter Storm Warning." in text
    sent = weather_world.asked("/v1/forecast")
    assert {(q["query"]["latitude"], q["query"]["longitude"]) for q in sent} >= {("45.59", "-122.59")}
    r.weather = make_weather(WeatherWorld(fail={"forecast"}))
    assert "Weather when you arrive" not in r.tool({"destination": "x", "origin": "12 Elm St"}, ctx(tmp_path)).content
    s2 = Router(s, {"APPLE_MAPS_TOKEN": "jwt"}, request=routed_to(MapsWorld(apple={"routes": [
        {"name": "I-5", "durationSeconds": 600, "distanceMeters": 5000}]})), mapkit_exe=tmp_path / "none", clock=lambda: NOW)
    s2.weather = make_weather(WeatherWorld(), clock=lambda: NOW)
    text = s2.tool({"destination": "Howell, MI", "origin": "12 Elm St"}, ctx(tmp_path)).content     # no shape: by name
    assert "Weather when you arrive" in text


def test_at_the_mac_here_is_where_the_phone_was_minutes_ago(tmp_path):
    clock = [NOW]
    recent = RecentPlace(clock=lambda: clock[0])
    s = make_settings(tmp_path)
    r = Router(s, {}, mapkit_exe=tmp_path / "none", clock=lambda: clock[0])
    r.recent = recent
    assert r.tool({"destination": "PDX"}, ctx(tmp_path)).is_error
    recent.note(HERE)
    c = ctx(tmp_path)
    out = r.tool({"destination": "PDX"}, c)
    assert not out.is_error and c.links == ["https://maps.apple.com/?daddr=PDX&dirflg=d"]
    assert r.tool({"destination": "PDX"}, ctx(tmp_path, channel="guest")).is_error
    clock[0] += 21 * 60
    assert r.tool({"destination": "PDX"}, ctx(tmp_path)).is_error          # too old to start a route from


def test_attach_adds_the_tools_and_finds_addresses_with_tomtom(tmp_path):
    from argus.traffic import TomTom
    s = make_settings(tmp_path, maps_units="metric")
    r = Router(s, {"TOMTOM_API_KEY": "tt"}, mapkit_exe=tmp_path / "none")
    seen = []
    r.services["tomtom"] = type("T", (TomTom,), {"locate": lambda self, place, near=None: seen.append(place.text) or (1.0, 2.0)})("tt")
    tools = {}
    w, recent = weather.attach(s, r, tools, request=routed_to(WeatherWorld()))
    assert set(tools) == {"weather", "watch_trip"} and r.weather is w and r.recent is recent and w.units == "metric"
    assert w.locate("12 Elm St, Howell, MI") == ((1.0, 2.0), "12 Elm St, Howell, MI") and seen == ["12 Elm St, Howell, MI"]
    plain = Router(s, {}, mapkit_exe=tmp_path / "none")
    w2, _ = weather.attach(s, plain, {}, request=routed_to(WeatherWorld()))
    assert w2.extra_geocode is None


def test_passed_headers_win_over_the_defaults():
    service = JsonService(lambda request: (200, {}))
    maps.http_json("GET", service.url + "/x", {"Accept": "application/geo+json", "User-Agent": "Argus personal assistant"})
    sent = service.requests[0]["headers"]
    assert sent["accept"] == "application/geo+json" and sent["user-agent"] == "Argus personal assistant"
    maps.http_json("GET", service.url + "/y", {})
    assert service.requests[1]["headers"]["user-agent"] == "Argus/2"


def test_the_phone_gateway_remembers_where_the_phone_was(tmp_path):
    from argus.server import ServerApp
    app = ServerApp.__new__(ServerApp)
    app.recent = RecentPlace()
    app.store = Store(tmp_path / "a.db")

    class FakeAgent:
        def run(self, **kw):
            class Turn:
                text, error, links = "ok", None, []
            return Turn()
    app.agent = FakeAgent()
    assert app.ask_phone("weather?", location=HERE)["reply"] == "ok"
    assert app.recent.get(60) == HERE


def test_alert_and_place_helpers():
    assert Alert("Flood Watch", "x", "Moderate").ends == ""
    assert maps.Route(1).path == [] and Route(1, source="x").source == "x" and Place.from_text("1,2").has_point
    assert EAST_WAY[0] == START
