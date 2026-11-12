"""Talking like a person: you can talk over Argus and it stops; a natural American voice that makes the next
sentence while speaking this one; Google reviews and search, and other AI models, next to Claude."""
import json
import os
import time

import numpy as np
import pytest

from argus import config, council, search
from argus.agent import Events
from argus.prompts import persona
from argus.toolbox import Context
from argus.voice import KOKORO_VOICES, KokoroVoice, Speaker, make_voice

from fakes import SPEECH, FakeVoicePrint, Script, make_agent, make_argus, make_settings, say, wait_for

MODELS = os.environ.get("ARGUS_TEST_MODELS", "")
KOKORO = os.path.join(MODELS, "kokoro-int8-en-v0_19")


# ---- talking over Argus ---------------------------------------------------------------------------------
def talking(argus, seconds=1.0):
    argus.segmenter.speech_active = True
    argus.segmenter.recent_audio = lambda: SPEECH
    argus.speaker._idle.clear()                       # Argus is speaking
    argus._speech_since = time.monotonic() - seconds


def test_your_voice_stops_argus_and_what_you_say_needs_no_name(tmp_path):
    argus, voice, script = make_argus(tmp_path, ["actually, somewhere quieter"], voiceprint=FakeVoicePrint(0.2, 0.8, 0.8),
                                      voice_lock=True, script=Script(say("Sure, a quiet place then.")))
    stopped = []
    real_cancel = argus.brain.cancel
    argus.brain.cancel = lambda: (stopped.append(1), real_cancel())
    talking(argus)
    argus._maybe_barge_in()                           # 0.2: that's Argus's own voice from the speakers
    assert stopped == []
    for _ in range(2):                                # 0.8 twice in a row: that's you
        argus._last_barge_check = 0.0
        argus._maybe_barge_in()
    assert stopped == [1]
    argus.speaker._idle.set()
    argus.segmenter.speech_active = False
    argus.handle_utterance(SPEECH)                    # no "Argus" needed
    assert wait_for(lambda: voice.spoken[-1:] == ["Sure, a quiet place then."])
    sent = json.dumps(script.bodies[-1]["messages"][-1])
    assert "(I interrupted you while you were answering.) actually, somewhere quieter" in sent
    log = (tmp_path / "audit.jsonl").read_text()
    assert '"barge_in", "why": "your voice", "score": 0.8' in log


def test_a_cough_or_a_short_sound_doesnt_stop_it(tmp_path):
    argus, voice, _ = make_argus(tmp_path, [], voiceprint=FakeVoicePrint(), voice_lock=True)
    stopped = []
    argus.brain.cancel = lambda: stopped.append(1)
    talking(argus, seconds=0.1)
    argus._maybe_barge_in()
    argus.segmenter.speech_active = False
    argus._maybe_barge_in()                           # the sound ended: the clock starts again next time
    assert stopped == [] and argus._speech_since is None
    argus.speaker._idle.set()


def test_without_an_enrolled_voice_new_words_stop_it_but_its_own_echo_doesnt(tmp_path):
    argus, voice, _ = make_argus(tmp_path, ["the museum is open until five", "wait which one is cheaper",
                                            "which one is cheaper I asked"])
    argus.speaker._recent.append((argus.speaker.clock(), "The museum is open until five today."))
    stopped = []
    argus.brain.cancel = lambda: stopped.append(1)
    talking(argus)
    argus._maybe_barge_in()                           # its own sentence, heard back: keeps talking
    assert stopped == []
    for _ in range(2):
        argus._last_barge_check = 0.0
        argus._maybe_barge_in()
    assert stopped == [1] and '"why": "new words"' in (tmp_path / "audit.jsonl").read_text()
    argus.speaker._idle.set()


def test_name_only_mode_keeps_the_old_way(tmp_path):
    argus, voice, _ = make_argus(tmp_path, ["wait which one is cheaper", "Argus stop"], barge_in="name")
    stopped = []
    argus.brain.cancel = lambda: stopped.append(1)
    talking(argus, seconds=0.0)
    argus._maybe_barge_in()
    assert stopped == []
    argus._last_barge_check = 0.0
    argus._maybe_barge_in()
    assert stopped == [1] and argus._barged_recently() is False     # "stop" just stops; nothing to answer
    argus.speaker._idle.set()


def test_barge_in_setting_is_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[listening]\nbarge_in = "always"\n')
    with pytest.raises(ValueError, match="listening.barge_in"):
        config.load()


# ---- the voice ------------------------------------------------------------------------------------------
@pytest.mark.skipif(not os.path.isdir(KOKORO), reason="Kokoro model not downloaded")
def test_kokoro_speaks_american_and_makes_the_next_sentence_early(tmp_path):
    played = []
    voice = KokoroVoice(KOKORO, voice="am_michael", speed=1.2, player=["true"], tmp_dir=str(tmp_path))
    samples, rate = voice.synthesize("Hello there.")
    assert rate == 24000 and samples.size > rate * 0.3
    made = []
    real = voice.synthesize
    voice.synthesize = lambda text: (made.append(text), real(text))[1]
    voice._run = lambda args: played.append(args)
    speaker = Speaker(voice)
    speaker.say("Oh nice, New York!")
    speaker.say("Is it your first time there?")
    assert speaker.wait_idle(60)
    assert made == ["Oh nice, New York!", "Is it your first time there?"] and len(played) == 2
    with pytest.raises(ValueError, match="unknown Kokoro voice"):
        KokoroVoice(KOKORO, voice="hal9000")


def test_the_voice_falls_back_when_kokoro_is_missing(tmp_path, monkeypatch):
    import argus.voice as voice_mod
    monkeypatch.setattr(voice_mod, "PiperVoice", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("x")))
    assert type(make_voice("kokoro", kokoro_dir=str(tmp_path / "none"))).__name__ == "SayVoice"
    assert list(KOKORO_VOICES)[:7] == ["af", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam", "am_michael"]


def test_prepared_sentences_are_dropped_when_stopped(tmp_path):
    from argus.voice import _NeuralVoice

    class Slow(_NeuralVoice):
        def __init__(self):
            super().__init__()
            self._init_playback(["true"], str(tmp_path))
            self.made = []

        def synthesize(self, text):
            time.sleep(0.05)
            self.made.append(text)
            return np.zeros(10, dtype=np.float32), 16000
    v = Slow()
    v.prepare("a")
    v.prepare("a")                                    # once is enough
    time.sleep(0.2)
    v.stop()
    assert v._made == {} and v.made == ["a"]
    v.reset()
    v._run = lambda args: None
    v.speak("b")
    assert v.made == ["a", "b"]

    class Broken(Slow):
        def synthesize(self, text):
            raise RuntimeError("no")
    b = Broken()
    b.prepare("x")
    time.sleep(0.05)
    with pytest.raises(RuntimeError):
        b._sound_for("x")


def test_voice_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    s = config.load()
    assert (s.voice_engine, s.kokoro_voice, s.speed) == ("kokoro", "am_michael", 1.2)
    (tmp_path / "config.toml").write_text('[voice]\nkokoro_voice = "darth"\n')
    with pytest.raises(ValueError, match="voice.kokoro_voice"):
        config.load()


def test_argusctl_voices(tmp_path, monkeypatch, capsys):
    from argus.__main__ import cmd_voices
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    heard = []

    class Fake:
        def __init__(self, name, speed):
            self.name, self.speed = name, speed

        def speak(self, text):
            heard.append((self.name, self.speed, text))

    class Args:
        name, speed = None, None
    assert cmd_voices(Args, make=Fake) == 0
    assert [h[0] for h in heard] == ["af", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam", "am_michael"]
    assert heard[0][1] == 1.2 and "ramen" in heard[0][2]
    out = capsys.readouterr().out
    assert "am_michael  (yours now)" in out and "kokoro_voice" in out
    Args.name, Args.speed = "bm_lewis", 1.0
    assert cmd_voices(Args, make=Fake) == 0 and heard[-1][:2] == ("bm_lewis", 1.0)
    Args.name = "nobody"
    assert cmd_voices(Args, make=Fake) == 1

    def broken(name, speed):
        raise FileNotFoundError("no model")
    Args.name = "am_adam"
    assert cmd_voices(Args, make=broken) == 1 and "./setup.sh" in capsys.readouterr().out


# ---- how it talks ---------------------------------------------------------------------------------------
def test_the_instructions_ask_for_a_back_and_forth():
    text = persona(user="Sam", city="Portland, OR", timezone="America/Los_Angeles", workspace="/w")
    for expected in ("This is a back-and-forth", "don't list everything", "at most two or three options",
                     "One question at a time", "talks over you", "place_search for Google's ratings",
                     "ask_council", "who they go with"):
        assert expected in text, expected
    assert "British butler" not in text


# ---- Google reviews and search --------------------------------------------------------------------------
PLACES = {"places": [
    {"displayName": {"text": "Udupi Palace"}, "formattedAddress": "1 Main St, Fremont, CA", "rating": 4.4,
     "userRatingCount": 1832, "priceLevel": "PRICE_LEVEL_INEXPENSIVE", "currentOpeningHours": {"openNow": True},
     "primaryTypeDisplayName": {"text": "Indian Restaurant"},
     "reviewSummary": {"text": {"text": "People love the dosas; service can be slow at peak times."}},
     "reviews": [{"rating": 5, "text": {"text": "Best masala dosa   in the Bay."}, "relativePublishTimeDescription": "a week ago"},
                 {"rating": 2, "text": {"text": "Waited 40 minutes."}, "relativePublishTimeDescription": "a month ago"},
                 {"rating": 4, "originalText": {"text": "Third review"}}, "junk"]},
    {"displayName": {"text": "Quiet Cafe"}, "rating": 4.0, "userRatingCount": 12,
     "editorialSummary": {"text": "A calm spot."}, "currentOpeningHours": {"openNow": False}}, "junk"]}


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.places = (200, PLACES)
        self.gemini = (200, {"candidates": [{"content": {"parts": [{"text": "Udupi Palace is the most "}, {"text": "praised."}]},
                                             "groundingMetadata": {"webSearchQueries": ["best dosa fremont"],
                                                                   "groundingChunks": [{"web": {"uri": "https://example.com/a", "title": "Eater"}},
                                                                                       {"web": {"uri": "https://example.com/a", "title": "Eater"}},
                                                                                       {"other": 1}]}}]})
        self.openai = (200, {"choices": [{"message": {"content": "Go with  Udupi."}}]})

    def __call__(self, method, url, headers, body=None, timeout=0):
        self.calls.append((url, headers, body))
        if "places.googleapis.com" in url:
            return self.places
        if "generativelanguage" in url:
            return self.gemini
        return self.openai


def tools_for(tmp_path, fake, secrets, where=lambda: None, **settings):
    tools = {}
    made = search.attach(make_settings(tmp_path, **settings), secrets, tools, request=fake, where=where)
    return made, tools, Context(channel="voice", workspace=tmp_path, store=None)


def test_google_reviews(tmp_path):
    fake = FakeGoogle()
    made, tools, ctx = tools_for(tmp_path, fake, {"GOOGLE_MAPS_API_KEY": "maps-key-1"}, where=lambda: (37.55, -121.98))
    assert "google_search" not in tools
    out = tools["place_search"].run({"query": "dosa near Fremont", "open_now": True, "min_rating": 4.2, "limit": 3}, ctx)
    text = out.content
    assert text.startswith(search.UNTRUSTED)
    assert "Udupi Palace: 4.4★ from 1,832 Google reviews, $, Indian Restaurant, open now" in text
    assert "in short: People love the dosas" in text and "review (5★, a week ago): Best masala dosa in the Bay." in text
    assert "Waited 40 minutes" in text and "Third review" not in text            # two reviews by default
    assert "Quiet Cafe: 4.0★ from 12 Google reviews, closed now" in text and "A calm spot." in text
    url, headers, body = fake.calls[0]
    assert headers["X-Goog-Api-Key"] == "maps-key-1" and "places.reviews" in headers["X-Goog-FieldMask"]
    assert body == {"textQuery": "dosa near Fremont", "pageSize": 3, "openNow": True, "minRating": 4.0,
                    "locationBias": {"circle": {"center": {"latitude": 37.55, "longitude": -121.98}, "radius": 15000.0}}}
    assert tools["place_search"].run({"query": " "}, ctx).is_error
    fake.places = (200, {})
    assert "found nothing" in tools["place_search"].run({"query": "x"}, ctx).content
    cases = [((403, {"error": {"message": "Places API (New) has not been used in project 1 before or it is disabled."}}),
              "isn't switched on"),
             ((400, {"error": {"message": "API key not valid. Please pass a valid API key."}}), "refused"),
             ((429, {"error": {"message": "quota"}}), "used up"),
             ((500, {"error": {"message": "boom maps-key-1"}}), "HTTP 500 (boom ...)")]
    for reply, said in cases:
        fake.places = reply
        out = tools["place_search"].run({"query": "x"}, ctx)
        assert out.is_error and said in out.content and "maps-key-1" not in out.content


def test_google_search_through_gemini(tmp_path):
    fake = FakeGoogle()
    made, tools, ctx = tools_for(tmp_path, fake, {"GEMINI_API_KEY": "gem-key-1"})
    assert "place_search" not in tools
    out = tools["google_search"].run({"question": "best dosa in Fremont?"}, ctx).content
    assert "Google (answered by Gemini gemini-2.5-flash from Google Search):" in out
    assert "Udupi Palace is the most praised." in out and "Searched for: best dosa fremont" in out
    assert out.count("https://example.com/a") == 1
    url, headers, body = fake.calls[0]
    assert url.endswith("/models/gemini-2.5-flash:generateContent") and headers == {"x-goog-api-key": "gem-key-1"}
    assert body["tools"] == [{"google_search": {}}] and "best dosa in Fremont?" in body["contents"][0]["parts"][0]["text"]
    fake.gemini = (200, {"promptFeedback": {"blockReason": "SAFETY"}})
    assert "no answer (SAFETY)" in tools["google_search"].run({"question": "x"}, ctx).content
    fake.gemini = (200, {"candidates": [{"content": {"parts": []}}]})
    assert "empty answer" in tools["google_search"].run({"question": "x"}, ctx).content
    assert tools["google_search"].run({"question": ""}, ctx).is_error
    fake.gemini = (429, {"error": {"message": "Resource exhausted"}})
    assert "used up" in tools["google_search"].run({"question": "x"}, ctx).content

    def down(*a, **k):
        from argus.maps import MapsError
        raise MapsError("couldn't reach generativelanguage.googleapis.com (timed out)")
    made["gemini"].request = down
    assert "couldn't reach" in tools["google_search"].run({"question": "x"}, ctx).content
    _, tools, _ = tools_for(tmp_path, fake, {})
    assert tools == {}


def test_the_council(tmp_path):
    fake = FakeGoogle()
    settings = make_settings(tmp_path, council=["Groq Llama | https://api.example.com/v1/chat/completions | llama-x | MY_LLAMA_KEY",
                                                 "Nobody | https://api.example.org/v1/chat/completions | m | MISSING_KEY"])
    tools = {}
    c = council.attach(settings, {"GEMINI_API_KEY": "gem", "MY_LLAMA_KEY": "groq-secret"}, tools, request=fake)
    assert [m.name for m in c.team] == ["Gemini (gemini-2.5-flash)", "Groq Llama"]
    ctx = Context(channel="voice", workspace=tmp_path, store=None)
    out = tools["ask_council"].run({"question": "Should I visit NYC in December or April?"}, ctx).content
    assert "- Gemini (gemini-2.5-flash): Udupi Palace is the most praised." in out and "- Groq Llama: Go with Udupi." in out
    openai_call = [c for c in fake.calls if "api.example.com" in c[0]][0]
    assert openai_call[1] == {"Authorization": "Bearer groq-secret"} and openai_call[2]["model"] == "llama-x"
    fake.openai = (401, {"error": {"message": "bad key groq-secret"}})
    out = tools["ask_council"].run({"question": "q"}, ctx).content
    assert "- Groq Llama: no answer (HTTP 401 (bad key ...))" in out
    fake.gemini = (500, {})
    fake.openai = (200, {"choices": [{"message": {"content": "  "}}]})
    out = tools["ask_council"].run({"question": "q"}, ctx)
    assert out.is_error and "Answer on your own" in out.content
    assert tools["ask_council"].run({"question": ""}, ctx).is_error
    assert council.attach(make_settings(tmp_path), {}, {}) is None


def test_council_settings_are_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[council]\nextra = ["just a name"]\n')
    with pytest.raises(ValueError, match="council.extra"):
        config.load()
    (tmp_path / "config.toml").write_text('[council]\nextra = ["X | http://insecure | m | KEY"]\n')
    with pytest.raises(ValueError, match="council.extra"):
        config.load()
    (tmp_path / "config.toml").write_text('[council]\nextra = ["X | https://a.example/v1 | m | X_KEY"]\n'
                                          '[brain]\ngemini_model = "gemini-2.5-flash-lite"\n')
    s = config.load()
    assert s.council == ["X | https://a.example/v1 | m | X_KEY"] and s.gemini_model == "gemini-2.5-flash-lite"


def test_a_restaurant_question_looks_in_several_places_at_once(tmp_path):
    fake = FakeGoogle()
    script = Script(([{"type": "tool_use", "id": "a", "name": "place_search", "input": {"query": "dosa Fremont"}},
                      {"type": "tool_use", "id": "b", "name": "google_search", "input": {"question": "best dosa Fremont"}}],
                     "tool_use"),
                    say("Udupi Palace: 4.4 stars, and Google agrees it's the dosa spot. Want the closest one instead?"))
    agent, _ = make_agent(tmp_path, script)
    search.attach(make_settings(tmp_path), {"GOOGLE_MAPS_API_KEY": "k", "GEMINI_API_KEY": "g"}, agent.tools, request=fake)
    turn = agent.run(session_id="s", channel="voice", content="good restaurants for dosa near me with reviews",
                     events=Events())
    assert turn.text.endswith("Want the closest one instead?") and turn.model == "claude-sonnet-5-5"
    assert '"tools_together", "count": 2' in (tmp_path / "audit.jsonl").read_text()


def test_the_morning_briefing_is_replaced_once(tmp_path):
    from argus.scheduler import BRIEFING_JOB, RETIRED_BRIEFINGS, sync_default_jobs
    from argus.store import Store
    store = Store(tmp_path / "a.db")
    store.add_task(RETIRED_BRIEFINGS[0], "task", cron="30 7 * * 1-5", next_run=time.time() + 60, origin="settings")
    store.set("default_job:task", "30 7 * * 1-5")
    sync_default_jobs(store, "", "30 7 * * 1-5", "America/Los_Angeles")
    assert store.task_named(RETIRED_BRIEFINGS[0]) is None and store.task_named(BRIEFING_JOB) is not None
    sync_default_jobs(store, "", "30 7 * * 1-5", "America/Los_Angeles")
    assert "calendar" in BRIEFING_JOB and "mail_search" in BRIEFING_JOB
