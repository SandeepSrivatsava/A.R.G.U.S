"""Keeping out of calls at the Mac (micwatch.py and the listening loop), "I'm on a call" for a call on the
iPhone itself, and seeing where the time goes on a spoken answer (`argusctl timing`)."""
import io
import json
import time

import numpy as np
import pytest

from argus.micwatch import MicWatch, friendly, on_a_call
from argus.wake import Command, command_of
from fakes import SPEECH, make_argus, wait_for


# ---- which apps mean a call -------------------------------------------------------------------------------------
def test_calls_are_told_apart_from_apples_always_on_listeners():
    seen = ["com.apple.corespeechd", "com.apple.assistantd", "com.apple.FaceTime", "us.zoom.xos", "com.argus.voice",
            "org.python.python", "com.apple.accessibility.heard", "", "com.apple.FaceTime"]
    assert on_a_call(seen) == ["com.apple.FaceTime", "us.zoom.xos"]
    assert on_a_call(seen, ignore=["Zoom"]) == ["com.apple.FaceTime"]                 # by name
    assert on_a_call(seen, ignore=["com.apple.FaceTime", "us.zoom"]) == []             # by ID or its start
    assert friendly("com.apple.mobilephone") == "Phone" and friendly("com.example.Recorder") == "Recorder"
    assert friendly("pid 912") == "pid 912"


class FakeProcess:
    def __init__(self, lines):
        self.stdout = io.StringIO("".join(json.dumps(line) + "\n" if not isinstance(line, str) else line
                                          for line in lines))
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True


def test_the_watcher_reports_calls_and_waits_before_saying_the_mic_is_free(tmp_path):
    exe = tmp_path / "argus-mic"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    changes, started = [], []

    def popen(args, **kw):
        started.append(args)
        return FakeProcess([{"apps": ["com.apple.corespeechd"]}, {"apps": ["com.apple.FaceTime"]}, "not json\n",
                            {"apps": ["com.apple.FaceTime", "com.apple.corespeechd"]}, {"apps": []}])
    watch = MicWatch(exe, changes.append, popen=popen, own_pid=4242, quiet_after=0.05)
    assert watch.start()
    assert started == [[str(exe), "--watch", "--except", "4242"]]
    assert wait_for(lambda: changes == [["com.apple.FaceTime"], []])
    watch.stop()
    assert not MicWatch(tmp_path / "missing", changes.append).start()

    def broken(args, **kw):
        raise OSError("not allowed")
    assert not MicWatch(exe, changes.append, popen=broken).start()


def test_a_short_gap_in_a_call_doesnt_reopen_the_microphone(tmp_path):
    changes = []
    watch = MicWatch(tmp_path / "x", changes.append, quiet_after=0.2)
    watch.seen(["us.zoom.xos"])
    watch.seen([])                       # Zoom let go for a moment...
    watch.seen(["us.zoom.xos"])          # ...and took it again
    time.sleep(0.3)
    assert changes == [["us.zoom.xos"]]
    watch.seen([])
    watch.stop()                         # stopped before the quiet period ended: nothing more reported
    time.sleep(0.3)
    assert changes == [["us.zoom.xos"]]


# ---- the listening loop during a call -------------------------------------------------------------------------
def test_a_call_at_the_mac_closes_the_microphone_and_holds_reminders(tmp_path):
    argus, voice, script = make_argus(tmp_path, ["Argus, what's the weather?"])
    argus.set_on_call(["com.apple.FaceTime"])
    assert not argus.source.is_open and argus.status()["mode"] == "paused (on a call: FaceTime)"
    assert not argus.present()
    argus.announce("Reminder: call home")
    argus.handle_utterance(SPEECH)                         # heard nothing while on the call
    assert script.bodies == [] and voice.spoken == []
    argus.set_on_call([])
    assert argus.source.is_open
    assert wait_for(lambda: voice.spoken[-2:] == ["While you were away:", "Reminder: call home"])
    log = (tmp_path / "audit.jsonl").read_text()
    assert '"on_call", "apps": ["FaceTime"]' in log.replace('"event": ', '').replace("{", "") or '"FaceTime"' in log


def test_argus_own_phone_calls_dont_count(tmp_path):
    class Calls:
        active = True
    argus, _, _ = make_argus(tmp_path, [], calls=Calls())
    argus.set_on_call(["com.apple.FaceTime"])                # FaceTime is carrying Argus's own call
    assert not argus.closed and "on a call" not in argus.status()["mode"]


def test_a_call_starting_mid_answer_stops_argus(tmp_path):
    argus, voice, script = make_argus(tmp_path, [])
    cancelled = []
    argus.brain.cancel = lambda: cancelled.append(1)
    argus.set_on_call(["us.zoom.xos"])
    argus.set_on_call(["us.zoom.xos", "com.apple.FaceTime"])    # already paused: not cancelled twice
    assert cancelled == [1]
    argus.set_locked(True)
    argus.set_on_call([])
    assert not argus.source.is_open and argus.status()["mode"] == "paused (Mac locked)"


def test_im_on_a_call_and_im_back(tmp_path):
    for said in ("I'm on a call", "im on the phone", "I'm taking a call", "I've got a call", "I'm on my phone call"):
        assert command_of(said) is Command.SLEEP, said
    for said in ("I'm off the phone", "I'm done with the call", "the call's over", "call is over", "I'm back"):
        assert command_of(said) is Command.WAKE, said
    argus, voice, script = make_argus(tmp_path, ["Argus, I'm on a call", "what time is it", "Argus I'm off the phone"])
    argus.handle_utterance(SPEECH)
    argus.speaker.wait_idle(2)
    assert argus.listener.sleeping
    argus.handle_utterance(SPEECH)
    assert script.bodies == []
    argus.handle_utterance(SPEECH)
    argus.speaker.wait_idle(2)
    assert not argus.listener.sleeping


# ---- where the time goes ------------------------------------------------------------------------------------------
def test_a_spoken_answer_records_its_timings(tmp_path):
    argus, voice, script = make_argus(tmp_path, ["Argus, hello there"])
    argus.handle_utterance(np.ones(32000, dtype=np.float32) * 0.1)
    assert wait_for(lambda: '"timing"' in (tmp_path / "audit.jsonl").read_text())
    entry = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()
             if '"timing"' in line][-1]
    assert entry["spoken_s"] == 2.0 and entry["speech_to_text_s"] >= 0 and entry["first_word_s"] >= 0
    assert len(entry["claude_first_s"]) == 1 and len(entry["claude_s"]) == 1
    turn = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines() if '"turn"' in line][-1]
    assert turn["timings"]["claude_s"] == entry["claude_s"]
    body = script.bodies[-1]
    assert body["model"] == "claude-haiku-5-5"            # an everyday question: the quick model, no thinking
    assert body["output_config"] == {"effort": "low"} and body["thinking"] == {"type": "disabled"}


def test_argusctl_timing(tmp_path, monkeypatch, capsys):
    from argus.__main__ import describe_timing, main
    line = describe_timing({"ts": "2026-11-11T18:05:12-0800", "speech_to_text_s": 0.4, "first_word_s": 2.3,
                            "claude_first_s": [0.9, 0.7], "claude_s": [1.1, 1.4], "tools": [["weather", 0.6]]})
    assert line == ("18:05:12  first word 3.3 s after you stopped talking: pause to notice you'd finished 0.6 s; "
                    "speech to text 0.4 s; Claude #1 0.9 s to start (1.1 s in all); Claude #2 0.7 s to start "
                    "(1.4 s in all); weather 0.6 s")
    assert describe_timing({"ts": "x" * 19, "first_word_s": 1.0, "claude_first_s": [0.5]}).endswith(
        "1.0 s after you stopped talking: Claude #1 0.5 s to start (0.5 s in all)")
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    assert main(["timing"]) == 0 and "No timed answers yet" in capsys.readouterr().out
    from argus.audit import Audit
    from argus import config
    Audit(config.load().audit_path).log("timing", speech_to_text_s=0.3, first_word_s=1.2, claude_first_s=[0.8],
                                        claude_s=[0.9])
    assert main(["timing", "5"]) == 0
    out = capsys.readouterr().out
    assert "first word 2.1 s after you stopped talking" in out and "include thinking and the network" in out


# ---- a conversation Claude won't accept any more --------------------------------------------------------------
@pytest.mark.invalid_requests_ok
def test_a_conversation_claude_refuses_is_replaced_and_the_question_asked_again(tmp_path):
    from fakes import Script, say
    script = Script(say("It's 18 degrees and sunny."))
    argus, voice, _ = make_argus(tmp_path, ["Argus, how's the weather?"], script=script)
    old = argus.brain.sessions.current()
    store = argus.brain.agent.store              # a saved message the API refuses (made with a different key setup)
    store.append(old, "user", [{"type": "text", "text": "earlier"}])
    store.append(old, "assistant", [{"type": "image_url", "url": "x"}])
    argus.handle_utterance(SPEECH)
    assert wait_for(lambda: voice.spoken[-1:] == ["It's 18 degrees and sunny."])
    assert argus.brain.sessions.current() != old
    assert "fresh_session_after_refusal" in (tmp_path / "audit.jsonl").read_text()


def test_spoken_errors_say_what_happened():
    from argus.brain import spoken_error
    assert "fresh conversation" in spoken_error("Claude API error 400: messages.3: bad")
    assert "overloaded" in spoken_error("Claude API error 529: Overloaded")
    assert "too many requests" in spoken_error("Claude API error 429: rate_limit_error")
    assert spoken_error("weird") == "Sorry, that didn't work. The details are in argusctl errors."
