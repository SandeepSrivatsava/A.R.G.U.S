"""The iPhone gateway and the glasses bridge, end to end over HTTP."""
import base64
import http.client
import json
import threading

import pytest

from argus.audit import Audit
from argus.glasses import GlassesBridge
from argus.phone import Gatekeeper, PhoneServer

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()
JPG = base64.b64encode(b"\xff\xd8\xff" + b"\0" * 32).decode()


@pytest.fixture
def phone(tmp_path):
    asked = []
    glasses = GlassesBridge()

    def ask(content):
        asked.append(content)
        return "Sure thing."

    server = PhoneServer(("127.0.0.1", 0), ask, Gatekeeper("phone-secret-token"), Audit(tmp_path / "audit.jsonl"), glasses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, asked, glasses
    server.shutdown()


def req(server, method, path, body=None, token="phone-secret-token"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read() or b"{}")


def test_phone_ask_and_look(phone):
    server, asked, _ = phone
    assert req(server, "POST", "/ask", {"text": "What's on my calendar?"}) == (200, {"reply": "Sure thing."})
    assert asked[-1] == "What's on my calendar?"
    assert req(server, "POST", "/look", {"image": PNG, "text": "what's this?"})[0] == 200
    assert asked[-1][0]["source"]["media_type"] == "image/png" and asked[-1][1]["text"] == "what's this?"


def test_phone_rejects_bad_requests(phone):
    server, asked, _ = phone
    assert req(server, "POST", "/ask", {"text": "hi"}, token="nope")[0] == 401
    assert req(server, "POST", "/ask", {"text": ""})[0] == 400
    assert req(server, "POST", "/look", {"image": "zzz"})[0] == 400
    assert req(server, "POST", "/admin", {"text": "hi"})[0] == 404
    assert asked == []


def test_glasses_app_round_trip(phone):
    server, _, glasses = phone
    assert req(server, "POST", "/glasses/status", {"connected": True, "device": "Ray-Ban Meta"}) == (200, {"ok": True, "glance_every": 0})
    assert glasses.connected and "Ray-Ban Meta" in glasses.describe()
    result = {}

    def argus_wants_a_look():
        result["jpeg"] = glasses.capture(timeout=10)

    t = threading.Thread(target=argus_wants_a_look)
    t.start()
    status, body = req(server, "GET", "/glasses/next")
    assert status == 200 and body.get("capture")
    assert req(server, "POST", "/glasses/frame", {"request_id": body["capture"], "image": JPG}) == (200, {"ok": True})
    t.join(5)
    assert result["jpeg"].startswith(b"\xff\xd8\xff")
    assert req(server, "POST", "/glasses/frame", {"request_id": "stale", "image": JPG})[0] == 409
