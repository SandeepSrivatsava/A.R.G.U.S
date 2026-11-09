import base64
import http.client
import json
import threading

import pytest

from argus.audit import Audit
from argus.glasses import GlassesBridge
from argus.phone import Gatekeeper, PhoneServer
from argus.telegram import TelegramAPI, TelegramBot

from fakes import FakeTelegram, Script, call, make_agent, say, wait_for

USER = 987654321


@pytest.fixture
def tg():
    server = FakeTelegram()
    yield server
    server.shutdown()


def make_bot(tmp_path, tg, script):
    agent, mock = make_agent(tmp_path, script)
    bot = TelegramBot(TelegramAPI(tg.token, tg.url), USER, agent, agent.store, Audit(tmp_path / "tg-audit.jsonl"),
                      approval_timeout=5)
    return bot, agent


def test_owner_gets_answers_strangers_get_nothing(tmp_path, tg):
    bot, agent = make_bot(tmp_path, tg, Script(say("Hello Sandeep.")))
    tg.message("hi", user=111)                              # a stranger
    tg.message("hi there")
    assert bot.poll_once(timeout=1) == 2
    assert wait_for(lambda: any(m["text"] == "Hello Sandeep." for m in tg.sent))
    assert all(m["chat_id"] == USER for m in tg.sent)
    assert "telegram_stranger" in (tmp_path / "tg-audit.jsonl").read_text()
    assert bot.poll_once(timeout=1) == 0                     # offset remembered: nothing handled twice


def test_group_chats_are_ignored(tmp_path, tg):
    bot, _ = make_bot(tmp_path, tg, Script(say("nope")))
    tg.push({"message": {"message_id": 1, "from": {"id": USER}, "chat": {"id": -100, "type": "group"}, "text": "hi"}})
    bot.poll_once(timeout=1)
    assert tg.sent == []


def test_risky_action_is_approved_with_buttons(tmp_path, tg):
    target = tmp_path / "workspace" / "old.txt"
    script = Script(call("run_command", {"command": "rm old.txt"}), say("Deleted it."))
    bot, agent = make_bot(tmp_path, tg, script)
    target.write_text("x")
    tg.message("delete old.txt")
    bot.poll_once(timeout=1)
    assert wait_for(lambda: any("reply_markup" in m for m in tg.sent))
    buttons = [m for m in tg.sent if "reply_markup" in m][0]["reply_markup"]["inline_keyboard"][0]
    once = [b["callback_data"] for b in buttons if b["text"] == "Allow once"][0]
    tg.press(once, user=111)                                # a stranger's tap does nothing
    tg.press(once)
    bot.poll_once(timeout=1)
    bot.poll_once(timeout=1)
    assert wait_for(lambda: any(m["text"] == "Deleted it." for m in tg.sent), 5)
    assert not target.exists()
    assert any(c[0] == "editMessageText" and "Allowed once" in c[1]["text"] for c in tg.calls)


def test_unanswered_approval_is_denied(tmp_path, tg):
    target = tmp_path / "workspace" / "keep.txt"
    script = Script(call("run_command", {"command": "rm keep.txt"}), say("Okay, I left it."))
    bot, agent = make_bot(tmp_path, tg, script)
    bot.approval_timeout = 0.5
    target.write_text("x")
    tg.message("delete keep.txt")
    bot.poll_once(timeout=1)
    assert wait_for(lambda: any(m["text"] == "Okay, I left it." for m in tg.sent), 5)
    assert target.exists()


def test_photos_commands_and_voice_notes(tmp_path, tg):
    script = Script(say("A golden retriever."))
    bot, agent = make_bot(tmp_path, tg, script)
    tg.files["photos/p1.jpg"] = b"\xff\xd8\xffJPEG"
    tg.message("", photo=[{"file_id": "p0"}, {"file_id": "p1"}], caption="what breed?")
    tg.message("/new")
    tg.message("", voice={"file_id": "v1"})
    bot.poll_once(timeout=1)
    assert wait_for(lambda: any(m["text"] == "A golden retriever." for m in tg.sent))
    sent = script.bodies[0]["messages"][-1]["content"]
    assert sent[0]["type"] == "image" and sent[1]["text"] == "what breed?"
    texts = [m["text"] for m in tg.sent]
    assert "Fresh start." in texts and any("voice notes" in t for t in texts)


def test_notify(tmp_path, tg):
    bot, _ = make_bot(tmp_path, tg, Script())
    assert bot.notify("Reminder: call mom")
    assert tg.sent[-1] == {"chat_id": USER, "text": "Reminder: call mom", "disable_web_page_preview": True}


# ---- iPhone gateway -----------------------------------------------------------------------------------
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
    assert req(server, "POST", "/glasses/status", {"connected": True, "device": "Ray-Ban Meta"}) == (200, {"ok": True})
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
