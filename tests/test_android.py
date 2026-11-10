"""Argus on an Android phone: the Termux:API wrappers, searching texts / the notification log / WhatsApp's
backup, phone approvals (dialog + fingerprint), and the Android app itself (answers, reminders, drive assist)."""
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from argus import android
from argus.android import (AndroidApprover, Message, MessageSearch, NotificationLog, Termux, TermuxError,
                           WhatsAppBackup, contact_names, matches, parse_time, person_matches, phone_tools, same_number,
                           sms_messages, terms_of)
from argus.approvals import DENY, ONCE, TASK, ApprovalRequest
from argus.maps import Route
from argus.policy import ASK, BLOCK, Policy
from fakes import call, make_settings, say, wait_for
from web_fakes import FakeTermux, JsonService, routed_to

KEY = "ab" * 32
NOW = time.time()


def ts(days_ago: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - days_ago * 86400))


SMS = [
    {"threadid": 1, "type": "inbox", "read": True, "sender": "Priya", "number": "+15035550100",
     "received": ts(3.01), "body": "The wifi password is maple-42"},
    {"threadid": 2, "type": "sent", "read": True, "number": "+15035550199", "received": ts(1), "body": "On my way"},
    {"threadid": 3, "type": "inbox", "read": False, "sender": "+15035550111", "number": "+15035550111",
     "received": ts(40), "body": "Your flight PNR is X7Q2LM"},
]


# ---- small helpers -----------------------------------------------------------------------------------------------
def test_times_numbers_and_words():
    assert parse_time("2026-11-09 18:30") == time.mktime(time.strptime("2026-11-09 18:30", "%Y-%m-%d %H:%M"))
    assert parse_time("2026-11-09 18:30:12") > parse_time("2026-11-09 18:30")
    assert parse_time(1_762_790_000_000) == 1_762_790_000.0 and parse_time(1_762_790_000) == 1_762_790_000.0
    assert parse_time("1762790000000") == 1_762_790_000.0
    for unknown in (None, True, "yesterday", ""):
        assert parse_time(unknown) == 0.0
    assert same_number("+1 (503) 555-0100", "503-555-0100") and not same_number("555-0100", "503-555-0100x")
    assert not same_number("12", "12")
    assert terms_of('Wifi "password", please!') == ["wifi", "password", "please"]
    assert terms_of("a I x") == [] and terms_of("pnr X7Q2LM") == ["pnr", "x7q2lm"]
    assert matches("The WiFi password", ["wifi", "password"]) and not matches("wifi", ["wifi", "password"])
    m = Message("text", 0, "Priya", "Priya", "hi")
    assert person_matches(m, "priya") and person_matches(m, "") and not person_matches(m, "Sam")
    n = Message("text", 0, "+15035550100", "me", "hi")
    assert person_matches(n, "503 555 0100") and person_matches(n, "0100") and not person_matches(n, "503 555 0199")


def test_texts_become_messages():
    found = sms_messages(SMS + [{"type": "outbox", "address": "+1555", "date": 1_762_790_000_000, "body": "queued"}])
    assert [(m.chat, m.who) for m in found] == [("Priya", "Priya"), ("+15035550199", "me"),
                                                ("+15035550111", "+15035550111"), ("+1555", "me")]
    assert found[0].source == "text" and found[0].when > 0 and found[3].when == 1_762_790_000.0


# ---- Termux:API -------------------------------------------------------------------------------------------------
def test_termux_commands_take_text_on_stdin_and_parse_json():
    fake = FakeTermux(termux_location={"latitude": 45.5, "longitude": -122.6, "accuracy": 9, "speed": 20.5},
                      termux_dialog={"code": -1, "text": "Allow once", "index": 0},
                      termux_fingerprint={"auth_result": "AUTH_RESULT_SUCCESS", "failed_attempts": 0},
                      termux_speech_to_text="partial\nfind the wifi password\n",
                      termux_sms_list=SMS + ["junk"], termux_notification_list=[{"packageName": "com.whatsapp"}, 3],
                      termux_contact_list=[{"name": "Priya", "number": "+15035550100"}],
                      termux_battery_status={"plugged": "PLUGGED_AC", "percentage": 80})
    t = Termux(run=fake, which=lambda name: "/bin/" + name)
    assert t.available()
    t.speak("  Hello   there ")
    t.speak("   ")
    assert fake.calls[-1] == (["termux-tts-speak"], "Hello there")
    assert t.listen() == "find the wifi password"
    t.notify("Argus", "Reminder:\n call home", nid="r1", urgent=True)
    assert fake.calls[-1][0] == ["termux-notification", "--id", "r1", "--title", "Argus", "--content",
                                 "Reminder: call home", "--priority", "high", "--vibrate", "200,100,200"]
    assert t.choose("Argus wants to text Priya", ["Allow once", "Allow, for this task", "Don't allow"]) == 0
    assert fake.calls[-1][0][-1] == "Allow once,Allow  for this task,Don't allow"
    assert t.fingerprint("Approve", "text Priya") is True
    assert t.location() == {"lat": 45.5, "lon": -122.6, "accuracy": 9.0, "speed": 20.5}
    assert fake.calls[-1][0] == ["termux-location", "-p", "network", "-r", "once"]
    assert len(t.sms(5)) == 3 and fake.calls[-1][0] == ["termux-sms-list", "-l", "5", "-t", "all"]
    assert t.notifications() == [{"packageName": "com.whatsapp"}]
    assert t.contacts()[0]["name"] == "Priya" and t.battery()["plugged"] == "PLUGGED_AC"
    t.open_url("https://maps.google.com")
    t.send_sms("+15035550100", "Running late")
    assert fake.calls[-1] == (["termux-sms-send", "-n", "+15035550100"], "Running late")
    t.wake_lock()
    t.remove_notification("r1")
    assert fake.names()[-2:] == ["termux-wake-lock", "termux-notification-remove"]
    assert not Termux(run=fake, which=lambda name: None).available()


def test_termux_failures_are_plain_errors():
    cases = {"termux-sms-list": FileNotFoundError, "termux-location": subprocess.TimeoutExpired("x", 1),
             "termux-contact-list": PermissionError("denied"),
             "termux-battery-status": subprocess.CompletedProcess([], 1, "", "Termux:API isn't installed"),
             "termux-notification-list": "not json"}
    t = Termux(run=FakeTermux(**{k.replace("-", "_"): v for k, v in cases.items()}))
    for call_it, message in ((lambda: t.sms(), "isn't installed"), (lambda: t.location(), "didn't answer"),
                             (lambda: t.contacts(), "couldn't start"), (lambda: t.battery(), "Termux:API isn't installed"),
                             (lambda: t.notifications(), "couldn't read")):
        with pytest.raises(TermuxError, match=message):
            call_it()
    quiet = Termux(run=FakeTermux(termux_dialog={"code": -2}, termux_fingerprint={"auth_result": "AUTH_RESULT_FAILURE"},
                                  termux_location="", termux_speech_to_text="\n", termux_sms_list={"oops": 1},
                                  termux_battery_status=[]))
    assert quiet.choose("t", ["a"]) is None and quiet.fingerprint("t", "d") is False
    assert quiet.location() is None and quiet.listen() == "" and quiet.sms() == [] and quiet.battery() == {}
    odd = Termux(run=FakeTermux(termux_dialog={"code": -1, "index": 7}))
    assert odd.choose("t", ["a", "b"]) is None


# ---- the notification log --------------------------------------------------------------------------------------
def test_notification_log_keeps_new_messages_privately(tmp_path):
    clock = [NOW]
    log = NotificationLog(tmp_path / "private" / "notifications.db", clock=lambda: clock[0])
    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "private" / "notifications.db").stat().st_mode & 0o777 == 0o600
    items = [{"packageName": "com.whatsapp", "title": "Priya", "content": "Gate code is 4471#", "when": ts(0.1)},
             {"packageName": "org.thoughtcrime.securesms", "title": "Sam", "content": "Dinner at 8?", "when": ts(2)},
             {"packageName": "com.termux", "title": "Termux", "content": "1 session"},
             {"packageName": "com.example", "title": "", "content": ""}]
    assert log.record(items) == 2
    assert log.record(items) == 0                                  # already seen
    found = log.search(["gate", "code"])
    assert [(m.source, m.chat, m.text) for m in found] == [("notification (WhatsApp)", "Priya", "Gate code is 4471#")]
    assert log.search(["100%_x"]) == []                            # LIKE wildcards are taken literally
    assert [m.chat for m in log.search([])] == ["Priya", "Sam"]
    assert [m.chat for m in log.search([], since=NOW - 86400)] == ["Priya"]
    clock[0] += 40 * 86400
    assert log.prune(30) == 2 and log.search([]) == []
    log.close()


# ---- WhatsApp's backup ---------------------------------------------------------------------------------------------
def whatsapp_db(path: Path, layout: str = "new") -> Path:
    db = sqlite3.connect(path)
    if layout == "new":
        db.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
                                  sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT);
            INSERT INTO jid VALUES (1, '15035550100', 's.whatsapp.net', '15035550100@s.whatsapp.net');
            INSERT INTO jid VALUES (2, '120363', 'g.us', '120363@g.us');
            INSERT INTO jid VALUES (3, '15035550177', 's.whatsapp.net', '15035550177@s.whatsapp.net');
            INSERT INTO jid VALUES (4, 'lidsomething', 'lid', 'lidsomething@lid');
            INSERT INTO chat VALUES (1, 1, NULL);
            INSERT INTO chat VALUES (2, 2, 'Goa trip');
            """)
        rows = [(1, 0, 0, NOW - 3 * 86400, "The wifi password is maple-42"),
                (1, 1, 0, NOW - 2 * 86400, "thanks! what's the wifi name?"),
                (2, 0, 3, NOW - 86400, "Villa wifi password: sunset2026"),
                (2, 0, 4, NOW - 600, "Check-in after 2 pm"),
                (1, 0, 0, NOW - 100 * 86400, "old wifi password: oak-7"),
                (1, 0, 0, NOW, None)]
        db.executemany("INSERT INTO message (chat_row_id, from_me, sender_jid_row_id, timestamp, text_data) "
                       "VALUES (?, ?, ?, ?, ?)", [(c, f, s, int(t * 1000), x) for c, f, s, t, x in rows])
    elif layout == "old":
        db.executescript("""
            CREATE TABLE messages (_id INTEGER PRIMARY KEY, key_remote_jid TEXT, key_from_me INTEGER, data TEXT,
                                   timestamp INTEGER, remote_resource TEXT);
            CREATE TABLE chat_list (_id INTEGER PRIMARY KEY, key_remote_jid TEXT, subject TEXT);
            INSERT INTO chat_list VALUES (1, '120363@g.us', 'Book club');
            """)
        db.executemany("INSERT INTO messages (key_remote_jid, key_from_me, data, timestamp, remote_resource) "
                       "VALUES (?, ?, ?, ?, ?)",
                       [("15035550100@s.whatsapp.net", 0, "Locker combo 1234", int(NOW * 1000), None),
                        ("120363@g.us", 0, "Next book: Dune; locker at the library", int(NOW * 1000) - 5,
                         "15035550177@s.whatsapp.net")])
    else:
        db.execute("CREATE TABLE something (x)")
    db.commit()
    db.close()
    return path


def fake_wadecrypt(tmp_path: Path, plain: Path, exit_code: int = 0, write: str = "copy") -> Path:
    """Stands in for wa-crypt-tools' wadecrypt: "decrypts" by copying a prepared database."""
    tool = tmp_path / "wadecrypt"
    action = {"copy": f'cp "{plain}" "$3"', "junk": 'echo junk > "$3"', "none": ":"}[write]
    tool.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/wadecrypt.args"\n{action}\n'
                    f'echo "bad key $1" >&2\nexit {exit_code}\n' if exit_code else
                    f'#!/bin/sh\necho "$@" > "{tmp_path}/wadecrypt.args"\n{action}\n')
    tool.chmod(0o755)
    return tool


def backup_folder(tmp_path: Path, mtime: float = NOW) -> Path:
    folder = tmp_path / "WhatsApp" / "Databases"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "msgstore-2026-11-01.1.db.crypt15").write_bytes(b"older")
    os.utime(folder / "msgstore-2026-11-01.1.db.crypt15", (mtime - 9 * 86400,) * 2)
    newest = folder / "msgstore.db.crypt15"
    newest.write_bytes(b"encrypted")
    os.utime(newest, (mtime, mtime))
    return folder


def test_whatsapp_backup_is_decrypted_once_and_searched(tmp_path):
    plain = whatsapp_db(tmp_path / "plain.db")
    folder = backup_folder(tmp_path)
    tool = fake_wadecrypt(tmp_path, plain)
    names = {"5035550100": "Priya", "5035550177": "Rahul"}
    wa = WhatsAppBackup(KEY.upper(), tmp_path / "private" / "whatsapp.db", tool=tool, folder=str(folder),
                        contacts=lambda: names)
    assert wa.problem() == "" and wa.backup() == folder / "msgstore.db.crypt15"
    found = wa.messages(["wifi", "password"], since=NOW - 30 * 86400)
    assert [(m.chat, m.who, m.text) for m in found] == [
        ("Goa trip", "Rahul", "Villa wifi password: sunset2026"), ("Priya", "Priya", "The wifi password is maple-42")]
    args = (tmp_path / "wadecrypt.args").read_text().split()
    assert args == [KEY.upper(), str(folder / "msgstore.db.crypt15"), str(tmp_path / "private" / ".whatsapp-decrypting.tmp")]
    cache = tmp_path / "private" / "whatsapp.db"
    assert cache.stat().st_mode & 0o777 == 0o600 and (tmp_path / "private").stat().st_mode & 0o777 == 0o700
    (tmp_path / "wadecrypt.args").unlink()
    assert [m.who for m in wa.messages(["check-in"])] == ["lidsomething"]
    assert not (tmp_path / "wadecrypt.args").exists()             # same backup: the private copy is reused
    assert wa.messages(["wifi"], since=0)[-1].text == "old wifi password: oak-7"
    assert [m.who for m in wa.messages(["wifi", "name"])] == ["me"]
    os.utime(folder / "msgstore.db.crypt15", (NOW + 60, NOW + 60))
    wa.messages(["wifi"])
    assert (tmp_path / "wadecrypt.args").exists()                  # WhatsApp wrote a newer backup


def test_whatsapp_older_layout_and_unknown_numbers(tmp_path):
    plain = whatsapp_db(tmp_path / "plain.db", "old")
    wa = WhatsAppBackup(KEY, tmp_path / "wa.db", tool=fake_wadecrypt(tmp_path, plain),
                        folder=str(backup_folder(tmp_path)), contacts=lambda: (_ for _ in ()).throw(TermuxError("no")))
    found = wa.messages(["locker"])
    assert [(m.chat, m.who) for m in found] == [("+15035550100", "+15035550100"), ("Book club", "+15035550177")]
    junk = whatsapp_db(tmp_path / "junk.db", "other")
    wa2 = WhatsAppBackup(KEY, tmp_path / "wa2.db", tool=fake_wadecrypt(tmp_path, junk), folder=str(backup_folder(tmp_path)))
    with pytest.raises(TermuxError, match="layout Argus doesn't know"):
        wa2.messages(["x"])


def test_whatsapp_problems_are_explained(tmp_path):
    folder = backup_folder(tmp_path)
    tool = fake_wadecrypt(tmp_path, whatsapp_db(tmp_path / "plain.db"))
    assert "needs WhatsApp's 64-digit backup key" in WhatsAppBackup("", tmp_path / "w.db", tool=tool).problem()
    assert "isn't 64 letters" in WhatsAppBackup("12345", tmp_path / "w.db", tool=tool).problem()
    assert "isn't installed" in WhatsAppBackup(KEY, tmp_path / "w.db", tool=tmp_path / "nope").problem()
    assert "no WhatsApp backup was found" in WhatsAppBackup(KEY, tmp_path / "w.db", tool=tool,
                                                            folder=str(tmp_path / "empty")).problem()
    with pytest.raises(TermuxError, match="no WhatsApp backup"):
        WhatsAppBackup(KEY, tmp_path / "w.db", tool=tool, folder=str(tmp_path / "empty")).refresh()
    failing = fake_wadecrypt(tmp_path, tmp_path / "plain.db", exit_code=1, write="none")
    with pytest.raises(TermuxError) as info:
        WhatsAppBackup(KEY, tmp_path / "w.db", tool=failing, folder=str(folder)).refresh()
    assert "is the 64-digit key right?" in str(info.value) and KEY not in str(info.value) and "[key]" in str(info.value)
    junk = fake_wadecrypt(tmp_path, tmp_path / "plain.db", write="junk")
    with pytest.raises(TermuxError, match="isn't a database"):
        WhatsAppBackup(KEY, tmp_path / "w.db", tool=junk, folder=str(folder)).refresh()
    assert not (tmp_path / ".whatsapp-decrypting.tmp").exists()

    def broken_run(*a, **kw):
        raise subprocess.TimeoutExpired("wadecrypt", 900)
    with pytest.raises(TermuxError, match="couldn't decrypt"):
        WhatsAppBackup(KEY, tmp_path / "w.db", tool=tool, folder=str(folder), run=broken_run).refresh()


def test_whatsapp_reader_gets_no_argus_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak")
    seen = {}

    def run(args, **kw):
        seen.update(kw["env"])
        Path(args[3]).write_bytes(Path(tmp_path / "plain.db").read_bytes())
        return subprocess.CompletedProcess(args, 0, "", "")
    whatsapp_db(tmp_path / "plain.db")
    wa = WhatsAppBackup(KEY, tmp_path / "w.db", tool=fake_wadecrypt(tmp_path, tmp_path / "plain.db"),
                        folder=str(backup_folder(tmp_path)), run=run)
    wa.refresh()
    assert "ANTHROPIC_API_KEY" not in seen and "PATH" in seen


@pytest.mark.skipif(not os.environ.get("ARGUS_WA_TOOLS"), reason="set ARGUS_WA_TOOLS to wa-crypt-tools' bin folder")
def test_real_wa_crypt_tools_round_trip(tmp_path):
    """With the real wa-crypt-tools: a crypt15 backup made by waencrypt is found and searched."""
    tools = Path(os.environ["ARGUS_WA_TOOLS"])
    plain = whatsapp_db(tmp_path / "msgstore.db")
    folder = tmp_path / "Databases"
    folder.mkdir()
    subprocess.run([str(tools / "waencrypt"), KEY, str(plain), str(folder / "msgstore.db.crypt15")], check=True,
                   capture_output=True)
    wa = WhatsAppBackup(KEY, tmp_path / "private" / "whatsapp.db", tool=tools / "wadecrypt", folder=str(folder))
    assert [m.text for m in wa.messages(["maple"])] == ["The wifi password is maple-42"]
    with pytest.raises(TermuxError, match="key right|wrong key"):     # wadecrypt may "succeed" with junk
        WhatsAppBackup("cd" * 32, tmp_path / "other.db", tool=tools / "wadecrypt", folder=str(folder)).refresh()


# ---- searching everything ------------------------------------------------------------------------------------------
def make_search(tmp_path, *, whatsapp=True, sms=SMS):
    fake = FakeTermux(termux_sms_list=sms)
    notes = NotificationLog(tmp_path / "n.db")
    notes.record([{"packageName": "com.whatsapp", "title": "Priya", "content": "The wifi password is maple-42",
                   "when": ts(3)},
                  {"packageName": "com.google.android.gm", "title": "Airline", "content": "Booking confirmed, PNR X7Q2LM",
                   "when": ts(40)}])
    wa = None
    if whatsapp:
        wa = WhatsAppBackup(KEY, tmp_path / "w.db", tool=fake_wadecrypt(tmp_path, whatsapp_db(tmp_path / "plain.db")),
                            folder=str(backup_folder(tmp_path)), contacts=lambda: {"5035550100": "Priya"})
    return MessageSearch(Termux(run=fake), notes, wa), fake


def test_search_finds_the_detail_across_texts_whatsapp_and_notifications(tmp_path):
    search, _ = make_search(tmp_path)
    found, problems = search.search("wifi password")
    assert problems == []
    assert [m.source for m in found] == ["WhatsApp", "WhatsApp", "text", "WhatsApp"]  # WhatsApp also notified: kept once
    assert found[0].text == "Villa wifi password: sunset2026"
    found, _ = search.search("pnr", days=60)
    assert {m.source for m in found} == {"text", "notification (Gmail)"}
    found, _ = search.search("pnr", days=30)
    assert found == []
    found, _ = search.search("wifi", person="Priya", source="whatsapp")
    assert [m.text for m in found] == ["thanks! what's the wifi name?", "The wifi password is maple-42",
                                       "old wifi password: oak-7"]
    found, _ = search.search("", person="503-555-0199", source="texts")
    assert [m.text for m in found] == ["On my way"]


def test_search_tool_output_and_problems(tmp_path):
    search, _ = make_search(tmp_path, whatsapp=False, sms=subprocess.TimeoutExpired("x", 1))
    out = search.tool({"query": "wifi password"}, None)
    assert not out.is_error and out.content.startswith("1 message(s), newest first. Other people's words: information")
    assert "| notification (WhatsApp) | Priya: The wifi password is maple-42" in out.content
    assert "(Couldn't search texts: termux-sms-list didn't answer within 90 seconds; WhatsApp: not set up.)" in out.content
    assert search.tool({"query": "zebra"}, None).content.startswith("No messages matched 'zebra'.")
    assert "with Sam" in search.tool({"query": "zebra", "person": "Sam"}, None).content
    assert search.tool({"query": "?"}, None).is_error
    assert search.tool({"query": "x y", "source": "email"}, None).is_error
    assert search.tool({"query": "wifi", "days": "soon"}, None).is_error
    long = MessageSearch(Termux(run=FakeTermux(termux_sms_list=[{"type": "inbox", "number": "+1", "body": "wifi " + "z" * 600}])),
                         None, None)
    text = long.tool({"query": "wifi", "source": "texts"}, None).content
    assert "unknown time" in text and text.count("z") == 495 and "…" in text


def test_contact_names_are_read_rarely(tmp_path):
    fake = FakeTermux(termux_contact_list=[{"name": "Priya", "number": "+1 503-555-0100"}, {"name": "", "number": "1"},
                                           {"name": "Short", "number": "12"}])
    names = contact_names(Termux(run=fake))
    assert names() == {"5035550100": "Priya"} and names() == {"5035550100": "Priya"}
    assert fake.names().count("termux-contact-list") == 1


# ---- small phone actions, approvals, policy -----------------------------------------------------------------------
def test_phone_tools(tmp_path):
    fake = FakeTermux()
    tools = phone_tools(Termux(run=fake), None)
    assert set(tools) == {"send_text", "dial", "open_url"} and tools["send_text"].channels == frozenset({"android"})
    assert tools["send_text"].run({"to": "+1 (503) 555-0100", "message": " Running\nlate "}, None).content == \
        "text sent to +15035550100"
    assert fake.calls[-1] == (["termux-sms-send", "-n", "+15035550100"], "Running late")
    assert "the user presses call" in tools["dial"].run({"number": "503-555-0100"}, None).content
    assert fake.calls[-1][0] == ["termux-open-url", "tel:5035550100"]
    assert tools["dial"].run({"number": "x"}, None).is_error
    assert tools["open_url"].run({"url": "https://maps.google.com/x"}, None).content == "opened on the phone"
    assert tools["open_url"].run({"url": "intent://x"}, None).is_error
    broken = phone_tools(Termux(run=FakeTermux(termux_sms_send=FileNotFoundError, termux_open_url=FileNotFoundError)),
                         MessageSearch(Termux(run=FakeTermux()), None, None))
    assert "search_messages" in broken
    for name, args in (("send_text", {"to": "+15035550100", "message": "hi"}), ("dial", {"number": "911"}),
                       ("open_url", {"url": "https://x.org"})):
        assert broken[name].run(args, None).is_error


def test_policy_for_texts(tmp_path):
    p = Policy(make_settings(tmp_path), workspace=tmp_path)
    v = p.check("send_text", {"to": "+1 503 555 0100", "message": "Running late"}, "android")
    assert v.action == ASK and v.scope == "text:+15035550100" and "Running late" in v.reason
    assert p.check("send_text", {"to": "73822", "message": "YES"}, "android").action == BLOCK
    assert p.check("send_text", {"to": "+15035550100", "message": "  "}, "android").action == BLOCK
    card = p.check("send_text", {"to": "+15035550100", "message": "card 4111 1111 1111 1111"}, "android")
    assert card.action == BLOCK and "card number" in card.reason
    assert p.check("search_messages", {"query": "wifi"}, "android").action == "allow"
    assert p.check("dial", {"number": "911"}, "android").action == "allow"


def test_server_policy_keeps_to_the_workspace(tmp_path):
    p = Policy(make_settings(tmp_path), workspace=tmp_path / "ws", home=tmp_path, outside_workspace="none")
    (tmp_path / "ws").mkdir()
    assert p.check("read_file", {"path": "notes.txt"}, "phone").action == "allow"
    assert p.check("read_file", {"path": "/etc/passwd"}, "phone").action == BLOCK
    assert p.check("write_file", {"path": str(tmp_path / "x.txt")}, "phone").action == BLOCK
    assert p.check("write_file", {"path": "ok.txt"}, "phone").action == "allow"
    assert p.check("read_file", {"path": str(tmp_path / ".argus" / "secrets.env")}, "phone").action == BLOCK


def approval(command="text Priya: Running late", choices=("once", "session", "deny")):
    return ApprovalRequest(run_id="r", command=command, choices=list(choices), origin="android")


@pytest.mark.parametrize("dialog,finger,expected", [
    ({"code": -1, "index": 0}, "AUTH_RESULT_SUCCESS", ONCE),
    ({"code": -1, "index": 1}, "AUTH_RESULT_SUCCESS", TASK),
    ({"code": -1, "index": 2}, "AUTH_RESULT_SUCCESS", DENY),
    ({"code": -2}, "AUTH_RESULT_SUCCESS", DENY),
    ({"code": -1, "index": 0}, "AUTH_RESULT_FAILURE", DENY),
])
def test_phone_approvals_need_the_dialog_and_a_fingerprint(dialog, finger, expected):
    fake = FakeTermux(termux_dialog=dialog, termux_fingerprint={"auth_result": finger})
    assert AndroidApprover(Termux(run=fake)).ask(approval()) == expected
    assert fake.names()[0] == "termux-notification" and fake.names()[-1] == "termux-notification-remove"
    if dialog.get("code") == -1 and dialog.get("index") != 2:
        assert "termux-fingerprint" in fake.names()
    else:
        assert "termux-fingerprint" not in fake.names()


def test_phone_approvals_fail_closed():
    once_only = FakeTermux(termux_dialog={"code": -1, "index": 1}, termux_fingerprint={"auth_result": "AUTH_RESULT_SUCCESS"})
    assert AndroidApprover(Termux(run=once_only)).ask(approval(choices=("once", "deny"))) == DENY   # "Don't allow"
    assert "Allow once,Don't allow" in once_only.calls[1][0][-1]
    broken = FakeTermux(termux_dialog=FileNotFoundError, termux_notification_remove=FileNotFoundError)
    assert AndroidApprover(Termux(run=broken)).ask(approval()) == DENY


# ---- the Android app -------------------------------------------------------------------------------------------------
@pytest.fixture
def phone_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    from mock_claude import ClaudeMock

    def build(responder, *, termux=None, google=None, **overrides):
        from argus import config
        from argus.android_app import AndroidApp
        from argus.maps import Router, http_json
        mock = ClaudeMock(responder).start()
        s = make_settings(tmp_path, phone_type="android", api_base=mock.url, self_learning=False, **overrides)
        config.ensure_private_dirs(s)
        secrets = {"ANTHROPIC_API_KEY": "sk-ant-test"}
        request = http_json
        if google is not None:
            secrets["GOOGLE_MAPS_API_KEY"] = "AIza-test"
            request = routed_to(google, "/directions")
        fake = termux or FakeTermux()
        app = AndroidApp(s, secrets=secrets, termux=Termux(run=fake), router=Router(s, secrets, request=request))
        return app, mock, fake
    return build


def test_android_app_searches_messages_and_answers_out_loud(phone_app):
    from fakes import Script
    script = Script(call("search_messages", {"query": "wifi password"}), say("Priya said it's maple-42."))
    app, mock, fake = phone_app(script, termux=FakeTermux(termux_sms_list=SMS,
                                                          termux_speech_to_text="what's the wifi password\n"))
    result = app.listen_and_answer()
    assert result["reply"] == "Priya said it's maple-42." and result["heard"] == "what's the wifi password"
    assert fake.calls[-1] == (["termux-tts-speak"], "Priya said it's maple-42.")
    tools = {t["name"] for t in script.bodies[0]["tools"]}
    assert {"search_messages", "send_text", "dial", "open_url", "route", "read_file", "schedule"} <= tools
    assert "run_command" not in tools and "open_app" not in tools and "glasses_look" not in tools
    system = script.bodies[0]["system"][0]["text"]
    assert "You run on Sandeep's Android phone" in system and "iPhone Mirroring" not in system
    result_block = script.bodies[1]["messages"][-1]["content"][0]
    assert "The wifi password is maple-42" in result_block["content"]


def test_android_app_directions_open_google_maps(phone_app):
    from fakes import Script
    google = JsonService(lambda request: (200, {"routes": [{"duration": "900s", "staticDuration": "800s",
                                                            "distanceMeters": 8000, "description": "US-26 E"}]}))
    script = Script(call("route", {"destination": "Powell's Books, Portland"}), say("About fifteen minutes via US-26."))
    fake = FakeTermux(termux_location={"latitude": 45.5, "longitude": -122.6, "accuracy": 20})
    app, _, _ = phone_app(script, termux=fake, google=google)
    reply = app.ask("how long to Powell's?")
    assert reply["open"] == "https://www.google.com/maps/dir/?api=1&destination=Powell%27s+Books%2C+Portland&travelmode=driving"
    assert ["termux-open-url", reply["open"]] in [args for args, _ in fake.calls]
    assert google.requests[0]["body"]["origin"]["location"]["latLng"] == {"latitude": 45.5, "longitude": -122.6}
    assert "The phone shares where it is" in script.bodies[0]["system"][1]["text"]


def test_android_app_location_falls_back_to_the_last_fix(phone_app):
    from fakes import Script

    def location(args, stdin):
        if args[2] == "network":
            raise subprocess.TimeoutExpired("termux-location", 30)
        return {"latitude": 1.5, "longitude": 2.5}
    app, _, _ = phone_app(Script(), termux=FakeTermux(termux_location=location))
    assert app.locate() == {"lat": 1.5, "lon": 2.5}
    app2, _, _ = phone_app(Script(), termux=FakeTermux(termux_location=FileNotFoundError))
    assert app2.locate() is None


def test_android_app_asks_for_a_fingerprint_before_texting(phone_app):
    from fakes import Script
    script = Script(call("send_text", {"to": "+15035550100", "message": "Running 10 minutes late"}), say("Sent."))
    fake = FakeTermux(termux_dialog={"code": -1, "index": 0}, termux_fingerprint={"auth_result": "AUTH_RESULT_SUCCESS"})
    app, _, _ = phone_app(script, termux=fake)
    assert app.ask("tell Priya I'm running late")["reply"] == "Sent."
    names = fake.names()
    assert names.index("termux-dialog") < names.index("termux-fingerprint") < names.index("termux-sms-send")
    denied = FakeTermux(termux_dialog={"code": -2})
    script2 = Script(call("send_text", {"to": "+15035550100", "message": "hi"}), say("Okay, I won't."))
    app2, _, _ = phone_app(script2, termux=denied)
    assert app2.ask("text Priya hi")["reply"] == "Okay, I won't." and "termux-sms-send" not in denied.names()


def test_android_app_reminders_notifications_and_status(phone_app):
    from fakes import Script
    fake = FakeTermux(termux_notification_list=[{"packageName": "com.whatsapp", "title": "Sam", "content": "Gate 4471"}])
    app, _, _ = phone_app(Script(), termux=fake)
    app.deliver("Reminder: • call home")
    assert any(args[:2] == ["termux-notification", "--id"] and "Reminder: - call home" in args for args, _ in fake.calls)
    assert fake.calls[-1] == (["termux-tts-speak"], "Reminder: - call home")
    assert app.poll_notifications() == 1 and app.poll_notifications() == 0
    status = app.status()
    assert status["mode"] == "android" and status["whatsapp"] == "not set up" and status["busy"] is False
    assert app.ask("   ") == {"ok": False, "reply": ""}
    app.busy.acquire()
    assert "still working" in app.ask("hello")["reply"]
    app.busy.release()
    quiet, _, quiet_fake = phone_app(Script(), android_speak=False, android_messages=False,
                                     termux=FakeTermux(termux_notification=FileNotFoundError, termux_tts_speak=FileNotFoundError))
    quiet.deliver("Reminder: x")
    assert "termux-tts-speak" not in quiet_fake.names() and quiet.poll_notifications() == 0
    assert quiet.listen_and_answer()["ok"] is False                 # termux-speech-to-text isn't answering


def test_android_app_handlers_and_scheduled_tasks(phone_app):
    from fakes import Script
    app, _, fake = phone_app(Script(say("It's sunny."), say("Done the task.")),
                             termux=FakeTermux(termux_speech_to_text=""))
    handlers = app.handlers()
    assert handlers["ask"]({"text": "weather?", "speak": False})["reply"] == "It's sunny."
    assert app.run_task("water plants") == "Done the task."
    assert handlers["say"]({"text": "hello"}) == {"ok": True} and fake.calls[-1] == (["termux-tts-speak"], "hello")
    assert handlers["talk"]({}) == {"ok": False, "reply": ""}          # heard nothing
    assert fake.calls[-1] == (["termux-tts-speak"], "I didn't catch that.")
    assert handlers["status"]({})["ok"]


class DriveTermux(FakeTermux):
    def __init__(self, *, plugged="PLUGGED_USB", speed=20.0, answer="Portland Airport"):
        self.state = {"plugged": plugged, "speed": speed}
        super().__init__(termux_battery_status=lambda a, s: {"plugged": self.state["plugged"]},
                         termux_location=lambda a, s: {"latitude": 45.5, "longitude": -122.6, "speed": self.state["speed"]},
                         termux_speech_to_text=answer)


def test_drive_assist_asks_once_and_warns_about_a_faster_route(phone_app, monkeypatch):
    from fakes import Script
    routes = [[Route(1500, via="I-205 N"), Route(1700, via="I-5 N")],           # at the start: I-205 is best
              [Route(1500, via="I-205 N")],                                     # later: no change
              [Route(1200, via="I-84 E"), Route(1900, via="I-205 N")],          # then a jam on I-205
              [Route(100, via="Nowhere")]]
    fake = DriveTermux()
    clock = [NOW]
    app, _, _ = phone_app(Script(say("Take I-205; about 25 minutes.")), termux=fake, android_drive_assist=True)
    app.clock = lambda: clock[0]
    monkeypatch.setattr(app.router, "plan", lambda *a, **k: ("Google Maps", routes.pop(0)))
    assert app.drive_tick() == "trip started"
    assert ["termux-tts-speak"] == fake.calls[[a for a, _ in fake.calls].index(["termux-tts-speak"])][0]
    assert any(stdin == "Where are you headed?" for _, stdin in fake.calls)
    assert any(stdin == "Take I-205; about 25 minutes." for _, stdin in fake.calls)
    assert app.drive_tick() == "on a trip"
    clock[0] += 601
    assert app.drive_tick() == "no better route"
    clock[0] += 601
    assert app.drive_tick() == "faster route"
    warning = [s for _, s in fake.calls if s and s.startswith("Traffic has changed")][-1]
    assert warning == "Traffic has changed. Going via I-84 E is now 12 min faster, 20 min in all. I'll open it in Maps."
    assert app.trip["via"] == "I-84 E"
    clock[0] += 601
    assert app.drive_tick() == "route unknown"
    fake.state["speed"] = 0
    clock[0] += 601
    assert app.drive_tick() == "stopped"
    clock[0] += 601
    assert app.drive_tick() == "trip over" and app.trip is None
    fake.state["speed"] = 20
    assert app.drive_tick() == "asked recently"                       # not again for two hours
    fake.state["plugged"] = "UNPLUGGED"
    assert app.drive_tick() == "not charging"


def test_drive_assist_stays_quiet_when_it_should(phone_app):
    from fakes import Script
    app, _, fake = phone_app(Script(), termux=DriveTermux(speed=1.0), android_drive_assist=True)
    assert app.drive_tick() == "not moving"
    app2, _, fake2 = phone_app(Script(), termux=DriveTermux(answer="nowhere."), android_drive_assist=True)
    assert app2.drive_tick() == "no destination"
    app3, _, _ = phone_app(Script(), termux=FakeTermux(termux_battery_status=FileNotFoundError))
    assert app3.drive_tick() is None
    app4, _, _ = phone_app(Script(), termux=FakeTermux(termux_battery_status={"plugged": "PLUGGED_AC"},
                                                       termux_location=FileNotFoundError))
    assert app4.drive_tick() is None
    app5, _, _ = phone_app(Script(), termux=FakeTermux(termux_battery_status={"plugged": "PLUGGED_AC"},
                                                       termux_location=""))
    assert app5.drive_tick() is None


def test_drive_assist_survives_traffic_lookups_failing(phone_app, monkeypatch):
    from argus.maps import MapsError
    from fakes import Script
    app, _, _ = phone_app(Script(say("Okay.")), termux=DriveTermux(), android_drive_assist=True)
    monkeypatch.setattr(app.router, "plan", lambda *a, **k: (_ for _ in ()).throw(MapsError("down")))
    assert app.drive_tick() == "trip started" and app.trip["via"] is None
    app.trip["checked"] = 0
    assert app.drive_tick() == "no traffic data"


def test_android_app_starts_its_background_jobs(phone_app, monkeypatch):
    from argus import android_app
    from fakes import Script
    monkeypatch.setattr(android_app, "POLL_SECONDS", 0.05)
    fake = FakeTermux(termux_notification_list=[{"packageName": "com.whatsapp", "title": "Sam", "content": "hi"}],
                      termux_wake_lock=FileNotFoundError)
    app, _, _ = phone_app(Script(), termux=fake, android_drive_assist=True)
    app.start()
    try:
        assert wait_for(lambda: "termux-notification-list" in fake.names(), 5)
        assert app.notes.search(["hi"])
    finally:
        app.stop()
    crashing, _, _ = phone_app(Script())
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("oops")
    t = threading.Thread(target=crashing._every, args=(0.01, boom), daemon=True)
    t.start()
    assert wait_for(lambda: len(calls) >= 2, 3)
    crashing._stop.set()


def test_argus_android_command_line(tmp_path, monkeypatch, capsys):
    from argus.__main__ import main
    from argus.control import ControlServer
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    spoken = []
    monkeypatch.setattr(android.Termux, "speak", lambda self, text: spoken.append(text))
    assert main(["android", "status"]) == 1 and "Argus isn't running" in capsys.readouterr().out
    assert main(["android", "talk"]) == 1 and spoken == ["Argus isn't running yet."]
    from argus import config
    s = config.load()
    config.ensure_private_dirs(s)
    server = ControlServer(s.socket_path, {"status": lambda r: {"ok": True, "mode": "android"},
                                           "talk": lambda r: {"ok": True, "reply": f"You said {r.get('text')}"}})
    server.start()
    try:
        assert main(["android", "ask", "where", "am", "I"]) == 0 and "You said where am I" in capsys.readouterr().out
        assert main(["android", "status"]) == 0 and '"mode": "android"' in capsys.readouterr().out
    finally:
        server.stop()
    (tmp_path / "home" / "config.toml").write_text("[phone]\ntype = 3\n")
    assert main(["android", "status"]) == 2


def test_android_serve_runs_until_stopped(tmp_path, monkeypatch):
    import signal
    from argus import android_app, config
    from argus.control import send
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(android_app, "Termux", lambda: Termux(run=FakeTermux()))
    handlers = {}
    monkeypatch.setattr(android_app.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    (tmp_path / "home").mkdir(mode=0o700)
    (tmp_path / "home" / "config.toml").write_text('[phone]\ntype = "android"\n[android]\nmessages = false\n')
    assert android_app.serve() == 2                                  # no Claude key yet
    (tmp_path / "home" / "secrets.env").write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
    (tmp_path / "home" / "secrets.env").chmod(0o644)
    assert android_app.serve() == 2                                  # readable by others: refused
    (tmp_path / "home" / "secrets.env").chmod(0o600)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", android_app.serve()), daemon=True)
    t.start()
    s = config.load()
    assert wait_for(lambda: s.socket_path.exists(), 10)
    assert wait_for(lambda: signal.SIGTERM in handlers, 5)
    assert send(s.socket_path, "status")["mode"] == "android"
    assert android_app.serve() == 0                                  # a second copy just says it's running
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    t.join(10)
    assert result["code"] == 0
    (tmp_path / "home" / "config.toml").write_text("[maps]\napp = 7\n")
    assert android_app.serve() == 2


def test_android_app_copes_when_the_phone_says_no(phone_app, tmp_path, monkeypatch):
    from fakes import Script
    fake = FakeTermux(termux_location={"latitude": 45.5, "longitude": -122.6}, termux_open_url=FileNotFoundError,
                      termux_speech_to_text=FileNotFoundError, termux_notification_list=FileNotFoundError)
    script = Script(call("route", {"destination": "PDX"}), say("Twenty minutes."))
    app, _, _ = phone_app(script, termux=fake)
    reply = app.ask("traffic to PDX?")
    assert reply["open"].startswith("https://www.google.com/maps/dir/") and reply["reply"] == "Twenty minutes."
    assert "couldn't listen" in app.listen_and_answer()["reply"]
    assert app.poll_notifications() == 0


def test_android_app_with_whatsapp_set_up(phone_app, monkeypatch, tmp_path):
    from argus import android_app
    from fakes import Script
    monkeypatch.setattr(android_app.AndroidApp, "__init__", android_app.AndroidApp.__init__)
    app, _, _ = phone_app(Script())
    assert app.whatsapp is None
    from argus.android_app import AndroidApp
    s = app.settings
    with_key = AndroidApp(s, secrets={"ANTHROPIC_API_KEY": "sk-ant-test", "WHATSAPP_BACKUP_KEY": KEY},
                          termux=Termux(run=FakeTermux()))
    assert with_key.whatsapp is not None and "isn't installed" in with_key.status()["whatsapp"]


def test_drive_assist_odd_moments(phone_app, monkeypatch):
    from fakes import Script
    fake = DriveTermux()
    fake.answers["termux-speech-to-text"] = FileNotFoundError
    app, _, _ = phone_app(Script(say("ok")), termux=fake, android_drive_assist=True)
    app.store.set("drive_asked_at", "not a number")
    assert app.drive_tick() == "no destination"                       # couldn't hear an answer
    fake2 = DriveTermux()
    fake2.answers["termux-open-url"] = FileNotFoundError
    app2, _, _ = phone_app(Script(say("ok")), termux=fake2, android_drive_assist=True)
    app2.store.set("drive_asked_at", "0")                              # (the same phone: forget the first app's ask)
    routes = [[], [Route(1200, via="A"), Route(1900, via="B")]]
    monkeypatch.setattr(app2.router, "plan", lambda *a, **k: ("Google Maps", routes.pop(0)))
    assert app2.drive_tick() == "trip started" and app2.trip["via"] is None
    app2.trip.update(via="B", checked=0)
    assert app2.drive_tick() == "faster route"                         # opening Maps failed: still announced


def test_android_command_line_serve_and_quiet_replies(tmp_path, monkeypatch, capsys):
    from argus import android_app
    from argus.control import ControlServer
    from argus import config
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(android_app, "serve", lambda: 5)
    assert android_app.main(["serve"]) == 5
    s = config.load()
    config.ensure_private_dirs(s)
    server = ControlServer(s.socket_path, {"talk": lambda r: {"ok": False}})
    server.start()
    try:
        assert android_app.main(["talk"]) == 1 and capsys.readouterr().out == ""
    finally:
        server.stop()


def test_whatsapp_edge_cases(tmp_path, monkeypatch):
    plain = tmp_path / "plain.db"
    db = sqlite3.connect(plain)
    db.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
        CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
                              sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT);
        INSERT INTO chat VALUES (1, NULL, NULL);
        INSERT INTO message VALUES (1, 1, 0, 0, 1762790000000, 'mystery message');
        """)
    db.commit()
    db.close()
    wa = WhatsAppBackup(KEY, tmp_path / "w.db", tool=fake_wadecrypt(tmp_path, plain), folder=str(backup_folder(tmp_path)))
    assert [(m.chat, m.who) for m in wa.messages(["mystery"])] == [("unknown", "unknown")]
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
    wa2 = WhatsAppBackup(KEY, tmp_path / "w2.db", tool=fake_wadecrypt(tmp_path, broken),
                         folder=str(backup_folder(tmp_path)))
    with pytest.raises(TermuxError, match="couldn't read the WhatsApp backup"):
        wa2.messages(["x"])
    real_glob = Path.glob

    def picky(self, pattern):
        if "nowhere" in str(self):
            raise PermissionError("not allowed")
        return real_glob(self, pattern)
    monkeypatch.setattr(Path, "glob", picky)
    wa3 = WhatsAppBackup(KEY, tmp_path / "w3.db", tool=tmp_path / "wadecrypt", folder=str(tmp_path / "nowhere"))
    assert wa3.backup() is None
