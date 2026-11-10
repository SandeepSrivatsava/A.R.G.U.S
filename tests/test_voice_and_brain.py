import threading
from pathlib import Path

import numpy as np

from argus.approvals import DENY, ONCE, TASK, ApprovalRequest, DenyAll, ScriptedApprover
from argus.audit import Audit
from argus.brain import Brain, Sessions, spoken_error
from argus.voice import RecordingVoice, SentenceBuffer, Speaker, speakable

from fakes import SPEECH, Script, call, make_agent, make_argus, say, wait_for

FIXTURES = Path(__file__).parent / "fixtures"


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
    assert slow.spoken == ["A."]
    assert "A." in speaker.recently_spoken()


# ---- approvals -----------------------------------------------------------------------------------------
def test_approval_answers_are_clamped():
    req = ApprovalRequest("r", choices=["once", "deny"])
    assert ScriptedApprover(["always"]).ask(req) == DENY
    assert ScriptedApprover([TASK]).ask(req) == ONCE           # task scope not offered
    assert ScriptedApprover([TASK]).ask(ApprovalRequest("r", choices=["once", "session", "deny"])) == TASK
    assert DenyAll().ask(req) == DENY
    assert "Mac" in ApprovalRequest("r", pattern_key="computer:left_click").spoken()


def test_approval_dialog_script_defaults_to_deny():
    import argus.approvals as approvals
    script = Path(approvals.__file__).with_name("approve.applescript").read_text()
    assert 'default button "Deny"' in script and "giving up after" in script and '"Allow for this task"' in script


# ---- the voice brain -----------------------------------------------------------------------------------
def make_brain(tmp_path, script, answers=()):
    agent, _ = make_agent(tmp_path, script)
    voice = RecordingVoice()
    approver = ScriptedApprover(list(answers))
    finished = threading.Event()
    brain = Brain(agent, Speaker(voice), approver, Audit(tmp_path / "audit.jsonl"), Sessions(agent.store),
                  on_reply_finished=finished.set)
    return brain, voice, approver, finished


def test_brain_speaks_the_reply_sentence_by_sentence(tmp_path):
    brain, voice, _, finished = make_brain(tmp_path, Script(say("The top story is about a storm. Roads are closed")))
    brain.ask("Argus, top news?")
    assert finished.wait(5)
    assert voice.spoken == ["The top story is about a storm.", "Roads are closed"]


def test_brain_approval_goes_to_the_screen_not_the_voice(tmp_path):
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspace" / "old.txt").write_text("x")
    script = Script(call("run_command", {"command": "rm old.txt"}, text="Deleting it. "), say("Done, it's gone."))
    brain, voice, approver, finished = make_brain(tmp_path, script, answers=[ONCE])
    brain.ask("delete old.txt")
    assert finished.wait(5)
    assert voice.spoken == ["Deleting it.", "I need your OK for that. Please check the screen.", "Done, it's gone."]
    assert approver.seen[0].description.startswith("deleting files")
    assert not (tmp_path / "workspace" / "old.txt").exists()


def test_brain_reports_problems_out_loud(tmp_path):
    agent, mock = make_agent(tmp_path, Script(say("hi")))
    agent.llm.api_key = "sk-ant-wrong"
    voice = RecordingVoice()
    brain = Brain(agent, Speaker(voice), DenyAll(), Audit(tmp_path / "a.jsonl"), Sessions(agent.store))
    brain.ask("hello")
    brain.wait(5)
    assert wait_for(lambda: voice.spoken == ["My Claude API key isn't working. Please check it."])
    assert spoken_error("couldn't reach the Claude API (timeout)").startswith("I can't reach")


def test_brain_cancel_stops_mid_task(tmp_path):
    started = threading.Event()

    def responder(body):
        started.set()
        return call("run_command", {"command": "sleep 5"})

    agent, _ = make_agent(tmp_path, responder)
    voice = RecordingVoice()
    finished = threading.Event()
    brain = Brain(agent, Speaker(voice), DenyAll(), Audit(tmp_path / "a.jsonl"), Sessions(agent.store),
                  on_reply_finished=finished.set)
    brain.ask("long task")
    assert started.wait(5)
    brain.cancel()
    brain.wait(8)
    assert not brain.busy and not finished.is_set()


def test_sessions_rotate(tmp_path):
    agent, _ = make_agent(tmp_path, Script())
    s = Sessions(agent.store, idle_minutes=30)
    first = s.current()
    assert s.current() == first
    s.rotate()
    assert s.current() != first


# ---- the listening loop (scripted transcripts, fake microphone) -----------------------------------------


def test_only_addressed_speech_reaches_the_brain(tmp_path):
    argus, voice, script = make_argus(tmp_path, ["We should paint the kitchen.", "Argus, what time is it?"],
                                      Script(say("It's ten past four.")))
    argus.handle_utterance(SPEECH)
    assert script.bodies == [] and argus.ignored == 1
    argus.handle_utterance(SPEECH)
    argus.brain.wait(5)
    assert script.bodies[0]["messages"][-1]["content"][0]["text"] == "Argus, what time is it?"
    assert wait_for(lambda: voice.spoken == ["It's ten past four."])
    assert "paint the kitchen" not in (tmp_path / "audit.jsonl").read_text()


def test_voice_commands_stay_local(tmp_path):
    argus, voice, script = make_argus(tmp_path, ["Argus, go to sleep", "Argus what's the time", "Argus wake up",
                                                 "Argus turn off the microphone"])
    for _ in range(4):
        argus.handle_utterance(SPEECH)
        argus.speaker.wait_idle(2)
    assert script.bodies == []
    assert voice.spoken[0].startswith("Going quiet") and "I'm back." in voice.spoken
    assert argus.status()["mode"] == "microphone off" and not argus.source.is_open


def test_double_clap_wakes_from_sleep_and_opens_listening(tmp_path):
    argus, voice, _ = make_argus(tmp_path, ["Argus, go to sleep"])
    argus.handle_utterance(SPEECH)
    argus.speaker.wait_idle(2)
    argus.on_clap()
    argus.speaker.wait_idle(2)
    assert not argus.listener.sleeping and voice.spoken[-1] == "I'm back."
    argus.on_clap()
    assert wait_for(lambda: argus.listener.in_followup())
    assert "clap" in (tmp_path / "audit.jsonl").read_text()


def test_locking_the_mac_closes_the_microphone(tmp_path):
    argus, _, _ = make_argus(tmp_path, [])
    assert argus.source.is_open
    argus.set_locked(True)
    assert not argus.source.is_open and argus.status()["mode"] == "paused (Mac locked)"
    argus.set_locked(False)
    assert argus.source.is_open


def test_short_utterances_and_mic_off_are_ignored(tmp_path):
    argus, _, script = make_argus(tmp_path, ["Argus hello"])
    argus.handle_utterance(np.ones(1000, dtype=np.float32))
    argus.set_mic(False)
    argus.handle_utterance(SPEECH)
    assert script.bodies == []


class FakePrint:
    threshold, enrolled = 0.5, True

    def __init__(self, score):
        self._score = score

    def score(self, samples):
        return self._score


def test_voice_lock_ignores_other_voices_without_changing_state(tmp_path):
    argus, _, script = make_argus(tmp_path, ["Argus, go to sleep", "Argus what's new"], Script(say("Not much.")))
    argus.voiceprint, argus.voice_lock = FakePrint(0.1), True
    argus.handle_utterance(SPEECH)                       # a stranger can't put Argus to sleep
    assert not argus.listener.sleeping and script.bodies == []
    argus.voiceprint = FakePrint(0.9)
    argus.handle_utterance(SPEECH)
    argus.brain.wait(5)
    assert len(script.bodies) == 1
    assert "voice_not_recognised" in (tmp_path / "audit.jsonl").read_text()


def test_own_voice_is_not_taken_as_a_follow_up(tmp_path):
    argus, _, script = make_argus(tmp_path, ["the top story today is about a storm"])
    argus.speaker._recent.append((argus.speaker.clock(), "The top story today is about a storm in the Bay Area."))
    argus.listener.reply_finished()
    argus.handle_utterance(SPEECH)
    assert script.bodies == [] and argus.listener.in_followup()
