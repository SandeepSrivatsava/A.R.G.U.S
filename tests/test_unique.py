"""What makes Argus Argus: it books by phone, it follows up on loose ends by itself, and it remembers what the
glasses saw. Plus the quick-voice fallback, keys and extra brains from the command line."""
import base64
import json
import time
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from argus import config, council, followups, search, sightings
from argus.__main__ import cmd_brains, cmd_key, cmd_voices
from argus.agent import Events
from argus.scheduler import Scheduler
from argus.store import Store
from argus.toolbox import Context, ToolOutput
from argus.voice import AdaptiveVoice, make_voice

from fakes import Script, call, make_agent, make_settings, say
from test_conversation import PLACES, FakeGoogle

TZ = "America/Los_Angeles"
NOON = datetime(2026, 11, 12, 12, 0).timestamp()          # the tests' clock: local noon


def ctx_for(tmp_path, channel="voice", **kw):
    return Context(channel=channel, workspace=tmp_path, store=None, **kw)


# ---- booking: find it, call, report --------------------------------------------------------------------------
def test_places_come_with_a_phone_number_and_use_where_the_phone_is(tmp_path):
    fake = FakeGoogle()
    fake.places = (200, {"places": [dict(PLACES["places"][0], nationalPhoneNumber="(510) 555-0142", reservable=True)]})
    tools = {}
    search.attach(make_settings(tmp_path), {"GOOGLE_MAPS_API_KEY": "k"}, tools, request=fake, where=lambda: (1.0, 2.0))
    out = tools["place_search"].run({"query": "Udupi Palace"}, ctx_for(tmp_path, "phone", location={"lat": 37.5, "lon": -122.0}))
    assert "phone: (510) 555-0142 (takes reservations)" in out.content
    assert "nationalPhoneNumber" in fake.calls[0][1]["X-Goog-FieldMask"]
    assert fake.calls[0][2]["locationBias"]["circle"]["center"] == {"latitude": 37.5, "longitude": -122.0}
    tools["place_search"].run({"query": "x"}, ctx_for(tmp_path, locate=lambda: None))
    assert fake.calls[1][2]["locationBias"]["circle"]["center"] == {"latitude": 1.0, "longitude": 2.0}

    def broken():
        raise RuntimeError("no fix")
    assert search._here(SimpleNamespace(location=None, locate=broken)) is None
    assert search._here(SimpleNamespace(location={"lat": "x"}, locate=None)) is None


def test_find_a_place_then_phone_it_to_book(tmp_path):
    fake = FakeGoogle()
    fake.places = (200, {"places": [dict(PLACES["places"][0], nationalPhoneNumber="(510) 555-0142")]})
    script = Script(call("place_search", {"query": "Indian restaurant Portland", "open_now": True}),
                    say("Udupi Palace, 4.4 stars, people love the dosas. Want me to call and book a table for two at seven?"),
                    call("phone_call", {"number": "(510) 555-0142", "business": "Udupi Palace",
                                        "goal": "book a table", "details": "2 people, 7 pm tonight, name Sam"}),
                    say("Done: a table for two at seven, under Sam."))
    agent, _ = make_agent(tmp_path, script)
    search.attach(make_settings(tmp_path), {"GOOGLE_MAPS_API_KEY": "k"}, agent.tools, request=fake)
    from argus.toolbox import Tool
    called = []
    agent.tools["phone_call"] = Tool("phone_call", "call", {"type": "object"},
                                     lambda args, ctx: (called.append(args), ToolOutput("Outcome: booked. Table for 2 at 7."))[1],
                                     frozenset({"voice"}))

    class Yes(Events):
        asked = []

        def approve(self, request):
            from argus.approvals import ONCE
            Yes.asked.append(request.description)
            return ONCE
    first = agent.run(session_id="s", channel="voice", content="find a good Indian place for 7 tonight", events=Yes())
    assert first.text.endswith("at seven?") and called == []
    second = agent.run(session_id="s", channel="voice", content="yes", events=Yes())
    assert second.text.startswith("Done") and called[0]["business"] == "Udupi Palace"
    assert len(Yes.asked) == 1 and "call Udupi Palace at" in Yes.asked[0]          # your OK before it dials
    system = json.dumps(script.bodies[0]["system"])
    assert "offer to book it" in system and "scripts/setup_calls.sh" in system


# ---- loose ends ----------------------------------------------------------------------------------------------
def test_adding_listing_and_closing_loose_ends(tmp_path):
    store, tools = Store(tmp_path / "a.db"), {}
    followups.attach(store, tools, tz=TZ, clock=lambda: NOON)
    ctx = ctx_for(tmp_path)
    out = tools["follow_up"].run({"action": "add", "what": "New York: no  hotel yet",
                                  "check": "hotel prices in Midtown for Dec 12-15, 2026", "first_in_days": 2}, ctx)
    assert out.content.startswith("Watching it (#1); first look Saturday 14 November")
    assert "already being watched" in tools["follow_up"].run({"action": "add", "what": "new york: no hotel yet"}, ctx).content
    assert followups.in_prompt(store) == [(1, "New York: no hotel yet")]
    listing = tools["follow_up"].run({"action": "list"}, ctx).content
    assert listing == "#1 New York: no hotel yet (next look: Sat 14 Nov 12:00)"
    night = tools["follow_up"].run({"action": "add", "what": "landlord reply", "first_in_days": 0.45}, ctx).content
    assert "Friday 13 November" in night                              # 22:48 tonight -> 8:00 tomorrow
    assert store.tasks()[0]["next_run"] == datetime(2026, 11, 13, 8, 0).timestamp()
    for bad in ({"action": "add"}, {"action": "done", "id": 99}, {"action": "done"}, {"action": "sing"}):
        assert tools["follow_up"].run(bad, ctx).is_error
    assert tools["follow_up"].run({"action": "done", "id": 1}, ctx).content == "Closed."
    assert [i for i, _, _ in followups.open_ends(store)] == [2]
    for n in range(followups.MAX_OPEN - 1):
        assert not tools["follow_up"].run({"action": "add", "what": f"thing {n}"}, ctx).is_error
    assert "close one first" in tools["follow_up"].run({"action": "add", "what": "one too many"}, ctx).content
    store.cancel_task(2)
    for i, _, _ in followups.open_ends(store):
        store.cancel_task(i)
    assert tools["follow_up"].run({"action": "list"}, ctx).content == "No loose ends are being watched."
    assert set(tools["follow_up"].channels) == {"voice", "phone", "cli", "android"}


def test_a_check_speaks_only_when_there_is_something_to_say(tmp_path):
    store = Store(tmp_path / "a.db")
    end = {"what": "NYC hotel", "check": "hotel prices", "every": 3, "until": NOON + 10 * 86400}
    store.add_task(json.dumps(end), "followup", due=NOON - 60)
    store.add_task("not json", "followup", due=NOON - 60)
    said, answers = [], ["", "Hotel prices for your dates went up about 15%. Want me to look now?"]
    asked = []
    scheduler = Scheduler(store, deliver=said.append, run_task=lambda t: "", headlines=lambda: "", tz=TZ,
                          follow_check=lambda e: (asked.append(e["what"]), answers.pop(0))[1])
    assert scheduler.tick(NOON) == 2
    assert said == [] and asked == ["NYC hotel"]                         # nothing new: silence
    (task,) = store.tasks()
    assert task["next_run"] == NOON + 3 * 86400
    assert scheduler.tick(NOON + 3 * 86400 + 1) == 1
    assert said == ["Hotel prices for your dates went up about 15%. Want me to look now?"]
    late = datetime(2026, 11, 18, 23, 30).timestamp()
    store.finish_task(task["id"], late - 5)
    assert scheduler.tick(late) == 1 and len(asked) == 2                 # at night: waits for 8 in the morning
    assert store.tasks()[0]["next_run"] == datetime(2026, 11, 19, 8, 0).timestamp()
    store.finish_task(task["id"], NOON + 9.9 * 86400)
    answers.append("")
    assert scheduler.tick(NOON + 9.9 * 86400 + 1) == 1 and store.tasks() == []      # past its time: closed
    assert followups.due({"text": json.dumps(end)}, NOON + 11 * 86400, lambda e: "x", said.append, TZ) is None
    assert followups.out_of_quiet_hours(datetime(2026, 11, 12, 3, 0).timestamp(), "Not/AZone") == \
        datetime(2026, 11, 12, 8, 0).timestamp()


def test_a_check_can_only_look_things_up(tmp_path):
    script = Script(call("weather", {"place": "New York"}), say("NOTHING"), say("Snow is forecast for your dates. Still going?"))
    agent, _ = make_agent(tmp_path, script)
    from argus.toolbox import Tool
    agent.tools["weather"] = Tool("weather", "w", {"type": "object"}, lambda a, c: ToolOutput("cold"),
                                  frozenset({"voice", "followup"}))
    followups.attach(agent.store, agent.tools)
    agent.loose_ends = lambda: followups.in_prompt(agent.store)
    check = followups.make_check(agent, agent.store, "Sam")
    end = {"what": "NYC trip: pack for the weather", "check": "weather in New York Dec 12-15"}
    assert check(end) == ""
    assert check(end) == "Snow is forecast for your dates. Still going?"
    names = [t.get("name") for t in script.bodies[0]["tools"]]
    assert names == ["weather"]                                          # no files, shell, mail, calls or follow_up
    assert script.bodies[0]["model"] == "claude-sonnet-5-5"
    assert "Loose end: NYC trip: pack for the weather" in json.dumps(script.bodies[0]["messages"])
    assert "a loose end Sandeep left open" in json.dumps(script.bodies[0]["system"])
    # in a normal conversation the open ends are in view, and the tool is offered
    agent.tools["follow_up"].run({"action": "add", "what": "NYC hotel"}, ctx_for(tmp_path))
    more = Script(say("ok"), say("hello"))
    agent2, _ = make_agent(tmp_path / "b", more)
    followups.attach(agent2.store, agent2.tools)
    agent2.loose_ends = lambda: [(7, "NYC hotel")]
    agent2.run(session_id="s", channel="voice", content="what's still open?", events=Events())
    assert "#7 NYC hotel" in json.dumps(more.bodies[0]["system"])
    assert "follow_up" in [t.get("name") for t in more.bodies[0]["tools"]]
    agent2.run(session_id="g", channel="guest", content="hi", events=Events())
    assert len(more.bodies) == 2 and "NYC hotel" not in json.dumps(more.bodies[-1]["system"])


# ---- what the glasses saw --------------------------------------------------------------------------------------
def test_where_did_i_put_my_keys(tmp_path):
    store, tools, now = Store(tmp_path / "a.db"), {}, [1_000_000.0]
    sightings.attach(store, tools, clock=lambda: now[0])
    ctx = ctx_for(tmp_path)
    assert "glances" not in tools
    assert tools["find_seen"].run({"thing": "keys"}, ctx).content == "The glasses haven't seen that in the last two weeks."
    tools["note_seen"].run({"things": [{"thing": "keys", "where": "kitchen counter, by the fruit bowl"},
                                       {"thing": "wallet", "where": "desk drawer"}, {"thing": "", "where": "x"}, "junk"]}, ctx)
    now[0] += 3 * 3600
    tools["note_seen"].run({"things": [{"thing": "car key", "where": "jacket pocket on the door hook"}]}, ctx)
    now[0] += 600
    out = tools["find_seen"].run({"thing": "where did I put my keys"}, ctx).content
    assert out.splitlines()[1:] == ["- car key: jacket pocket on the door hook (10 minutes ago)",
                                    "- keys: kitchen counter, by the fruit bowl (3 hours ago)"]
    assert tools["find_seen"].run({"thing": "the"}, ctx).content.startswith("The glasses haven't")
    now[0] += 15 * 86400
    tools["note_seen"].run({"things": [{"thing": "phone", "where": "sofa"}]}, ctx)
    assert [thing for _, thing, _ in store.sightings()] == ["phone"]             # two weeks, then forgotten
    assert [sightings.ago(s) for s in (5, 600, 7200, 3 * 86400)] == ["just now", "10 minutes ago", "2 hours ago", "3 days ago"]
    assert "glance" in tools["note_seen"].channels and "glance" not in tools["find_seen"].channels


def test_glances_note_things_and_drop_the_photo(tmp_path):
    script = Script(call("note_seen", {"things": [{"thing": "keys", "where": "hallway shelf"}]}), say("ok"), say("ok"))
    agent, _ = make_agent(tmp_path, script)
    now = [0.0]
    from argus.audit import Audit
    glancer = sightings.Glancer(agent, agent.store, Audit(tmp_path / "audit.jsonl"), minutes=3, clock=lambda: now[0])
    sightings.attach(agent.store, agent.tools, glancer=glancer)
    photo = base64.b64encode(b"\xff\xd8 jpeg bytes").decode()
    assert glancer.interval == 180 and glancer.glance(photo) is True
    assert not glancer.glance(photo)                                             # too soon
    body = script.bodies[0]
    assert body["model"] == "claude-haiku-5-5"
    assert [t.get("name") for t in body["tools"]] == ["note_seen"]              # nothing else, not even web search
    assert agent.store.sightings()[0][1:] == ("keys", "hallway shelf")
    saved = agent.store._q("SELECT content FROM messages")
    assert saved == [] and photo not in (tmp_path / "audit.jsonl").read_text()   # the photo isn't kept anywhere
    ctx = ctx_for(tmp_path)
    assert "paused" in agent.tools["glances"].run({"action": "pause"}, ctx).content and glancer.interval == 0
    now[0] += 1000
    assert not glancer.glance(photo)
    assert "every 3 minutes" in agent.tools["glances"].run({"action": "resume"}, ctx).content
    assert glancer.glance(photo) is True
    assert "every 3 minutes" in agent.tools["glances"].run({"action": "status"}, ctx).content


def test_the_phone_gateway_takes_glances_only_when_switched_on(tmp_path):
    import urllib.request
    from argus.audit import Audit
    from argus.glasses import GlassesBridge
    from argus.phone import Gatekeeper, PhoneServer
    taken = []
    glancer = SimpleNamespace(interval=180, glance=lambda b64: (taken.append(b64), True)[1])
    token = "t" * 43

    def post(server, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}", json.dumps(body).encode(),
                                     {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 64).decode()
    for with_glancer in (glancer, None):
        server = PhoneServer(("127.0.0.1", 0), lambda c, l=None: {"reply": "x"}, Gatekeeper(token), Audit(tmp_path / "a.jsonl"),
                             GlassesBridge(), glancer=with_glancer)
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            status = post(server, "/glasses/status", {"connected": True, "device": "Ray-Ban Meta"})
            glance = post(server, "/glasses/glance", {"image": jpeg})
            if with_glancer:
                assert status == (200, {"ok": True, "glance_every": 180}) and glance == (200, {"ok": True, "taken": True})
                assert post(server, "/glasses/glance", {"image": "not an image"})[0] == 400
            else:
                assert status[1]["glance_every"] == 0 and glance[0] == 403
        finally:
            server.shutdown()
    assert len(taken) == 1


def test_glance_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    assert config.load().glances_minutes == 0
    (tmp_path / "config.toml").write_text("[glasses]\nglances_minutes = 3\n")
    assert config.load().glances_minutes == 3
    (tmp_path / "config.toml").write_text("[glasses]\nglances_minutes = 0.2\n")
    with pytest.raises(ValueError, match="glances_minutes"):
        config.load()


# ---- a voice that keeps up -----------------------------------------------------------------------------------
class FakeNeural:
    def __init__(self, work, sound=2.0):
        self.work, self.sound, self.timings, self.upcoming, self.said, self.stopped = work, sound, [], [], [], 0

    def speak(self, text):
        self.said.append(text)
        self.timings.append((time.monotonic(), self.work, self.sound))

    def reset(self):
        pass

    def stop(self):
        self.stopped += 1


def test_a_natural_voice_that_cant_keep_up_gives_way_to_the_quick_one(tmp_path):
    marker, switched = tmp_path / "natural-voice-too-slow", []
    slow, quick = FakeNeural(work=2.4), FakeNeural(work=0.2)
    voice = AdaptiveVoice(slow, quick, marker, switched.append)
    for n in range(6):
        voice.upcoming = [f"next {n}"]
        voice.reset()
        voice.speak(f"sentence {n}")
    assert len(slow.said) == 5 and len(quick.said) == 1 and slow.stopped == 1     # (the first one is a warm-up)
    assert switched == [pytest.approx(1.2)] and marker.read_text() == "1.20\n"
    assert voice.upcoming == ["next 5"] and voice.timings is quick.timings
    voice.stop()
    assert quick.stopped == 1
    fine = AdaptiveVoice(FakeNeural(work=1.9), quick, tmp_path / "other")   # 0.95: keeps up, just
    for n in range(8):
        fine.speak("x")
    assert fine.current is fine.natural and not (tmp_path / "other").exists()


def test_make_voice_remembers_the_switch_and_argusctl_can_undo_it(tmp_path, monkeypatch, capsys):
    import argus.voice as voice_mod
    made = []
    monkeypatch.setattr(voice_mod, "PiperVoice", lambda d, speed=1.0, tmp_dir=None: (made.append(("piper", speed)), FakeNeural(0.1))[1])
    monkeypatch.setattr(voice_mod, "KokoroVoice", lambda d, voice="", speed=1.0, tmp_dir=None: (made.append(("kokoro", speed)), FakeNeural(0.1))[1])
    voice = make_voice("kokoro", speed=1.2, tmp_dir=str(tmp_path))
    assert isinstance(voice, AdaptiveVoice) and made == [("piper", pytest.approx(1.1)), ("kokoro", 1.2)]
    (tmp_path / "natural-voice-too-slow").write_text("1.3\n")
    assert isinstance(make_voice("kokoro", speed=1.2, tmp_dir=str(tmp_path)), FakeNeural)       # straight to the quick one
    assert isinstance(make_voice("piper", speed=1.0, tmp_dir=str(tmp_path)), FakeNeural)
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    run = config.load().run_dir
    run.mkdir(parents=True, exist_ok=True)
    (run / "natural-voice-too-slow").write_text("1.3\n")
    assert cmd_voices(SimpleNamespace(name=None, speed=None, natural=True)) == 0
    assert not (run / "natural-voice-too-slow").exists() and "try the natural voice again" in capsys.readouterr().out


def test_voice_timings_reach_the_timing_log(tmp_path):
    from argus.voice import Speaker, _NeuralVoice

    class Quick(_NeuralVoice):
        def __init__(self):
            super().__init__()
            self._init_playback(["true"], str(tmp_path))

        def synthesize(self, text):
            return np.zeros(16000, dtype=np.float32), 16000

        def _run(self, args):
            pass
    began = time.monotonic()
    speaker = Speaker(Quick())
    speaker.say("One.")
    speaker.say("Two.")
    assert speaker.wait_idle(10)
    assert [sound for _, sound in speaker.voice_timings(began)] == [1.0, 1.0]
    assert speaker.voice_timings(time.monotonic() + 1) == []


# ---- interrupting needs to be sure ------------------------------------------------------------------------------
def test_one_misheard_echo_doesnt_cut_argus_off(tmp_path):
    from fakes import SPEECH, make_argus
    argus, voice, _ = make_argus(tmp_path, ["so what do you think about", "the museum is open until five",
                                            "no wait I meant tomorrow", "yes tomorrow is what I meant"],
                                 barge_in="words")          # stopping for any new words: headphones only
    argus.speaker._recent.append((argus.speaker.clock(), "The museum is open until five today."))
    stopped = []
    argus.brain.cancel = lambda: stopped.append(1)
    argus.segmenter.speech_active = True
    argus.segmenter.recent_audio = lambda: SPEECH
    argus.speaker._idle.clear()
    argus._speech_since = time.monotonic() - 1
    for _ in range(2):                         # different words once, then its own sentence: keeps talking
        argus._last_barge_check = 0.0
        argus._maybe_barge_in()
    assert stopped == [] and argus._barge_hits == 0
    for _ in range(2):                         # different words twice in a row: that's you
        argus._last_barge_check = 0.0
        argus._maybe_barge_in()
    assert stopped == [1]
    argus.speaker._idle.set()


# ---- keys and brains from the command line -------------------------------------------------------------------------
def test_argusctl_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    A = SimpleNamespace
    assert cmd_key(A(name=None, remove=False)) == 0 and "FLIGHTAWARE_API_KEY    not set" in capsys.readouterr().out
    assert cmd_key(A(name="flightaware_api_key", remove=False), ask_secret=lambda p: " fa-secret-12345 ") == 0
    assert "ending ...2345" in capsys.readouterr().out
    secrets = tmp_path / "secrets.env"
    assert secrets.read_text() == "FLIGHTAWARE_API_KEY=fa-secret-12345\n" and oct(secrets.stat().st_mode & 0o777) == "0o600"
    cmd_key(A(name=None, remove=False))
    out = capsys.readouterr().out
    assert "saved (ending ...2345)" in out and "fa-secret" not in out
    assert cmd_key(A(name="NOPE", remove=False)) == 1
    assert cmd_key(A(name="GEMINI_API_KEY", remove=False), ask_secret=lambda p: "") == 1
    assert cmd_key(A(name="GEMINI_API_KEY", remove=False), ask_secret=lambda p: "has a space") == 1
    assert cmd_key(A(name="FLIGHTAWARE_API_KEY", remove=True)) == 0 and secrets.read_text() == ""


def test_argusctl_brains_and_free_presets(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    A = SimpleNamespace
    fake = FakeGoogle()
    assert cmd_brains(A(action="list", name=None)) == 0
    out = capsys.readouterr().out
    assert "argusctl brains add groq" in out and "argusctl brains add gemini" in out and "  on " not in out
    assert "argusctl brains add chatgpt" in out and "argusctl brains add grok" in out and "pay-per-use" in out
    assert cmd_brains(A(action="add", name="skynet")) == 1
    assert cmd_brains(A(action="add", name="groq"), ask_secret=lambda p: "gsk_test_key_123", request=fake) == 0
    out = capsys.readouterr().out
    assert "console.groq.com/keys" in out and "Test: it answered" in out
    cmd_brains(A(action="list", name=None))
    out = capsys.readouterr().out
    assert "  on   Groq (gpt-oss-120b, OpenAI's open model)" in out and "brains add openrouter" in out
    team = council.members(make_settings(tmp_path), {"GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "o"})
    assert [(m.name, m.model) for m in team] == [("Groq (gpt-oss-120b, OpenAI's open model)", "openai/gpt-oss-120b"),
                                                  ("OpenRouter (free models)", "openrouter/free")]
    assert team[0].ask("q", fake) == "Go with Udupi." and fake.calls[-1][0] == "https://api.groq.com/openai/v1/chat/completions"


def test_the_report_shows_voice_and_interruptions(tmp_path, monkeypatch, capsys):
    from argus import diagnose
    from argus.audit import Audit
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    settings = config.load()
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    (settings.run_dir / "natural-voice-too-slow").write_text("1.2\n")
    settings.audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = Audit(settings.audit_path)
    audit.log("barge_in", why="new words", heard_words=4)
    audit.log("barge_in", why="your voice", score=0.71)
    audit.log("voice_switched", to="quick", work_per_second=1.2)
    audit.log("timing", first_word_s=2.1, voice=[[1.9, 2.2]])
    text = diagnose.build_report(settings, {})
    for expected in ("voice: kokoro am_michael at 1.2x (switched to the quick voice", "interrupting: voice",
                     "(note) Argus stopped talking because it heard new words",
                     "it heard your voice (voice match 0.71)", "so Argus switched to the quick voice",
                     "voice (made, sound) [[1.9, 2.2]]"):
        assert expected in text, expected


def test_one_sentence_is_made_at_a_time_and_only_the_next_one_ahead(tmp_path):
    """Making several sentences at once slowed each of them, so a fast enough voice measured too slow (0.99)."""
    from argus.voice import _NeuralVoice
    import threading

    class Counting(_NeuralVoice):
        def __init__(self):
            super().__init__()
            self._init_playback(["true"], str(tmp_path))
            self.busy, self.most, self.made, self.guard = 0, 0, [], threading.Lock()

        def synthesize(self, text):
            with self.guard:
                self.busy += 1
                self.most = max(self.most, self.busy)
            time.sleep(0.05)
            with self.guard:
                self.busy -= 1
            self.made.append(text)
            return np.zeros(1600, dtype=np.float32), 16000

    voice = Counting()
    voice.upcoming = ["two", "three", "four"]
    voice.speak("one")
    time.sleep(0.3)
    assert voice.most == 1 and voice.made == ["one", "two"]       # "three" and "four" wait their turn


def test_movies_and_other_recommendations_go_to_the_main_model(tmp_path):
    from argus import tiers
    from fakes import make_settings
    s = make_settings(tmp_path, light_model="claude-haiku-5-5")
    for asked in ("find me a good movie", "any shows to watch tonight", "a book like Dune", "what time is it"):
        tier, _ = tiers.choose(asked, "voice", s)
        assert (tier == tiers.MAIN) == (asked != "what time is it"), asked


def test_claude_can_write_and_run_code_but_not_for_guests_or_the_quick_model(tmp_path):
    from argus import tiers
    agent, _ = make_agent(tmp_path, Script(say("ok")))
    names = lambda specs: [t.get("name") for t in specs]
    assert "code_execution" in names(agent._tools_for("voice", tier=tiers.MAIN))
    assert {"type": "code_execution_20250825", "name": "code_execution"} in [
        {k: v for k, v in t.items() if k != "cache_control"} for t in agent._tools_for("cli")]
    assert "code_execution" not in names(agent._tools_for("voice", tier=tiers.LIGHT))
    assert "code_execution" not in names(agent._tools_for("guest"))
    assert "code_execution" not in names(agent._tools_for("scheduler"))
    agent.settings.code_execution = False
    assert "code_execution" not in names(agent._tools_for("voice"))


def test_a_clear_match_of_your_voice_stops_argus_at_once(tmp_path):
    from fakes import SPEECH, make_argus
    argus, _, _ = make_argus(tmp_path, [])
    argus.barge_in = "voice"
    scores = iter([0.72, 0.45, 0.45])
    argus.voiceprint = type("Print", (), {"enrolled": True, "score": lambda self, audio: next(scores)})()
    stopped = []
    argus.brain.cancel = lambda: stopped.append(1)
    argus.segmenter.speech_active = True
    argus.segmenter.recent_audio = lambda: SPEECH
    argus.speaker._idle.clear()
    argus._speech_since = time.monotonic() - 1
    argus._last_barge_check = 0.0
    argus._maybe_barge_in()                    # 0.72: clearly you, stops on the first check
    assert stopped == [1]
    argus._last_barge_check = 0.0
    argus._maybe_barge_in()                    # 0.45: borderline, needs a second one
    assert stopped == [1] and argus._barge_hits == 1
    argus._last_barge_check = 0.0
    argus._maybe_barge_in()
    assert stopped == [1, 1]
    argus.speaker._idle.set()


def test_ticket_watches_can_check_every_hour_but_no_faster(tmp_path):
    store, tools = Store(tmp_path / "a.db"), {}
    followups.attach(store, tools, tz=TZ, clock=lambda: NOON)
    ctx = ctx_for(tmp_path)
    tools["follow_up"].run({"action": "add", "what": "film tickets", "check": "seats", "every_hours": 0.1,
                            "first_in_days": 0.02}, ctx)
    (task,) = store.tasks()
    end = json.loads(task["text"])
    assert end["every"] == 1 / 24                                         # clamped to an hour
    asked = []
    scheduler = Scheduler(store, deliver=lambda s: None, run_task=lambda t: "", headlines=lambda: "", tz=TZ,
                          follow_check=lambda e: asked.append(e["what"]) or "")
    scheduler.tick(task["next_run"] + 1)
    assert asked == ["film tickets"]
    assert abs(store.tasks()[0]["next_run"] - (task["next_run"] + 1 + 3600)) < 2


def test_argus_is_told_which_model_answers(tmp_path):
    script = Script(say("Haiku."), say("Opus."))
    agent, _ = make_agent(tmp_path, script)
    agent.settings.routing, agent.settings.council = "auto", ["x"]
    agent.run(session_id="m", channel="voice", content="what time is it", events=Events())
    agent.run(session_id="n", channel="voice", content="debug my python script", events=Events())
    first, second = (json.dumps(b["system"]) for b in script.bodies)
    assert f"this answer comes from {agent.settings.light_model} (the quick model)" in first
    assert "(the heavy model)" in second and "ask_council" in first and "Phone calls use" in first
