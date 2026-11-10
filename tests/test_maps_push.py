"""Directions with live traffic (Google Routes, Apple Maps Server API, MapKit on the Mac), navigation links,
the route tool, the Apple Maps token maker, and push notifications (ntfy)."""
import base64
import json
import shutil
import subprocess
import time

import pytest

from argus import maps
from argus.maps import (AppleMapsServer, GoogleRoutes, MapKitHelper, MapsError, Place, Router, clean_location,
                        der_to_raw, distance, make_apple_maps_token, minutes, nav_link, parse_latlon, route_tool)
from argus.push import Push, check_server, new_topic, push_from
from argus.toolbox import Context
from fakes import make_settings
from web_fakes import JsonService, routed_to

HERE = {"lat": 45.5231, "lon": -122.6765, "accuracy": 12.0}


def ctx(tmp_path, **kw):
    return Context(channel="phone", workspace=tmp_path, store=None, timezone="America/Los_Angeles", **kw)


def google_reply(*routes, status=200):
    def handler(request):
        return status, {"routes": list(routes)} if status == 200 else routes[0]
    return handler


# ---- places, links, numbers --------------------------------------------------------------------------------
def test_locations_from_phones_are_cleaned_up():
    assert clean_location({"lat": 45.52314, "lon": -122.67651, "accuracy": "12.4", "speed": -1}) == \
        {"lat": 45.52314, "lon": -122.67651, "accuracy": 12.4}
    assert clean_location({"latitude": "45.5", "longitude": "-122.6", "speed": 22.3}) == \
        {"lat": 45.5, "lon": -122.6, "speed": 22.3}
    assert clean_location("45.5, -122.6") == {"lat": 45.5, "lon": -122.6}
    for bad in (None, "", "Portland", {"lat": 91, "lon": 0}, {"lat": "x", "lon": 1}, {"lat": 0, "lon": 0},
                {"lat": float("nan"), "lon": 1}, [45, -122], "200,10"):
        assert clean_location(bad) is None, bad
    assert parse_latlon("45.5,-122.6") == (45.5, -122.6) and parse_latlon("45.5") is None
    assert clean_location({"lat": 1, "lon": 2, "accuracy": float("inf")}) == {"lat": 1.0, "lon": 2.0}


def test_navigation_links_never_carry_the_current_location():
    here, airport = Place.current(HERE), Place.from_text("Portland Airport, OR")
    apple = nav_link("apple", here, airport)
    assert apple == "https://maps.apple.com/?daddr=Portland+Airport%2C+OR&dirflg=d" and "45.52" not in apple
    google = nav_link("google", here, airport, "transit")
    assert google == "https://www.google.com/maps/dir/?api=1&destination=Portland+Airport%2C+OR&travelmode=transit"
    from_home = nav_link("apple", Place.from_text("12 Elm St"), Place.from_text("45.6,-122.5"), "bike")
    assert "saddr=12+Elm+St" in from_home and "daddr=45.600000%2C-122.500000" in from_home and "dirflg" not in from_home
    assert "origin=12+Elm+St" in nav_link("google", Place.from_text("12 Elm St"), airport, "walk")
    assert here.spoken() == "your current location" and Place.from_text("1,2").spoken() == "1,2"
    assert Place(lat=1.0, lon=2.0).spoken() == "1.000000,2.000000"


def test_times_and_distances_read_naturally():
    assert minutes(30) == "1 min" and minutes(25 * 60) == "25 min" and minutes(3600) == "1 h"
    assert minutes(3600 + 7 * 60) == "1 h 7 min"
    assert distance(19800) == "12.3 mi" and distance(19800, "metric") == "19.8 km"
    assert distance(400_000) == "249 mi" and distance(400_000, "metric") == "400 km" and distance(None) == ""
    assert maps._seconds("1234s") == 1234 and maps._seconds("12.5s") == 12 and maps._seconds(90.0) == 90
    assert maps._seconds("soon") is None and maps._seconds(True) is None and maps._seconds(-5) is None


# ---- Google Routes API ---------------------------------------------------------------------------------------
def test_google_routes_request_and_answer():
    service = JsonService(google_reply(
        {"duration": "1700s", "staticDuration": "1300s", "distanceMeters": 19800, "description": "I-5 N",
         "routeLabels": ["DEFAULT_ROUTE"], "warnings": ["This route has tolls."]},
        {"duration": "1500s", "staticDuration": "1450s", "distanceMeters": 24000, "description": "I-205 N",
         "routeLabels": ["DEFAULT_ROUTE_ALTERNATE", "FUEL_EFFICIENT"]},
        {"distanceMeters": 5}))                       # a route without a time is skipped
    google = GoogleRoutes("AIza-test-key", url=service.url + "/directions/v2:computeRoutes")
    found = google.routes(Place.current(HERE), Place.from_text("PDX"), "drive", depart=2_000_000_000.0,
                          avoid=["tolls", "ferries"])
    sent = service.requests[0]
    assert sent["method"] == "POST" and sent["headers"]["x-goog-api-key"] == "AIza-test-key"
    assert sent["headers"]["x-goog-fieldmask"] == maps.GOOGLE_FIELDS
    body = sent["body"]
    assert body["origin"] == {"location": {"latLng": {"latitude": 45.5231, "longitude": -122.6765}}}
    assert body["destination"] == {"address": "PDX"} and body["travelMode"] == "DRIVE"
    assert body["routingPreference"] == "TRAFFIC_AWARE" and body["computeAlternativeRoutes"] is True
    assert body["routeModifiers"] == {"avoidTolls": True, "avoidFerries": True}
    assert body["departureTime"] == "2033-05-18T03:33:20Z" and body["units"] == "IMPERIAL"
    assert [(r.via, r.seconds, r.usual_seconds) for r in found] == [("I-5 N", 1700, 1300), ("I-205 N", 1500, 1450)]
    assert found[1].labels == ["uses the least fuel"] and found[0].notes == ["This route has tolls."]
    walk = GoogleRoutes("k", url=service.url + "/r", units="metric")
    walk.routes(Place.from_text("A"), Place.from_text("B"), "walk")
    body = service.requests[-1]["body"]
    assert body["travelMode"] == "WALK" and "routingPreference" not in body and body["units"] == "METRIC"


@pytest.mark.parametrize("status,error,expected", [
    (403, {"error": {"message": "Routes API has not been used in project"}}, "Routes API is enabled"),
    (400, {"error": {"message": "Invalid destination"}}, "couldn't plan that trip (Invalid destination)"),
    (429, {}, "request limit"),
    (500, {"error": {"message": "Internal"}}, "HTTP 500 (Internal)"),
])
def test_google_routes_errors_say_what_to_do(status, error, expected):
    service = JsonService(lambda request: (status, error))
    with pytest.raises(MapsError, match=expected.replace("(", r"\(").replace(")", r"\)")):
        GoogleRoutes("k", url=service.url + "/x").routes(Place.from_text("A"), Place.from_text("B"))


def test_unreachable_service_is_a_maps_error():
    with pytest.raises(MapsError, match="couldn't reach 127.0.0.1"):
        maps.http_json("GET", "http://127.0.0.1:9/nothing", {}, timeout=2)


def test_non_json_reply_is_kept_readable():
    service = JsonService(lambda request: (502, b"<html>bad gateway</html>"))
    status, data = maps.http_json("GET", service.url + "/x", {})
    assert status == 502 and "bad gateway" in data["raw"]


# ---- Apple Maps Server API ---------------------------------------------------------------------------------
def apple_service(routes=None, token_status=200, directions_status=None):
    issued = []

    def handler(request):
        if request["path"] == "/v1/token":
            issued.append(request["headers"]["authorization"])
            if token_status != 200:
                return token_status, {"error": {"message": "Unauthorized"}}
            return 200, {"accessToken": f"access-{len(issued)}", "expiresInSeconds": 1800}
        if directions_status:
            status = directions_status.pop(0) if isinstance(directions_status, list) else directions_status
            if status != 200:
                return status, {"error": {"message": "Not allowed"}}
        return 200, {"routes": routes if routes is not None else [
            {"name": "OR-99E", "durationSeconds": 1600, "distanceMeters": 21000, "hasTolls": True},
            {"name": "I-5", "durationSeconds": 1400, "distanceMeters": 20000, "staticTravelTimeSeconds": 1100}]}
    service = JsonService(handler)
    return service, issued


def test_apple_maps_swaps_the_token_and_asks_for_alternatives():
    service, issued = apple_service()
    clock = [1000.0]
    apple = AppleMapsServer("maps-auth-jwt", base=service.url, clock=lambda: clock[0])
    found = apple.routes(Place.current(HERE), Place.from_text("Portland Airport"), "drive", depart=2_000_000_000.0,
                         avoid=["tolls"])
    assert issued == ["Bearer maps-auth-jwt"]
    directions = service.requests[-1]
    assert directions["headers"]["authorization"] == "Bearer access-1"
    assert directions["query"] == {"origin": "45.523100,-122.676500", "destination": "Portland Airport",
                                   "transportType": "Automobile", "requestsAlternateRoutes": "true", "lang": "en-US",
                                   "departureDate": "2033-05-18T03:33:20Z", "avoid": "Tolls"}
    assert [(r.via, r.seconds, r.usual_seconds, r.notes) for r in found] == [
        ("OR-99E", 1600, None, ["has tolls"]), ("I-5", 1400, 1100, [])]
    apple.routes(Place.from_text("A"), Place.from_text("B"), "walk")
    assert len(issued) == 1                                   # the access token is reused while it's fresh
    clock[0] += 1800
    apple.routes(Place.from_text("A"), Place.from_text("B"))
    assert len(issued) == 2 and service.requests[-1]["query"]["transportType"] == "Automobile"


def test_apple_maps_retries_once_with_a_fresh_access_token():
    service, issued = apple_service(directions_status=[401, 200])
    apple = AppleMapsServer("jwt", base=service.url)
    assert apple.routes(Place.from_text("A"), Place.from_text("B"))[0].via == "OR-99E"
    assert len(issued) == 2


def test_apple_maps_errors():
    service, _ = apple_service(token_status=401)
    with pytest.raises(MapsError, match="only last 7 days"):
        AppleMapsServer("jwt", base=service.url).routes(Place.from_text("A"), Place.from_text("B"))
    service, _ = apple_service(token_status=500)
    with pytest.raises(MapsError, match="sign-in failed"):
        AppleMapsServer("jwt", base=service.url).access_token()
    service, _ = apple_service(directions_status=403)
    with pytest.raises(MapsError, match="HTTP 403 \\(Not allowed\\)"):
        AppleMapsServer("jwt", base=service.url).routes(Place.from_text("A"), Place.from_text("B"))
    odd = JsonService(lambda r: (200, {"accessToken": "a", "expiresInSeconds": "soon"}) if r["path"] == "/v1/token"
                      else (200, {"routes": [{"name": "x"}, {"name": "y", "expectedTravelTimeSeconds": 60}]}))
    assert [r.via for r in AppleMapsServer("jwt", base=odd.url).routes(Place.from_text("A"), Place.from_text("B"))] == ["y"]


# ---- MapKit on the Mac -----------------------------------------------------------------------------------------
def test_mapkit_helper(tmp_path):
    exe = tmp_path / "argus-maps"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    seen = []

    def run(args, **kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({"routes": [
            {"name": "I-84 W", "seconds": 1260, "meters": 18000, "notes": ["Construction"]}, {"name": "bad"}]}), "")
    helper = MapKitHelper(exe, run=run)
    assert helper.available()
    found = helper.routes(Place.current(HERE), Place.from_text("Gresham"), "drive", 2_000_000_000.0, ["tolls", "ferries"])
    assert seen[0] == [str(exe), "--from", "45.523100,-122.676500", "--to", "Gresham", "--mode", "drive",
                       "--depart", "2000000000", "--avoid-tolls"]
    assert [(r.via, r.seconds, r.meters, r.notes) for r in found] == [("I-84 W", 1260, 18000, ["Construction"])]
    for reply, message in ((subprocess.TimeoutExpired("x", 40), "didn't answer"), ("not json", "couldn't read"),
                           ("[1]", "couldn't read"), (json.dumps({"error": "No route found"}), "No route found")):
        def bad(args, _reply=reply, **kw):
            if isinstance(_reply, Exception):
                raise _reply
            return subprocess.CompletedProcess(args, 1, _reply, "")
        with pytest.raises(MapsError, match=message):
            MapKitHelper(exe, run=bad).routes(Place.from_text("A"), Place.from_text("B"))
    assert not MapKitHelper(tmp_path / "missing").available()


# ---- choosing the service, the route tool --------------------------------------------------------------------
def router(tmp_path, *, phone="iphone", traffic="auto", secrets=None, service=None, mapkit=False):
    s = make_settings(tmp_path, phone_type=phone, maps_traffic=traffic)
    exe = tmp_path / "bin-argus-maps"
    exe.unlink(missing_ok=True)
    if mapkit:
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    request = routed_to(service) if service is not None else maps.http_json
    return Router(s, secrets or {}, request=request, mapkit_exe=exe,
                  run=lambda args, **kw: subprocess.CompletedProcess(args, 0, json.dumps(
                      {"routes": [{"name": "via MapKit", "seconds": 600, "meters": 5000}]}), ""))


def test_the_traffic_service_follows_the_phone(tmp_path):
    both = {"GOOGLE_MAPS_API_KEY": "AIza", "APPLE_MAPS_TOKEN": "jwt"}
    assert router(tmp_path, secrets=both).service().name == "Apple Maps"
    assert isinstance(router(tmp_path, secrets=both).service(), AppleMapsServer)
    assert isinstance(router(tmp_path, phone="android", secrets=both).service(), GoogleRoutes)
    assert isinstance(router(tmp_path, secrets={"GOOGLE_MAPS_API_KEY": "AIza"}).service(), GoogleRoutes)
    assert isinstance(router(tmp_path, mapkit=True).service(), MapKitHelper)
    assert isinstance(router(tmp_path, traffic="google", secrets=both, mapkit=True).service(), GoogleRoutes)
    assert router(tmp_path, traffic="google").service() is None
    assert router(tmp_path, traffic="off", secrets=both).service() is None
    assert router(tmp_path).describe() == "no live-traffic service set up (navigation links only)"
    assert router(tmp_path, mapkit=True).describe() == "live traffic from MapKit on this Mac"
    assert router(tmp_path, phone="android", secrets=both).describe() == "live traffic from Google Routes API"


def test_route_tool_from_the_phone_location(tmp_path):
    service = JsonService(google_reply(
        {"duration": "1500s", "staticDuration": "1450s", "distanceMeters": 24000, "description": "I-205 N"},
        {"duration": "2100s", "staticDuration": "1300s", "distanceMeters": 19800, "description": "I-5 N",
         "warnings": ["This route has tolls."]}))
    r = router(tmp_path, phone="android", secrets={"GOOGLE_MAPS_API_KEY": "AIza"}, service=service)
    c = ctx(tmp_path, location=HERE)
    out = r.tool({"destination": "Portland Airport, OR"}, c)
    assert not out.is_error
    text = out.content
    assert text.startswith("Driving from your current location to Portland Airport, OR, leaving now (Google Maps, "
                           "with live traffic):")
    assert "1. via I-205 N: 25 min 14.9 mi. Fastest." in text
    assert "2. via I-5 N: 35 min (13 min slower than usual because of traffic) 12.3 mi. This route has tolls." in text
    assert "45.52" not in text                        # the location goes to Google, not into the conversation
    assert c.links == ["https://www.google.com/maps/dir/?api=1&destination=Portland+Airport%2C+OR&travelmode=driving"]
    assert text.endswith("Navigation link (Google Maps): " + c.links[0])


def test_route_tool_asks_the_phone_where_it_is_or_explains(tmp_path):
    r = router(tmp_path)
    out = r.tool({"destination": "PDX"}, ctx(tmp_path))
    assert out.is_error and "Ask where they're starting from" in out.content
    asked = []

    def locate():
        asked.append(1)
        return HERE
    out = r.tool({"destination": "PDX", "origin": "my location"}, ctx(tmp_path, locate=locate))
    assert asked and not out.is_error and "No live-traffic service is set up" in out.content
    assert "https://maps.apple.com/?daddr=PDX&dirflg=d" in out.content

    def broken():
        raise RuntimeError("GPS is off")
    assert r.tool({"destination": "PDX"}, ctx(tmp_path, locate=broken)).is_error


def test_route_tool_checks_its_input(tmp_path):
    r = router(tmp_path, mapkit=True)
    c = ctx(tmp_path, location=HERE)
    assert r.tool({"destination": " "}, c).content == "say where to"
    assert "mode must be" in r.tool({"destination": "x", "mode": "fly"}, c).content
    assert "avoid takes" in r.tool({"destination": "x", "avoid": "tolls"}, c).content
    assert "avoid takes" in r.tool({"destination": "x", "avoid": ["potholes"]}, c).content
    assert "ISO local date-time" in r.tool({"destination": "x", "depart_at": "after lunch"}, c).content
    assert "already passed" in r.tool({"destination": "x", "depart_at": "2020-01-01T08:00"}, c).content
    out = r.tool({"destination": "Gresham", "origin": "12 Elm St, Portland", "depart_at": "2099-01-01T08:00"}, c)
    assert not out.is_error and "Driving from 12 Elm St, Portland to Gresham, leaving Thu 01 Jan 08:00" in out.content
    assert "1. via via MapKit: 10 min 3.1 mi." in out.content     # one route: no "fastest"
    soon = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 60))
    assert "leaving now" in r.tool({"destination": "Gresham", "depart_at": soon}, ctx(tmp_path, location=HERE)).content


def test_route_tool_when_the_service_fails_or_finds_nothing(tmp_path):
    failing = JsonService(lambda request: (403, {"error": {"message": "API key not valid"}}))
    r = router(tmp_path, secrets={"GOOGLE_MAPS_API_KEY": "AIza"}, service=failing)
    out = r.tool({"destination": "PDX"}, ctx(tmp_path, location=HERE))
    assert out.is_error and "API key not valid" in out.content and "still works: https://maps.apple.com/" in out.content
    empty = JsonService(lambda request: (200, {}))
    r = router(tmp_path, secrets={"GOOGLE_MAPS_API_KEY": "AIza"}, service=empty)
    out = r.tool({"destination": "Atlantis", "mode": "walk"}, ctx(tmp_path, location=HERE))
    assert out.is_error and "No walking route was found from your current location to Atlantis" in out.content
    with pytest.raises(MapsError):
        router(tmp_path).plan(Place.from_text("A"), Place.from_text("B"))
    tool = route_tool(r)
    assert tool.name == "route" and "android" in tool.channels and tool.schema["required"] == ["destination"]


# ---- a long-lasting Apple Maps token ---------------------------------------------------------------------------
def test_der_signatures_become_jwt_signatures():
    r, s = b"\x01" * 32, b"\x80" + b"\x02" * 31
    der = b"\x30\x45\x02\x20" + r + b"\x02\x21\x00" + s
    assert der_to_raw(der) == r + s
    short = b"\x30\x08\x02\x02\x00\x05\x02\x02\x00\x07"
    assert der_to_raw(short) == b"\0" * 31 + b"\x05" + b"\0" * 31 + b"\x07"
    long_form = b"\x30\x81\x45\x02\x20" + r + b"\x02\x21\x00" + s
    assert der_to_raw(long_form) == r + s
    for bad in (b"", b"\x31" + der[1:], b"\x30\x06\x03\x01\x00\x02\x01\x00", b"\x30\x26\x02\x22" + b"\x01" * 34 + b"\x02\x00"):
        with pytest.raises((ValueError, IndexError)):
            der_to_raw(bad)


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_maps_token_is_a_verifiable_es256_jwt(tmp_path):
    key = tmp_path / "AuthKey_ABCDE12345.p8"
    subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(key)],
                   check=True, capture_output=True)
    token = make_apple_maps_token(key, "ABCDE12345", "TEAM123456", days=30, now=1_800_000_000)
    header, claims, signature = token.split(".")
    pad = lambda part: base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))   # noqa: E731
    assert json.loads(pad(header)) == {"alg": "ES256", "kid": "ABCDE12345", "typ": "JWT"}
    assert json.loads(pad(claims)) == {"iss": "TEAM123456", "iat": 1_800_000_000, "exp": 1_800_000_000 + 30 * 86400}
    raw = pad(signature)
    assert len(raw) == 64

    def integer(value: bytes) -> bytes:
        value = value.lstrip(b"\0") or b"\0"
        if value[0] & 0x80:
            value = b"\0" + value
        return b"\x02" + bytes([len(value)]) + value
    body = integer(raw[:32]) + integer(raw[32:])
    (tmp_path / "sig.der").write_bytes(b"\x30" + bytes([len(body)]) + body)
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(tmp_path / "pub.pem")], check=True,
                   capture_output=True)
    check = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(tmp_path / "pub.pem"), "-signature",
                            str(tmp_path / "sig.der")], input=f"{header}.{claims}".encode(), capture_output=True)
    assert check.returncode == 0 and b"Verified OK" in check.stdout
    with pytest.raises(ValueError, match="10 letters"):
        make_apple_maps_token(key, "short", "TEAM123456")
    with pytest.raises(ValueError, match="days"):
        make_apple_maps_token(key, "ABCDE12345", "TEAM123456", days=0)
    with pytest.raises(ValueError, match="no such key"):
        make_apple_maps_token(tmp_path / "nope.p8", "ABCDE12345", "TEAM123456")
    (tmp_path / "junk.p8").write_text("not a key")
    with pytest.raises(ValueError, match="couldn't sign"):
        make_apple_maps_token(tmp_path / "junk.p8", "ABCDE12345", "TEAM123456")


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_argusctl_maps_token_saves_it_privately(tmp_path, monkeypatch, capsys):
    from argus import config
    from argus.__main__ import main
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    key = tmp_path / "key.p8"
    subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(key)],
                   check=True, capture_output=True)
    assert main(["maps-token", "--key", str(key), "--key-id", "ABCDE12345", "--team-id", "TEAM123456", "--print"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed.count(".") == 2
    assert main(["maps-token", "--key", str(key), "--key-id", "ABCDE12345", "--team-id", "TEAM123456",
                 "--days", "90"]) == 0
    secrets_file = tmp_path / "home" / "secrets.env"
    assert (secrets_file.stat().st_mode & 0o777) == 0o600
    assert config.read_secrets(secrets_file)["APPLE_MAPS_TOKEN"].count(".") == 2
    assert "lasts 90 days" in capsys.readouterr().out
    assert main(["maps-token", "--key", str(key), "--key-id", "bad", "--team-id", "TEAM123456"]) == 1
    assert "Couldn't make the token" in capsys.readouterr().out


def test_secrets_writer_keeps_the_file_private(tmp_path):
    from argus import config
    path = tmp_path / "s" / "secrets.env"
    config.write_secret(path, "A_KEY", "one")
    config.write_secret(path, "B_KEY", "two")
    config.write_secret(path, "A_KEY", "three")
    assert path.read_text() == "B_KEY=two\nA_KEY=three\n" and (path.stat().st_mode & 0o777) == 0o600
    config.write_secret(path, "B_KEY", "")
    assert config.read_secrets(path) == {"A_KEY": "three"}
    for name, value in (("lower", "x"), ("A_KEY", "two\nlines")):
        with pytest.raises(ValueError):
            config.write_secret(path, name, value)
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        config.write_secret(path, "A_KEY", "x")


def test_secrets_writer_cleans_up_after_a_failed_write(tmp_path, monkeypatch):
    from argus import config
    path = tmp_path / "secrets.env"
    monkeypatch.setattr(config.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        config.write_secret(path, "A_KEY", "x")
    assert list(tmp_path.iterdir()) == []


# ---- push notifications (ntfy) ----------------------------------------------------------------------------------
def test_push_server_rules():
    assert check_server("https://ntfy.sh") == "" and check_server("https://ntfy.example.com/") == ""
    assert check_server("http://100.101.102.103:8080") == "" and check_server("http://127.0.0.1") == ""
    assert check_server("http://my-server.tail1234.ts.net") == "" and check_server("http://localhost:2586") == ""
    assert "only allowed inside Tailscale" in check_server("http://ntfy.sh")
    assert "only allowed inside Tailscale" in check_server("http://8.8.8.8")
    assert "web address" in check_server("ftp://ntfy.sh") and "web address" in check_server("https://u:p@ntfy.sh")
    assert "just the server" in check_server("https://ntfy.sh/my-topic")
    topic = new_topic()
    assert topic.startswith("argus-") and len(topic) >= 30 and topic != new_topic()


def test_push_sends_json_to_the_topic():
    service = JsonService(lambda request: (200, {"id": "abc"}))
    sent = []

    class Audit:
        def log(self, event, **fields):
            sent.append((event, fields))
    push = Push(service.url + "/", "argus-" + "x" * 30, token="tk_secret", audit=Audit())
    assert push.send("Reminder:  call   home", link="https://maps.apple.com/?daddr=x", urgent=True)
    request = service.requests[0]
    assert request["path"] == "/" and request["headers"]["authorization"] == "Bearer tk_secret"
    assert request["body"] == {"topic": "argus-" + "x" * 30, "message": "Reminder: call home", "title": "Argus",
                               "priority": 4, "click": "https://maps.apple.com/?daddr=x"}
    assert sent == [("push", {"ok": True, "status": 200, "chars": 19})]
    assert not push.send("   ")
    plain = Push(service.url, "argus-" + "y" * 30)
    assert plain.send("hello", link="javascript:alert(1)")
    assert "authorization" not in service.requests[-1]["headers"] and "click" not in service.requests[-1]["body"]


def test_push_failures_never_raise():
    down = JsonService(lambda request: (500, {}))
    assert not Push(down.url, "argus-" + "z" * 30).send("hi")
    assert not Push("http://127.0.0.1:9", "argus-" + "z" * 30).send("hi")
    with pytest.raises(ValueError, match="20 to 64"):
        Push("https://ntfy.sh", "short")
    with pytest.raises(ValueError, match="push server"):
        Push("http://ntfy.sh", "argus-" + "z" * 30)


def test_push_from_settings(tmp_path):
    s = make_settings(tmp_path)
    assert push_from(s, {"NTFY_TOPIC": "argus-" + "q" * 30}) is None            # switched off
    s.push_enabled = True
    assert push_from(s, {}) is None                                             # no topic
    p = push_from(s, {"NTFY_TOPIC": "argus-" + "q" * 30, "NTFY_TOKEN": "tk"})
    assert p.server == "https://ntfy.sh" and p.token == "tk"
    assert push_from(s, {"NTFY_TOPIC": "bad topic"}) is None


def test_push_settings_are_checked_when_loading(tmp_path, monkeypatch):
    from argus import config
    path = tmp_path / "config.toml"
    path.write_text('[push]\nenabled = true\nserver = "http://ntfy.sh"\n')
    with pytest.raises(ValueError, match="push.server: plain http"):
        config.load(path)
    path.write_text('[push]\nenabled = false\nserver = "http://ntfy.sh"\n')
    assert config.load(path).push_server == "http://ntfy.sh"           # unused while push is off


@pytest.mark.parametrize("text,message", [
    ('[phone]\ntype = "nokia"\n', "phone.type"), ('[maps]\napp = "bing"\n', "maps.app"),
    ('[maps]\ntraffic = "waze"\n', "maps.traffic"), ('[maps]\nunits = "furlongs"\n', "maps.units"),
    ('[android]\nnotification_days = 0\n', "android.notification_days"),
])
def test_bad_phone_and_maps_settings_are_refused(tmp_path, text, message):
    from argus import config
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=message.replace(".", r"\.")):
        config.load(path)


def test_phone_and_maps_settings_load(tmp_path):
    from argus import config
    path = tmp_path / "config.toml"
    path.write_text('[phone]\ntype = "android"\n[maps]\ntraffic = "google"\nunits = "metric"\n'
                    '[android]\ndrive_assist = true\nwhatsapp_dir = "/sdcard/x"\n')
    s = config.load(path)
    assert (s.phone_type, s.maps_link_app, s.maps_traffic, s.maps_units) == ("android", "google", "google", "metric")
    assert s.android_drive_assist and s.android_whatsapp_dir == "/sdcard/x"
    s.maps_app = "apple"
    assert s.maps_link_app == "apple"
    assert config.Settings().maps_link_app == "apple"


def test_deliver_from_the_mac_pushes_only_while_away(tmp_path):
    """The Mac speaks reminders when you're there; with push on, they also reach the phone while you're away."""
    from argus.app import App
    sent = []

    class FakePush:
        def send(self, text, **kw):
            sent.append(text)
            return True

    class FakeArgus:
        def __init__(self):
            self.here, self.spoken = True, []

        def present(self):
            return self.here

        def announce(self, text):
            self.spoken.append(text)

    app = App.__new__(App)
    app.push, app.argus = FakePush(), FakeArgus()
    app.deliver("Reminder: water the plants")
    assert app.argus.spoken == ["Reminder: water the plants"] and sent == []
    app.argus.here = False
    app.deliver("Reminder: call home")
    assert sent == ["Reminder: call home"]
    app.argus = None
    app.deliver("• headline")
    assert sent[-1] == "• headline"


def test_set_setting_edits_config_in_place(tmp_path):
    from argus import config
    path = tmp_path / "config.toml"
    path.write_text('name = "Argus"\n\n[push]\nenabled = false   # off\n\n[maps]\napp = "apple"\n')
    config.set_setting(path, "push", "enabled", "true")
    config.set_setting(path, "push", "server", '"https://ntfy.example.com"')
    config.set_setting(path, "maps", "units", '"metric"')
    config.set_setting(path, "phone", "type", '"android"')
    assert path.read_text() == ('name = "Argus"\n\n[push]\nenabled = true\nserver = "https://ntfy.example.com"\n\n'
                                '[maps]\napp = "apple"\nunits = "metric"\n\n[phone]\ntype = "android"\n')
    loaded = config.load(path)
    assert loaded.push_enabled and loaded.maps_units == "metric" and loaded.phone_type == "android"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    fresh = tmp_path / "new.toml"
    config.set_setting(fresh, "push", "enabled", "true")
    assert fresh.read_text() == "[push]\nenabled = true\n"


def test_argusctl_push_on_test_off(tmp_path, monkeypatch, capsys):
    from argus import config, push as push_mod
    from argus.__main__ import main
    home = tmp_path / "home"
    monkeypatch.setenv("ARGUS_HOME", str(home))
    assert main(["push", "test"]) == 1 and "aren't on" in capsys.readouterr().out
    assert main(["push", "on", "--topic", "short"]) == 2
    assert main(["push", "on", "--server", "http://ntfy.sh"]) == 2 and "only allowed inside Tailscale" in capsys.readouterr().out
    service = JsonService(lambda request: (200, {}))
    assert main(["push", "on", "--server", service.url]) == 0
    out = capsys.readouterr().out
    topic = config.read_secrets(home / "secrets.env")["NTFY_TOPIC"]
    assert topic.startswith("argus-") and f"topic  {topic}" in out and "Keep the topic private" in out
    s = config.load()
    assert s.push_enabled and s.push_server == service.url
    assert main(["push", "test"]) == 0 and service.requests[-1]["body"]["topic"] == topic
    assert main(["push", "on", "--topic", "argus-" + "s" * 30]) == 0
    assert config.read_secrets(home / "secrets.env")["NTFY_TOPIC"] == "argus-" + "s" * 30
    monkeypatch.setattr(push_mod.Push, "send", lambda self, text, **kw: False)
    assert main(["push", "test"]) == 1 and "Sending failed" in capsys.readouterr().out
    assert main(["push", "off"]) == 0 and not config.load().push_enabled
