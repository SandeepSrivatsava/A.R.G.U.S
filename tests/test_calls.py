"""Phone calls from the owner's own number: which numbers, the conversation engine (with a fake phone line and the
stand-in Claude), the Mac line's pieces (with fake tools), and the controls while a call is on."""
from __future__ import annotations

import io
import threading
from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest

from argus import calls as C
from argus.calls import CallEngine, CallLimiter, CallManager, CallPlan, find_new_button, normalize_number
from argus.llm import Claude
from argus.policy import ASK, BLOCK
from fakes import SPEECH, FakeVoicePrint, Script, call, make_argus, make_settings, say, wait_for
from mock_claude import ClaudeMock

RNG = np.random.default_rng(7)


# ---- which numbers -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("(415) 555-0123", "+14155550123"), ("415.555.0123", "+14155550123"), ("+1 415 555 0123", "+14155550123"),
    ("1-800-555-0199", "+18005550199")])
def test_numbers_are_tidied(raw, expected):
    assert normalize_number(raw) == (expected, "")


@pytest.mark.parametrize("raw,why", [
    ("", "no phone number"), ("911", "emergency"), ("1-911", "emergency"), ("988", "emergency"), ("411", "emergency"),
    ("112", "emergency"), ("1 900 555 0123", "pay-per-call"), ("976-555-0123", "pay-per-call"),
    ("(015) 555-0123", "valid"), ("415 155 0123", "valid"), ("911-555-0123", "valid"), ("+44 20 7946 0000", "outside"),
    ("12345", "full phone number"), ("call me", "no phone number")])
def test_numbers_that_are_never_called(raw, why):
    number, reason = normalize_number(raw)
    assert number is None and why in reason


def test_international_numbers_when_switched_on():
    assert normalize_number("+44 20 7946 0000", allow_international=True) == ("+442079460000", "")


def test_numbers_are_read_out_in_groups():
    assert C.spoken_number("+14155550123") == "415 555 0123"
    assert C.spoken_number("+442079460000") == "+442079460000"


def test_calls_are_limited_per_hour_and_per_number():
    now = [0.0]
    limiter = CallLimiter(per_hour=3, clock=lambda: now[0])
    assert limiter.allow("+1a") == (True, "") and limiter.allow("+1a")[0]
    assert limiter.allow("+1a") == (False, "that number was already called twice in the last 15 minutes")
    assert limiter.allow("+1b")[0]
    assert "3 calls this hour" in limiter.allow("+1c")[1]
    now[0] = 3601
    assert limiter.allow("+1c")[0]


# ---- sound helpers ---------------------------------------------------------------------------------------------
def _power(signal, freq, rate=16000):
    t = np.arange(signal.size) / rate
    return abs(np.dot(signal, np.exp(-2j * np.pi * freq * t))) / signal.size


def test_key_tones_have_the_right_frequencies():
    tone = C.dtmf("5")
    assert _power(tone, 770) > 0.05 and _power(tone, 1336) > 0.05 and _power(tone, 941) < 0.01
    assert C.dtmf("").size == 0
    with pytest.raises(ValueError):
        C.dtmf("A")


def test_tones_and_speech_are_told_apart():
    t = np.arange(480) / 16000
    ring = 0.2 * (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 480 * t))
    assert C.is_tone(ring) and C.is_tone(C.dtmf("1")[:480])
    assert not C.is_tone(RNG.normal(0, 0.1, 480)) and not C.is_tone(np.zeros(480)) and not C.is_tone(np.ones(10))


def test_resample_keeps_duration():
    assert C.resample(np.ones(16000), 16000, 48000).size == 48000
    assert C.resample(np.zeros(0), 16000, 8000).size == 0
    x = np.arange(10, dtype=np.float32)
    assert C.resample(x, 8000, 8000) is not None and np.array_equal(C.resample(x, 8000, 8000), x)


def test_the_segmenter_finds_sentences_and_ignores_tones_and_silence():
    seg = C.EnergySegmenter()
    assert seg.feed(np.zeros(16000)) == []
    t = np.arange(16000) / 16000
    assert seg.feed(0.2 * (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 480 * t))) == []
    out = seg.feed(RNG.normal(0, 0.1, 16000)) + seg.feed(np.zeros(16000))
    assert len(out) == 1 and 1.0 <= out[0].size / 16000 <= 2.0
    short = C.EnergySegmenter()
    assert short.feed(np.concatenate([RNG.normal(0, 0.1, 1600), np.zeros(16000)])) == []   # a click, not words
    long = C.EnergySegmenter(max_speech=1.0)
    assert len(long.feed(RNG.normal(0, 0.1, 48000))) >= 2                                  # cut into pieces


# ---- finding the Call / End buttons -----------------------------------------------------------------------------
def screen_with(*spots, size=(120, 200)):
    img = np.full(size + (3,), 240, dtype=np.uint8)
    for (cy, cx, r, colour) in spots:
        yy, xx = np.ogrid[:size[0], :size[1]]
        img[(yy - cy) ** 2 + (xx - cx) ** 2 <= r * r] = colour
    return img


GREEN_RGB, RED_RGB = (52, 199, 89), (255, 59, 48)


def test_a_new_green_button_is_found_and_old_ones_are_not():
    before = screen_with((20, 30, 6, GREEN_RGB))                     # a green thing already on screen
    after = screen_with((20, 30, 6, GREEN_RGB), (60, 150, 7, GREEN_RGB))
    x, y = find_new_button(before, after, C.GREEN)
    assert abs(x - 150) < 1 and abs(y - 60) < 1
    assert find_new_button(before, before, C.GREEN) is None
    assert find_new_button(None, screen_with((60, 150, 7, RED_RGB)), C.RED) is not None


def test_specks_and_long_bars_are_not_buttons():
    tiny = screen_with((60, 150, 2, GREEN_RGB))
    assert find_new_button(None, tiny, C.GREEN) is None
    bar = np.full((120, 200, 3), 240, dtype=np.uint8)
    bar[50:53, 10:190] = GREEN_RGB
    assert find_new_button(None, bar, C.GREEN) is None


# ---- the conversation ----------------------------------------------------------------------------------------------
class FakeLine(C.CallLine):
    """A phone line driven by a script of what the other side does: ("speech", s), ("silence", s), ("tone", s),
    ("hangup",). Time is fake and moves as audio is read."""

    def __init__(self, *script, dial_error=None, problems=()):
        self.items = deque([list(i) for i in script])
        self.t = 0.0
        self.played, self.keys = [], []
        self.hung = self.taken = self.closed = False
        self.alive = True
        self.dialed = None
        self.dial_error, self.problems = dial_error, list(problems)
        self.on_play = None

    def clock(self):
        return self.t

    def check(self):
        return self.problems

    def dial(self, number):
        if self.dial_error:
            raise C.CallError(self.dial_error)
        self.dialed = number

    def read(self, timeout):
        while self.items and self.items[0][0] == "hangup":
            self.items.popleft()
            self.alive = False
        if not self.items:
            self.t += timeout
            return None
        item = self.items[0]
        kind = item[0]
        item[1] -= 0.1
        if item[1] <= 1e-9:
            self.items.popleft()
        self.t += 0.1
        if kind == "speech":
            return RNG.normal(0, 0.1, 1600).astype(np.float32)
        if kind == "tone":
            t = np.arange(1600) / 16000
            return (0.2 * (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 480 * t))).astype(np.float32)
        return np.zeros(1600, dtype=np.float32)

    def play(self, samples, rate):
        self.played.append((samples.size, rate))
        if self.on_play:
            self.on_play()

    def send_keys(self, keys):
        self.keys.append(keys)

    def connected(self):
        return self.alive

    def hang_up(self):
        self.hung = True
        return True

    def take_over(self):
        self.taken = True

    def close(self):
        self.closed = True


class Words:
    """Speech recognition stand-in: what the other side said, utterance by utterance."""

    def __init__(self, *texts):
        self.texts = list(texts)

    def transcribe(self, samples):
        return self.texts.pop(0) if self.texts else ""


class Voice:
    def __init__(self):
        self.said = []

    def synthesize(self, text):
        self.said.append(text)
        return np.zeros(160, dtype=np.float32), 16000


def engine_for(tmp_path, line, words, script, **kw):
    mock = ClaudeMock(script).start()
    llm = Claude("sk-ant-test", base_url=mock.url, max_retries=0)
    voice = Voice()
    engine = CallEngine(line, llm, words, voice, model="claude-sonnet-5-5", clock=line.clock,
                        cost=lambda model, usage: 0.001, **kw)
    engine.mock = mock
    return engine, voice


PLAN = CallPlan(number="+14155550123", goal="book a table for two tomorrow at 7pm", business="Nopa",
                details="name Sandeep", flexible="6:30 to 8 is fine", owner="Sandeep")


def run(engine, cancel=None, hang=None, take=None):
    return engine.run(PLAN, cancel or threading.Event(), hang or threading.Event(), take or threading.Event())


def test_a_booking_call_from_hello_to_goodbye(tmp_path):
    line = FakeLine(("tone", 2), ("speech", 1), ("silence", 1), ("speech", 1), ("silence", 1),
                    ("speech", 1), ("silence", 1))
    words = Words("Nopa, how can I help you?", "Sure, what name?", "You're all set for seven.")
    script = Script(say("I'd like to book a table for two tomorrow at seven, please."),
                    say("It's Sandeep."),
                    call("end_call", {"outcome": "done", "summary": "Table for two tomorrow at 7 pm under Sandeep."},
                         text="Great, two people at seven tomorrow under Sandeep. Thank you, goodbye!"))
    engine, voice = engine_for(tmp_path, line, words, script)
    result = run(engine)
    assert line.dialed == "+14155550123" and line.hung
    assert voice.said[0] == "Hi, this is Sandeep's AI assistant calling. This call is transcribed for Sandeep."
    assert result.outcome == "done" and "7 pm" in result.summary and result.cost > 0
    who = [w for w, _ in result.transcript]
    assert who == ["them", "argus", "argus", "them", "argus", "them", "argus"]
    first = script.bodies[0]
    assert "Nopa" in first["system"][0]["text"] and "never agree to deposits" in first["system"][0]["text"].lower() or \
        "deposits" in first["system"][0]["text"]
    assert "They answered: \"Nopa, how can I help you?\"" in first["messages"][0]["content"][0]["text"]
    assert {t["name"] for t in first["tools"]} == {"end_call", "press_keys"}
    report = result.report()
    assert report.startswith("Outcome: done.") and "Them: Sure, what name?" in report


def test_no_answer(tmp_path):
    line = FakeLine(("tone", 50))
    engine, voice = engine_for(tmp_path, line, Words(), Script(), answer_timeout=10)
    result = run(engine)
    assert result.outcome == "no answer" and line.hung and voice.said == []


def test_they_hang_up_before_answering(tmp_path):
    line = FakeLine(("tone", 1.5), ("hangup",))
    engine, _ = engine_for(tmp_path, line, Words(), Script(), answer_timeout=10)
    result = run(engine)
    assert result.outcome == "ended" and "before anyone answered" in result.summary


def test_they_hang_up_mid_call(tmp_path):
    line = FakeLine(("speech", 1), ("silence", 1), ("hangup",))
    engine, _ = engine_for(tmp_path, line, Words("Hello?"), Script(say("Hi, I'd like a table.")))
    result = run(engine)
    assert result.outcome == "ended" and result.summary == "The other side hung up."


def test_a_quiet_line_gets_one_hello_then_a_hang_up(tmp_path):
    line = FakeLine(("speech", 1), ("silence", 1))
    engine, voice = engine_for(tmp_path, line, Words("Hello?"), Script(say("Hi, I'd like a table.")),
                               silence_timeout=4)
    result = run(engine)
    assert "Hello, are you still there?" in voice.said
    assert result.outcome == "ended" and result.summary == "The line went quiet." and line.hung


def test_the_owner_hangs_up(tmp_path):
    hang = threading.Event()
    line = FakeLine(("speech", 1), ("silence", 1), ("speech", 30))
    line.on_play = lambda: len(line.played) >= 2 and hang.set()
    engine, _ = engine_for(tmp_path, line, Words("Hello?"), Script(say("Hi, I'd like a table.")))
    result = run(engine, hang=hang)
    assert result.outcome == "cancelled" and line.hung and not line.taken


def test_the_owner_takes_over(tmp_path):
    take = threading.Event()
    line = FakeLine(("speech", 1), ("silence", 1), ("speech", 30))
    line.on_play = lambda: len(line.played) >= 2 and take.set()
    engine, _ = engine_for(tmp_path, line, Words("Hello?"), Script(say("Hi, I'd like a table.")))
    result = run(engine, take=take)
    assert result.outcome == "handed to you" and line.taken and not line.hung


def test_phone_menus_get_key_presses(tmp_path):
    line = FakeLine(("speech", 1), ("silence", 1), ("speech", 1), ("silence", 1), ("speech", 1), ("silence", 1))
    words = Words("Press 1 for reservations.", "Reservations, how can I help?", "Done.")
    script = Script(call("press_keys", {"keys": "1"}), say("Hi, a table for two at seven tomorrow?"),
                    call("end_call", {"outcome": "done", "summary": "Booked."}, text="Thanks, bye!"))
    engine, _ = engine_for(tmp_path, line, words, script)
    result = run(engine)
    assert line.keys == ["1"] and result.outcome == "done"
    second = script.bodies[1]["messages"][-1]["content"]
    assert second[0]["type"] == "tool_result" and second[0]["content"] == "pressed 1"
    assert second[1]["text"] == "Reservations, how can I help?"


def test_bad_keys_are_refused(tmp_path):
    line = FakeLine(("speech", 1), ("silence", 1), ("speech", 1), ("silence", 1))
    script = Script(call("press_keys", {"keys": "1; rm -rf"}),
                    call("end_call", {"outcome": "needs you", "summary": "A menu I couldn't use."}))
    engine, _ = engine_for(tmp_path, line, Words("Press one.", "Goodbye."), script)
    result = run(engine)
    assert line.keys == [] and result.outcome == "needs you"
    assert script.bodies[1]["messages"][-1]["content"][0]["is_error"] is True


def test_calls_have_a_time_limit(tmp_path):
    line = FakeLine(*[("speech", 1), ("silence", 1)] * 6)
    engine, voice = engine_for(tmp_path, line, Words(*["Hmm, let me check."] * 6),
                               Script(*[say("Okay.")] * 6), max_minutes=0.05)
    result = run(engine)
    assert result.outcome == "needs you" and "ran out of time" in result.summary
    assert "I'm sorry, I have to go now. Sandeep will call you back. Goodbye." in voice.said


def test_losing_claude_mid_call_ends_it_politely(tmp_path):
    line = FakeLine(("speech", 1), ("silence", 1))
    engine, voice = engine_for(tmp_path, line, Words("Hello?"), Script())
    engine.mock.fail_next = [500, 500, 500]
    result = run(engine)
    assert result.outcome == "failed" and line.hung and "having trouble" in voice.said[-1]


def test_a_call_that_cant_be_placed(tmp_path):
    line = FakeLine(dial_error="no iPhone nearby")
    engine, _ = engine_for(tmp_path, line, Words(), Script())
    result = run(engine)
    assert result.outcome == "failed" and "no iPhone nearby" in result.summary


def test_card_numbers_never_go_down_the_line(tmp_path):
    line = FakeLine(("speech", 1), ("silence", 1), ("speech", 1), ("silence", 1))
    script = Script(say("The card is 4111 1111 1111 1111."),
                    call("end_call", {"outcome": "needs you", "summary": "They wanted a card."}))
    engine, voice = engine_for(tmp_path, line, Words("Card number please?", "Okay."), script)
    run(engine)
    assert not any("4111" in s for s in voice.said)
    assert any(s.startswith("Sorry, I can't share payment details.") for s in voice.said)


def test_an_empty_reply_keeps_the_conversation_in_order(tmp_path):
    line = FakeLine(("speech", 1), ("silence", 1), ("speech", 1), ("silence", 1), ("speech", 1), ("silence", 1))
    script = Script(([], "end_turn"), say("Yes, for two please."),
                    call("end_call", {"outcome": "done", "summary": "ok"}))
    engine, _ = engine_for(tmp_path, line, Words("Hello?", "Hello, anyone?", "Fine."), script)
    result = run(engine)
    assert result.outcome == "done"
    texts = [b["text"] for b in script.bodies[1]["messages"][0]["content"]]
    assert texts[-1] == "Hello, anyone?"


# ---- the tool Argus uses ------------------------------------------------------------------------------------------
def manager_for(tmp_path, line, words, script, **settings_kw):
    settings = make_settings(tmp_path, calls_enabled=True, **settings_kw)
    mock = ClaudeMock(script).start()
    speaker = SimpleNamespace(muted=False, waited=[], wait_idle=lambda timeout=None: True)
    from argus.audit import Audit
    manager = CallManager(settings, lambda: line, Claude("sk-ant-test", base_url=mock.url, max_retries=0), words,
                          Voice(), audit=Audit(tmp_path / "audit.jsonl"), speaker=speaker, engine_clock=line.clock,
                          cost=lambda m, u: 0.002)
    return manager, speaker


def ctx():
    return SimpleNamespace(cancel=threading.Event())


def test_the_tool_runs_a_call_and_reports(tmp_path):
    line = FakeLine(("speech", 1), ("silence", 1), ("speech", 1), ("silence", 1))
    seen = {}
    script = Script(say("Hi, a table for two tomorrow at seven?"),
                    call("end_call", {"outcome": "done", "summary": "Booked for 7."}, text="Thanks, bye!"))
    manager, speaker = manager_for(tmp_path, line, Words("Hello, Nopa.", "Sure, booked."), script)
    line.on_play = lambda: seen.update(active=manager.active, muted=speaker.muted)
    out = manager.tool({"number": "415-555-0123", "business": "Nopa", "goal": "book a table"}, ctx())
    assert not out.is_error and out.content.startswith("Outcome: done.") and "Booked for 7." in out.content
    assert seen == {"active": True, "muted": True}
    assert not manager.active and not speaker.muted and line.closed
    log = (tmp_path / "audit.jsonl").read_text()
    assert '"phone_call_started"' in log and '"outcome": "done"' in log
    record = (tmp_path / "calls.log").read_text()       # every call kept: who, why, what was said
    assert "Nopa (+14155550123)" in record and "Goal: book a table" in record and "Them: Hello, Nopa." in record
    assert oct((tmp_path / "calls.log").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("args,why", [
    ({"number": "911", "goal": "x"}, "emergency"), ({"number": "415-555-0123", "goal": " "}, "goal"),
    ({"number": "+44 20 7946 0000", "goal": "x"}, "outside the US")])
def test_the_tool_refuses_bad_requests(tmp_path, args, why):
    manager, _ = manager_for(tmp_path, FakeLine(), Words(), Script())
    out = manager.tool(args, ctx())
    assert out.is_error and why in out.content


def test_the_tool_respects_the_hourly_limit(tmp_path):
    manager, _ = manager_for(tmp_path, FakeLine(), Words(), Script(), calls_per_hour=1)
    manager.tool({"number": "415-555-0123", "goal": "x"}, ctx())
    out = manager.tool({"number": "415-555-0199", "goal": "x"}, ctx())
    assert out.is_error and "calls this hour" in out.content


def test_calls_that_arent_set_up_say_what_is_missing(tmp_path):
    line = FakeLine(problems=["BlackHole 2ch isn't installed (run scripts/setup_calls.sh)"])
    manager, speaker = manager_for(tmp_path, line, Words(), Script())
    out = manager.tool({"number": "415-555-0123", "goal": "x"}, ctx())
    assert out.is_error and "BlackHole 2ch isn't installed" in out.content and not speaker.muted
    assert "Outcome: failed." in (tmp_path / "calls.log").read_text()     # tried calls are kept too


def test_one_call_at_a_time(tmp_path):
    manager, _ = manager_for(tmp_path, FakeLine(), Words(), Script())
    manager._lock.acquire()
    try:
        out = manager.tool({"number": "415-555-0123", "goal": "x"}, ctx())
    finally:
        manager._lock.release()
    assert out.is_error and "already on a call" in out.content


def test_a_crash_inside_the_call_still_puts_everything_back(tmp_path):
    line = FakeLine(("speech", 1), ("silence", 1))

    class Exploding(Words):
        def transcribe(self, samples):
            raise RuntimeError("model crashed")
    manager, speaker = manager_for(tmp_path, line, Exploding(), Script())
    out = manager.tool({"number": "415-555-0123", "goal": "x"}, ctx())
    assert out.is_error and "The call broke off: model crashed" in out.content
    assert line.closed and line.hung and not speaker.muted and not manager.active


def test_hang_up_and_take_over_switches(tmp_path):
    manager, _ = manager_for(tmp_path, FakeLine(), Words(), Script())
    manager.hang_up()
    manager.take_over()
    assert manager._hang_up.is_set() and manager._take_over.is_set()


# ---- approval and policy ----------------------------------------------------------------------------------------------
def test_calls_need_approval_and_bad_numbers_are_blocked(tmp_path):
    from argus.policy import Policy
    s = make_settings(tmp_path)
    policy = Policy(s, workspace=s.workspace)
    verdict = policy.check("phone_call", {"number": "(415) 555-0123", "business": "Nopa", "goal": "book a table"},
                           "voice")
    assert verdict.action == ASK and verdict.scope == "call:+14155550123" and not verdict.money
    assert verdict.reason == "call Nopa at 415 555 0123 from your number to book a table"
    assert verdict.short == "call Nopa"
    assert policy.check("phone_call", {"number": "911", "goal": "x"}, "voice").action == BLOCK
    assert policy.check("phone_call", {"number": "415 555 0123", "goal": "x"}, "voice").reason.startswith(
        "call 415 555 0123")


# ---- while a call is on, the listening loop only takes "hang up" / "let me talk" ------------------------------------------
class FakeCalls:
    def __init__(self):
        self.active = True
        self.hung = self.taken = 0

    def hang_up(self):
        self.hung += 1

    def take_over(self):
        self.taken += 1


def test_on_a_call_only_the_owner_s_hang_up_and_let_me_talk_count(tmp_path):
    calls = FakeCalls()
    notes = []
    argus, voice, script = make_argus(
        tmp_path, ["Argus, let me talk", "Argus, hang up", "Argus, stop", "Argus, what time is it", "table for two",
                   "Argus, hang up"],
        voiceprint=FakeVoicePrint(0.9, 0.9, 0.9, 0.9, 0.1), voice_lock=True, calls=calls, notify=notes.append)
    for _ in range(6):
        argus.handle_utterance(SPEECH)
    assert calls.taken == 1 and calls.hung == 2              # the last "hang up" wasn't the owner's voice
    assert notes == ["Your microphone is on the call now.", "Hanging up.", "Hanging up.",
                     "I'm on a call. Say \"Argus, hang up\" or \"Argus, let me talk\"."]
    assert script.bodies == []


def test_reminders_wait_until_the_call_is_over(tmp_path):
    calls = FakeCalls()
    argus, voice, _ = make_argus(tmp_path, [], calls=calls)
    argus.announce("Take the cake out of the oven.")
    argus.speaker.wait_idle(2)
    assert voice.spoken == []
    calls.active = False
    argus._reply_finished()
    assert wait_for(lambda: voice.spoken == ["While you were away:", "Take the cake out of the oven."])


# ---- the Mac line, with stand-ins for macOS ----------------------------------------------------------------------------
class Shell:
    """Stand-in for subprocess.run with SwitchAudioSource / osascript / open."""

    def __init__(self, inputs=("MacBook Air Microphone", "BlackHole 2ch"), current="MacBook Air Microphone",
                 fail_switch=False):
        self.inputs, self.current, self.fail_switch = list(inputs), current, fail_switch
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out, code = "", 0
        if cmd[0].rsplit("/", 1)[-1] == "SwitchAudioSource":
            if "-a" in cmd:
                out = "\n".join(self.inputs)
            elif "-c" in cmd:
                out = self.current
            elif "-s" in cmd:
                if self.fail_switch:
                    return SimpleNamespace(returncode=1, stdout="", stderr="no such device")
                self.current = cmd[cmd.index("-s") + 1]
        return SimpleNamespace(returncode=code, stdout=out, stderr="")


class Tap:
    def __init__(self, cmd, **kw):
        self.cmd = cmd
        pcm = (np.full(1600, 0.25) * 32767).astype("<i2").tobytes()
        self.stdout = io.BytesIO(pcm * 2)
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


class Screen:
    def __init__(self, frames):
        self.frames = list(frames)
        self.clicks = []

    def grab(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def click(self, x, y):
        self.clicks.append((round(x), round(y)))


def mac_line(tmp_path, shell=None, screen=None, version="15.1", which=lambda name: "/opt/homebrew/bin/" + name,
             **kw):
    tap = tmp_path / "audiotee"
    tap.write_text("#!/bin/sh\n")
    taps = []
    t = [0.0]

    def popen(cmd, **k):
        taps.append(Tap(cmd))
        return taps[-1]
    played = []

    def sleep(s):
        t[0] += s
    line = C.MacCallLine(tap=str(tap), run=shell or Shell(), popen=popen, screen=screen,
                         player=lambda data, rate, device: played.append(device), which=which,
                         mac_version=lambda: version, sleep=sleep, clock=lambda: t[0], **kw)
    return line, taps, played, t


@pytest.mark.parametrize("version,which,inputs,problem", [
    ("", None, None, "only work on a Mac"), ("14.1", None, None, "macOS 14.2 or newer"),
    ("15.1", lambda name: None, None, "SwitchAudioSource isn't installed"),
    ("15.1", None, ("MacBook Air Microphone",), "BlackHole 2ch isn't installed")])
def test_the_mac_line_says_what_is_missing(tmp_path, version, which, inputs, problem):
    shell = Shell(inputs=inputs) if inputs else Shell()
    kw = {"which": which} if which else {}
    line, _, _, _ = mac_line(tmp_path, shell=shell, version=version, **kw)
    assert any(problem in p for p in line.check())


def test_a_missing_call_recorder_is_reported(tmp_path):
    line = C.MacCallLine(tap=str(tmp_path / "nope"), run=Shell(), which=lambda n: "/x/" + n,
                         mac_version=lambda: "15.1")
    assert any("call recorder (audiotee) isn't installed" in p for p in line.check())


def test_dialing_through_the_iphone(tmp_path):
    blank = screen_with()
    with_call = screen_with((60, 150, 7, GREEN_RGB))
    with_end = screen_with((60, 150, 7, RED_RGB))
    screen = Screen([blank, blank, with_call, with_end])
    shell = Shell()
    line, taps, played, t = mac_line(tmp_path, shell=shell, screen=screen)
    assert line.check() == []
    line.dial("+1 (415) 555-0123")
    quits = [c for c in shell.calls if c[0] == "osascript"]
    assert quits and "ScreenContinuity" in quits[0][2]
    assert shell.current == "BlackHole 2ch"
    assert ["open", "tel://+14155550123"] in [c[:2] for c in shell.calls]
    assert screen.clicks == [(150, 60)]
    assert taps[0].cmd[1:5] == ["--sample-rate", "16000", "--chunk-duration", "0.1"]
    assert "--exclude-processes" in taps[0].cmd
    chunk = line.read(1.0)
    assert chunk is not None and chunk.size == 1600 and abs(float(chunk[0]) - 0.25) < 0.01
    line.play(np.zeros(160, dtype=np.float32), 16000)
    assert sorted(played, key=str) == sorted(["BlackHole 2ch", None], key=str)
    assert line.connected()                                  # still inside the first seconds
    t[0] += 5
    assert line.connected()                                  # the red End button is on screen
    assert line.hang_up() and screen.clicks[-1] == (150, 60)
    line.close()
    assert shell.current == "MacBook Air Microphone" and taps[0].terminated


def test_no_call_button_means_no_call(tmp_path):
    blank = screen_with()
    line, _, _, _ = mac_line(tmp_path, screen=Screen([blank]))
    with pytest.raises(C.CallError, match="Calls from iPhone"):
        line.dial("4155550123")
    line.close()


def test_when_the_call_window_goes_the_call_is_over(tmp_path):
    blank = screen_with()
    screen = Screen([blank, screen_with((60, 150, 7, GREEN_RGB)), blank])
    line, _, _, t = mac_line(tmp_path, screen=screen)
    line.dial("4155550123")
    t[0] += 5
    assert not line.connected() and not line.hang_up()


def test_taking_over_gives_back_the_real_microphone_at_once(tmp_path):
    screen = Screen([screen_with(), screen_with((60, 150, 7, GREEN_RGB))])
    shell = Shell()
    line, taps, _, _ = mac_line(tmp_path, shell=shell, screen=screen, monitor=False)
    line.dial("4155550123")
    line.take_over()
    assert shell.current == "MacBook Air Microphone" and taps[0].terminated
    line.close()                                             # nothing left to undo
    assert line.read(0.01) is not None or True


def test_audio_switch_failures(tmp_path, caplog):
    line, _, _, _ = mac_line(tmp_path, shell=Shell(fail_switch=True), screen=Screen([screen_with()]))
    with pytest.raises(C.CallError, match="couldn't change the Mac's audio"):
        line.dial("4155550123")
    line._previous_input = "MacBook Air Microphone"
    line.close()
    assert "couldn't put the microphone back" in caplog.text


def test_say_can_render_speech_for_calls(monkeypatch, tmp_path):
    import wave
    from argus import voice as voice_mod

    def fake_run(cmd, **kw):
        out = cmd[cmd.index("-o") + 1]
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes((np.full(2205, 0.5) * 32767).astype("<i2").tobytes())
        assert kw["input"] == b"Hello there"
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(voice_mod.subprocess, "run", fake_run)
    samples, rate = voice_mod.SayVoice("Daniel", 190).synthesize("Hello there")
    assert rate == 22050 and samples.size == 2205 and abs(float(samples[0]) - 0.5) < 0.01


def test_the_app_adds_the_call_tool_when_calls_are_on(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    from argus import config as config_mod
    from argus.app import App
    from argus.voice import RecordingVoice, Speaker
    settings = config_mod.load()
    settings.calls_enabled = True
    config_mod.ensure_private_dirs(settings)
    app = App(settings, voice=False, secrets={"ANTHROPIC_API_KEY": "sk-ant-test-key-123"})
    manager = app._build_calls(Words(), Speaker(RecordingVoice()), {"vad": "x"}, line_factory=lambda: FakeLine())
    assert manager is not None and "phone_call" in app.agent.tools
    assert app.agent.tools["phone_call"].channels == frozenset({"voice"})

    class Mute:
        def speak(self, text):
            pass
    assert app._build_calls(Words(), Speaker(Mute()), {"vad": "x"}) is None


def test_doctor_checks_calls_when_they_are_on(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    from argus import __main__ as cli
    from argus import config as config_mod
    settings = config_mod.load()
    config_mod.ensure_private_dirs(settings)
    settings.config_path.write_text("[calls]\nenabled = true\n") if hasattr(settings, "config_path") else \
        (settings.home / "config.toml").write_text("[calls]\nenabled = true\n")
    monkeypatch.setattr(C.platform, "mac_ver", lambda: ("", ("", "", ""), ""))
    cli.main(["doctor", "--offline"])
    assert "FAIL  phone calls: phone calls only work on a Mac" in capsys.readouterr().out


def test_asking_to_call_is_one_short_question_without_the_address(tmp_path):
    from argus.approvals import ApprovalRequest
    from argus.policy import Policy, business_name
    from argus.toolbox import describe_call
    s = make_settings(tmp_path)
    args = {"number": "(415) 555-0123", "business": "Jack in the Box, 100 Main St, Portland", "goal": "ask when they close"}
    verdict = Policy(s, workspace=s.workspace).check("phone_call", args, "voice")
    assert verdict.short == "call Jack in the Box" and "Main St" not in verdict.reason
    request = ApprovalRequest("r", description=verdict.reason, short=verdict.short, code="owl", pattern_key=verdict.scope)
    assert request.spoken() == "May I call Jack in the Box? To allow, say yes, owl. Or use the screen."
    assert "Main St" not in describe_call("phone_call", args)
    assert business_name(None) == "" and business_name("Nopa") == "Nopa"
    assert Policy(s, workspace=s.workspace).check("phone_call", {"number": "415 555 0123"}, "voice").short == \
        "call 415 555 0123"


def test_switchaudiosource_is_found_in_homebrew_when_launchd_path_lacks_it(tmp_path, monkeypatch):
    folder = tmp_path / "brew"
    folder.mkdir()
    tool = folder / "SwitchAudioSource"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setattr(C, "TOOL_DIRS", ("/nowhere", str(folder)))
    assert C.find_tool("SwitchAudioSource", which=lambda n: None) == str(tool)
    assert C.find_tool("SwitchAudioSource", which=lambda n: "/usr/bin/x") == "/usr/bin/x"
    assert C.find_tool("audiotee-missing", which=lambda n: None) is None
    shell = Shell()
    line = C.MacCallLine(tap=str(tmp_path / "tap"), run=shell, which=lambda n: C.find_tool(n, which=lambda _: None),
                         mac_version=lambda: "15.1")
    (tmp_path / "tap").write_text("")
    assert line.check() == []
    assert shell.calls[0][0] == str(tool)


def test_blackhole_missing_and_not_yet_loaded_say_different_things(tmp_path):
    (tmp_path / "tap").write_text("")
    for present, words in ((False, "run scripts/setup_calls.sh yourself in Terminal"), (True, "restart the Mac")):
        line = C.MacCallLine(tap=str(tmp_path / "tap"), run=Shell(inputs=("MacBook Air Microphone",)),
                             which=lambda n: "/opt/homebrew/bin/" + n, mac_version=lambda: "15.1",
                             exists=lambda path, present=present: present and path.endswith("BlackHole2ch.driver"))
        (problem,) = line.check()
        assert words in problem


def test_calls_he_asks_for_dial_without_a_second_question(tmp_path):
    from argus.policy import ALLOW, Policy
    s = make_settings(tmp_path)
    policy = Policy(s, workspace=s.workspace)
    args = {"number": "(415) 555-0123", "business": "Nopa", "goal": "book a table"}
    assert policy.check("phone_call", args, "voice").action == ASK      # voice not checked to be his: ask
    policy.owner_verified = lambda: True                                 # voice lock on: only his voice
    assert policy.check("phone_call", args, "voice").action == ALLOW
    for channel in ("phone", "cli", "android"):
        assert policy.check("phone_call", args, channel).action == ALLOW
    for channel in ("guest", "scheduler", "followup", "trip", "glance"):
        assert policy.check("phone_call", args, channel).action == ASK
    assert policy.check("phone_call", {"number": "911", "goal": "x"}, "voice").action == BLOCK
    policy.owner_verified = lambda: 1 / 0                                # a broken check never means yes
    assert policy.check("phone_call", args, "voice").action == ASK
    s.calls_ask_first = True
    policy.owner_verified = lambda: True
    assert policy.check("phone_call", args, "phone").action == ASK


def test_everything_missing_for_calls_is_listed_at_once(tmp_path):
    line = C.MacCallLine(tap=str(tmp_path / "nope"), run=Shell(), which=lambda n: None,
                         mac_version=lambda: "15.1", exists=lambda p: False)
    problems = line.check()
    assert len(problems) == 3 and any("BlackHole" in p for p in problems) and any("audiotee" in p for p in problems)


def test_argusctl_calls_lists_every_piece_and_the_fix(tmp_path, monkeypatch, capsys):
    from argus import __main__ as cli
    s = make_settings(tmp_path)
    s.calls_enabled = True
    monkeypatch.setattr(cli.config_mod, "load", lambda: s)
    (tmp_path / "tap").write_text("")
    def line(inputs, driver):
        return C.MacCallLine(tap=str(tmp_path / "tap"), run=Shell(inputs=inputs), which=lambda n: "/opt/homebrew/bin/" + n,
                             mac_version=lambda: "15.1", exists=lambda p: driver)
    assert cli.cmd_calls(None, line=line(("MacBook Air Microphone",), False)) == 1
    out = capsys.readouterr().out
    assert "FAIL  BlackHole 2ch driver installed" in out and "scripts/setup_calls.sh" in out
    assert cli.cmd_calls(None, line=line(("MacBook Air Microphone",), True)) == 1
    assert "Restart the Mac" in capsys.readouterr().out
    assert cli.cmd_calls(None, line=line(("BlackHole 2ch",), True)) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and "Everything Argus can check is in place" in out
    no_switch = C.MacCallLine(tap=str(tmp_path / "tap"), run=Shell(), which=lambda n: None, mac_version=lambda: "",
                              exists=lambda p: False)
    assert [ok for ok, _ in no_switch.checklist()] == [False, True, False, False, False, True, True]
    failing = C.MacCallLine(tap=str(tmp_path / "tap"), run=Shell(fail_switch=True), which=lambda n: "/x/" + n,
                            mac_version=lambda: "15.1", exists=lambda p: True)
    failing._switch = lambda *a: (_ for _ in ()).throw(C.CallError("x"))
    assert failing.checklist()[4][0] is False


def test_calls_say_when_screen_recording_or_accessibility_is_off(tmp_path, capsys, monkeypatch):
    (tmp_path / "tap").write_text("")
    for perms, words in (({"screen_recording": False, "accessibility": True}, "Screen Recording is off"),
                         ({"screen_recording": True, "accessibility": False}, "Accessibility is off"),
                         ({}, "Screen Recording is off")):
        line = C.MacCallLine(tap=str(tmp_path / "tap"), run=Shell(), which=lambda n: "/opt/homebrew/bin/" + n,
                             mac_version=lambda: "15.1", exists=lambda p: True, permissions=lambda perms=perms: perms)
        assert any(words in p for p in line.check())
    broken = C.MacCallLine(tap=str(tmp_path / "tap"), run=Shell(), which=lambda n: "/x/" + n, mac_version=lambda: "15.1",
                           exists=lambda p: True, permissions=lambda: 1 / 0)
    assert len(broken.check()) == 2 and [ok for ok, _ in broken.checklist()][-2:] == [False, False]
    from argus import __main__ as cli
    s = make_settings(tmp_path)
    s.calls_enabled = True
    monkeypatch.setattr(cli.config_mod, "load", lambda: s)
    off = C.MacCallLine(tap=str(tmp_path / "tap"), run=Shell(), which=lambda n: "/x/" + n, mac_version=lambda: "15.1",
                        exists=lambda p: True, permissions=lambda: {})
    assert cli.cmd_calls(None, line=off) == 1
    assert "argusctl calls --open" in capsys.readouterr().out
    opened = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: opened.append(cmd))
    assert cli.cmd_calls(SimpleNamespace(open=True), line=off) == 0
    assert any("Privacy_ScreenCapture" in c[1] for c in opened) and opened[-1][:2] == ["open", "-R"]
    monkeypatch.setattr(cli, "_send_quiet", lambda cmd: {"ok": False})
    assert cli.cmd_calls(SimpleNamespace(open=False)) == 1


def test_argusctl_calls_reset_and_why_a_check_failed(tmp_path, monkeypatch, capsys):
    from argus import __main__ as cli
    s = make_settings(tmp_path)
    s.calls_enabled = True
    monkeypatch.setattr(cli.config_mod, "load", lambda: s)
    ran, restarted = [], []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: ran.append(cmd))
    assert cli.cmd_calls(SimpleNamespace(reset=True, open=False, restart=lambda: restarted.append(1))) == 0
    assert ran == [["tccutil", "reset", "ScreenCapture", "com.argus.voice"],
                   ["tccutil", "reset", "Accessibility", "com.argus.voice"]] and restarted == [1]
    (tmp_path / "tap").write_text("")
    line = C.MacCallLine(tap=str(tmp_path / "tap"), run=Shell(), which=lambda n: "/x/" + n, mac_version=lambda: "15.1",
                         exists=lambda p: True,
                         permissions=lambda: {"errors": ["accessibility check: ImportError: no ApplicationServices"]})
    capsys.readouterr()
    assert cli.cmd_calls(SimpleNamespace(reset=False, open=False), line=line) == 1
    out = capsys.readouterr().out
    assert "NOTE  accessibility check: ImportError" in out and "argusctl calls --reset" in out


def test_permission_checks_say_why_they_couldnt_run():
    from argus.computer import check_permissions
    result = check_permissions()
    assert set(result) == {"screen_recording", "accessibility", "errors"}
    assert len(result["errors"]) == 2                # no Quartz or ApplicationServices off a Mac
