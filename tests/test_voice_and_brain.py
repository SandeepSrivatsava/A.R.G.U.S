import threading

import numpy as np
import pytest

from argus.approvals import DENY, ONCE, TASK, ApprovalRequest, DenyAll, ScriptedApprover
from argus.audit import Audit
from argus.brain import Brain, Sessions
from argus.daemon import Argus
from argus.hearing import ScriptedTranscriber
from argus.hermes import Hermes, HermesError, read_env_file
from argus.listener import Listener
from argus.voice import RecordingVoice, SentenceBuffer, Speaker, speakable
from argus.wake import NameMatcher

from fakes import FakeHermes, wait_for


# ---- what gets read aloud ------------------------------------------------------------------------------
def test_speakable_strips_markdown_links_and_emoji():
    text = "**Done!** See [the docs](https://x.y/z) or https://example.com 🎉\n- item one\n```code```"
    assert speakable(text) == "Done! See the docs or a link item one"


def test_sentence_buffer_streams_whole_sentences():
    buf = SentenceBuffer()
    out = []
    for piece in ["It's 21 degrees", " in Lond", "on. Mr. Smith called", " at 3.30 pm! Anything", " else?"]:
        out += buf.feed(piece)
    out += buf.flush()
    assert out == ["It's 21 degrees in London.", "Mr. Smith called at 3.30 pm!", "Anything else?"]


def test_sentence_buffer_breaks_very_long_sentences():
    buf = SentenceBuffer(max_chars=80)
    out = buf.feed("word, " * 30)
    assert out and all(len(s) <= 80 for s in out)


def test_speaker_speaks_in_order_and_stops():
    voice = RecordingVoice(seconds_per_sentence=0.05)
    speaker = Speaker(voice)
    for s in ["One.", "Two.", "Three."]:
        speaker.say(s)
    assert speaker.speaking
    assert speaker.wait_idle(2)
    assert voice.spoken == ["One.", "Two.", "Three."]
    slow = RecordingVoice(seconds_per_sentence=2)
    speaker = Speaker(slow)
    for s in ["A.", "B.", "C."]:
        speaker.say(s)
    assert wait_for(lambda: slow.spoken == ["A."])
    speaker.stop()
    assert speaker.wait_idle(1)
    assert slow.spoken == ["A."]          # the queue was dropped
    assert "A." in speaker.recently_spoken()


# ---- Hermes client -------------------------------------------------------------------------------------
@pytest.fixture
def hermes_server():
    server = FakeHermes()
    yield server
    server.shutdown()


def test_hermes_client_refuses_remote_hosts_and_missing_key():
    with pytest.raises(ValueError):
        Hermes("http://192.168.1.5:8642", "k")
    with pytest.raises(ValueError):
        Hermes("http://127.0.0.1:8642", "")


def test_read_env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nAPI_SERVER_KEY='abc'\nexport API_SERVER_PORT=9000\nBAD LINE\n")
    assert read_env_file(p) == {"API_SERVER_KEY": "abc", "API_SERVER_PORT": "9000"}


def test_hermes_runs_and_events(hermes_server):
    hermes_server.script = [[{"event": "message.delta", "delta": "Hi"},
                             {"event": "run.completed", "output": "Hi"},
                             {"event": "message.delta", "delta": "never seen"}]]
    h = Hermes(hermes_server.url, "test-key")
    run_id = h.start_run("hello", session_id="s1", instructions="be brief")
    assert hermes_server.runs[0]["session_id"] == "s1" and hermes_server.runs[0]["instructions"] == "be brief"
    names = [e["event"] for e in h.events(run_id)]
    assert names == ["message.delta", "run.completed"]   # stops at the terminal event
    with pytest.raises(ValueError):
        h.approve(run_id, "always")                       # never permanent
    with pytest.raises(HermesError):
        Hermes(hermes_server.url, "wrong").start_run("x")


# ---- approvals -----------------------------------------------------------------------------------------
def test_approval_answers_are_clamped():
    req = ApprovalRequest("r", choices=["once", "deny"])
    assert ScriptedApprover(["always"]).ask(req) == DENY
    assert ScriptedApprover([TASK]).ask(req) == ONCE           # task scope not offered
    assert ScriptedApprover([TASK]).ask(ApprovalRequest("r", choices=["once", "session", "deny"])) == TASK
    assert DenyAll().ask(req) == DENY


def test_approval_request_from_event():
    req = ApprovalRequest.from_event("r1", {"command": "computer_use: click 'Send' in Mail", "pattern_key": "cua:click:background",
                                           "choices": ["once", "session", "always", "deny"]})
    assert req.summary() == "use your Mac: click 'Send' in Mail"
    assert "Mac" in req.spoken() and req.task_scope_offered


# ---- brain -----------------------------------------------------------------------------------------------
def make_brain(server, tmp_path, answers=()):
    voice = RecordingVoice()
    speaker = Speaker(voice)
    approver = ScriptedApprover(list(answers))
    finished = threading.Event()
    brain = Brain(Hermes(server.url, "test-key"), speaker, approver, Audit(tmp_path / "audit.jsonl"),
                  on_reply_finished=finished.set)
    return brain, voice, approver, finished


def test_brain_speaks_streamed_reply(hermes_server, tmp_path):
    hermes_server.script = [[{"event": "message.delta", "delta": "The top story is about a storm. "},
                             {"event": "message.delta", "delta": "Roads are closed"},
                             {"event": "run.completed", "output": "The top story is about a storm. Roads are closed"}]]
    brain, voice, _, finished = make_brain(hermes_server, tmp_path)
    brain.ask("Argus, top news?")
    assert finished.wait(5)
    assert voice.spoken == ["The top story is about a storm.", "Roads are closed"]
    assert hermes_server.runs[0]["session_id"].startswith("argus-voice-")
    assert "Never say your own name" in hermes_server.runs[0]["instructions"]


def test_brain_speaks_final_output_when_not_streamed(hermes_server, tmp_path):
    hermes_server.script = [[{"event": "run.completed", "output": "**It's sunny.** 🌞"}]]
    brain, voice, _, finished = make_brain(hermes_server, tmp_path)
    brain.ask("weather")
    assert finished.wait(5)
    assert voice.spoken == ["It's sunny."]


def test_brain_approval_flow(hermes_server, tmp_path):
    hermes_server.script = [[
        {"event": "message.delta", "delta": "Opening Mail. "},
        {"event": "approval.request", "command": "computer_use: click 'Send'", "pattern_key": "cua:click:background",
         "choices": ["once", "session", "always", "deny"]},
        {"event": "_wait_for_approval"},
        {"event": "run.completed", "output": "Sent."}]]
    brain, voice, approver, finished = make_brain(hermes_server, tmp_path, answers=[TASK])
    brain.ask("send it")
    assert finished.wait(5)
    assert hermes_server.approvals == [{"choice": "session", "run_id": "run_0"}]
    assert approver.seen[0].pattern_key == "cua:click:background"
    assert "I need your OK to use your Mac. Please check the screen." in voice.spoken
    log = (tmp_path / "audit.jsonl").read_text()
    assert '"event": "approval"' in log and '"choice": "session"' in log


def test_brain_denied_approval_is_spoken(hermes_server, tmp_path):
    hermes_server.script = [[{"event": "approval.request", "command": "rm -rf ~/Documents", "choices": ["once", "deny"]},
                             {"event": "_wait_for_approval"},
                             {"event": "run.completed", "output": "I didn't delete anything."}]]
    brain, voice, _, finished = make_brain(hermes_server, tmp_path, answers=["deny"])
    brain.ask("clean up")
    assert finished.wait(5)
    assert hermes_server.approvals[0]["choice"] == "deny"
    assert voice.spoken[-1] == "I didn't delete anything."


def test_brain_cancel_stops_the_run(hermes_server, tmp_path):
    hermes_server.script = [[{"event": "message.delta", "delta": "Working. "}, {"event": "_wait_for_stop"}]]
    brain, voice, _, finished = make_brain(hermes_server, tmp_path)
    brain.ask("long task")
    assert wait_for(lambda: brain._run_id is not None)
    brain.cancel()
    brain.wait(5)
    assert hermes_server.stopped == ["run_0"]
    assert not finished.is_set()


def test_brain_reports_unreachable_hermes(tmp_path):
    voice = RecordingVoice()
    brain = Brain(Hermes("http://127.0.0.1:9", "k"), Speaker(voice), DenyAll(), Audit(tmp_path / "a.jsonl"))
    brain.ask("hello")
    brain.wait(5)
    assert wait_for(lambda: voice.spoken == ["I can't reach my brain right now. Check that Hermes is running."])


def test_sessions_rotate_after_quiet_spell():
    t = [1_700_000_000.0]
    s = Sessions(idle_minutes=30, clock=lambda: t[0])
    first = s.current()
    t[0] += 60
    assert s.current() == first
    t[0] += 31 * 60
    assert s.current() != first
    second = s.current()
    s.rotate()
    t[0] += 1
    assert s.current() != second


# ---- the listening loop (scripted transcripts, fake microphone) -----------------------------------------
class FakeSource:
    def __init__(self):
        self.is_open = False

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def read(self, timeout=0.25):
        return None


class FakeSegmenter:
    speech_active = False

    def feed(self, chunk):
        return []

    def flush(self):
        return []

    def reset(self):
        pass

    def recent_audio(self):
        return np.zeros(0, dtype=np.float32)


def make_argus(server, tmp_path, transcripts, answers=()):
    voice = RecordingVoice()
    speaker = Speaker(voice)
    audit = Audit(tmp_path / "audit.jsonl")
    brain = Brain(Hermes(server.url, "test-key"), speaker, ScriptedApprover(list(answers)), audit)
    argus = Argus(source=FakeSource(), segmenter=FakeSegmenter(), transcriber=ScriptedTranscriber(transcripts),
                  listener=Listener(NameMatcher("argus")), speaker=speaker, brain=brain, audit=audit)
    argus.set_mic(True)
    return argus, voice


SPEECH = np.ones(16000, dtype=np.float32) * 0.1


def test_only_addressed_speech_reaches_hermes(hermes_server, tmp_path):
    hermes_server.script = [[{"event": "run.completed", "output": "It's ten past four."}]]
    argus, voice = make_argus(hermes_server, tmp_path, ["We should paint the kitchen.", "Argus, what time is it?"])
    argus.handle_utterance(SPEECH)
    assert hermes_server.runs == [] and argus.ignored == 1
    argus.handle_utterance(SPEECH)
    argus.brain.wait(5)
    assert [r["input"] for r in hermes_server.runs] == ["Argus, what time is it?"]
    assert wait_for(lambda: voice.spoken == ["It's ten past four."])
    log = (tmp_path / "audit.jsonl").read_text()
    assert "paint the kitchen" not in log            # unaddressed speech is never stored


def test_voice_commands_stay_local(hermes_server, tmp_path):
    argus, voice = make_argus(hermes_server, tmp_path, ["Argus, go to sleep", "Argus what's the time",
                                                        "Argus wake up", "Argus turn off the microphone"])
    for _ in range(4):
        argus.handle_utterance(SPEECH)
        argus.speaker.wait_idle(2)
    assert hermes_server.runs == []
    assert voice.spoken[0].startswith("Going quiet") and "I'm back." in voice.spoken
    assert argus.status()["mode"] == "microphone off" and not argus.source.is_open


def test_locking_the_mac_closes_the_microphone(hermes_server, tmp_path):
    argus, _ = make_argus(hermes_server, tmp_path, [])
    assert argus.source.is_open
    argus.set_locked(True)
    assert not argus.source.is_open and argus.status()["mode"] == "paused (Mac locked)"
    argus.set_locked(False)
    assert argus.source.is_open


def test_short_utterances_and_mic_off_are_ignored(hermes_server, tmp_path):
    argus, _ = make_argus(hermes_server, tmp_path, ["Argus hello"])
    argus.handle_utterance(np.ones(1000, dtype=np.float32))
    argus.set_mic(False)
    argus.handle_utterance(SPEECH)
    assert hermes_server.runs == []


class FakePrint:
    threshold, enrolled = 0.5, True

    def __init__(self, score):
        self._score = score

    def score(self, samples):
        return self._score


def test_voice_lock_ignores_other_voices_without_changing_state(hermes_server, tmp_path):
    argus, _ = make_argus(hermes_server, tmp_path, ["Argus, go to sleep", "Argus what's new"])
    argus.voiceprint, argus.voice_lock = FakePrint(0.1), True
    argus.handle_utterance(SPEECH)                       # a stranger can't put Argus to sleep
    assert not argus.listener.sleeping and hermes_server.runs == []
    argus.voiceprint = FakePrint(0.9)
    hermes_server.script = [[{"event": "run.completed", "output": "Not much."}]]
    argus.handle_utterance(SPEECH)
    argus.brain.wait(5)
    assert len(hermes_server.runs) == 1
    assert "voice_not_recognised" in (tmp_path / "audit.jsonl").read_text()


def test_own_voice_is_not_taken_as_a_follow_up(hermes_server, tmp_path):
    argus, _ = make_argus(hermes_server, tmp_path, ["the top story today is about a storm"])
    argus.speaker._recent.append((argus.speaker.clock(), "The top story today is about a storm in the Bay Area."))
    argus.listener.reply_finished()
    argus.handle_utterance(SPEECH)
    assert hermes_server.runs == [] and argus.listener.in_followup()   # window still open for you


def test_a_new_request_cancels_the_old_one_cleanly(hermes_server, tmp_path):
    hermes_server.script = [[{"event": "message.delta", "delta": "Working on the first. "}, {"event": "_wait_for_stop"}],
                            [{"event": "run.completed", "output": "Second answer."}]]
    brain, voice, _, finished = make_brain(hermes_server, tmp_path)
    brain.ask("first")
    assert wait_for(lambda: brain._run_id == "run_0")
    brain.ask("second")
    assert finished.wait(5)
    assert hermes_server.stopped == ["run_0"] and voice.spoken[-1] == "Second answer."


def test_approval_dialog_script_defaults_to_deny():
    from pathlib import Path
    import argus.approvals as approvals
    script = Path(approvals.__file__).with_name("approve.applescript").read_text()
    assert 'default button "Deny"' in script and "giving up after" in script and '"Allow for this task"' in script
