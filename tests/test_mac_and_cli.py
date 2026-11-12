"""The Mac-only code, run against strict stand-ins for macOS's frameworks (Quartz, AppKit, LocalAuthentication,
rumps, sounddevice), plus the voice engines, the listening loop's edges and the argusctl command line.

The stand-ins only have the names Argus is meant to use, so a typo in Argus fails here instead of on the Mac.
Those names are checked against the real PyObjC 13.0 frameworks by test_pyobjc_names_exist (needs
ARGUS_PYOBJC_WHEELS pointing at unpacked macOS wheels; skipped otherwise)."""
import json
import os
import re
import io
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from argus import approvals as approvals_mod
from argus import computer as computer_mod
from argus import guard as guard_mod
from argus.approvals import DENY, ONCE, TASK, ApprovalRequest, MacApprover
from argus.audit import Audit
from argus.computer import Computer, MacBackend
from argus.control import ControlServer
from argus.voice import SayVoice, make_voice

from fakes import SPEECH, Script, make_argus, say, wait_for

MODELS = Path(os.environ.get("ARGUS_TEST_MODELS", "/nonexistent"))


def module(name, **attrs):
    """A stand-in module with exactly these attributes (anything else raises AttributeError)."""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


# ---- Quartz ------------------------------------------------------------------------------------------------
CONSTANTS = dict(
    kCGEventLeftMouseDown=1, kCGEventLeftMouseUp=2, kCGEventRightMouseDown=3, kCGEventRightMouseUp=4,
    kCGEventMouseMoved=5, kCGEventLeftMouseDragged=6, kCGEventOtherMouseDown=25, kCGEventOtherMouseUp=26,
    kCGMouseButtonLeft=0, kCGMouseButtonRight=1, kCGMouseButtonCenter=2, kCGHIDEventTap=0, kCGScrollEventUnitLine=1,
    kCGMouseEventClickState=1, kCGEventFlagMaskCommand=1 << 20, kCGEventFlagMaskShift=1 << 17,
    kCGEventFlagMaskAlternate=1 << 19, kCGEventFlagMaskControl=1 << 18, kCGEventFlagMaskSecondaryFn=1 << 23,
    kCGWindowListOptionOnScreenOnly=1, kCGWindowListExcludeDesktopElements=16, kCGNullWindowID=0)


@pytest.fixture
def quartz(monkeypatch):
    posted, state = [], {"screen_ok": True, "requested": False, "session": {"CGSSessionScreenIsLocked": False},
                         "windows": []}

    def event(**fields):
        return dict(fields)

    def post(tap, ev):
        posted.append(ev)

    def set_flags(ev, flags):
        ev["flags"] = flags

    def set_field(ev, field, value):
        ev["click_state"] = value

    def set_unicode(ev, length, text):
        ev.update(length=length, text=text)

    def preflight():
        return state["screen_ok"]

    def request():
        state["requested"] = True
        return False

    q = module("Quartz", **CONSTANTS,
               CGMainDisplayID=lambda: 1,
               CGDisplayBounds=lambda d: SimpleNamespace(size=SimpleNamespace(width=1440.0, height=900.0)),
               CGEventCreateMouseEvent=lambda src, kind, point, button: event(kind=kind, point=tuple(point), button=button),
               CGEventCreateScrollWheelEvent=lambda src, unit, count, dy, dx: event(kind="scroll", unit=unit, dy=dy, dx=dx),
               CGEventCreateKeyboardEvent=lambda src, code, down: event(kind="key", code=code, down=down),
               CGEventKeyboardSetUnicodeString=set_unicode, CGEventSetFlags=set_flags,
               CGEventSetIntegerValueField=set_field, CGEventPost=post,
               CGEventCreate=lambda src: "now", CGEventGetLocation=lambda ev: SimpleNamespace(x=100.5, y=200.25),
               CGPreflightScreenCaptureAccess=preflight, CGRequestScreenCaptureAccess=request,
               CGSessionCopyCurrentDictionary=lambda: state["session"],
               CGWindowListCopyWindowInfo=lambda options, window: state["windows"])
    monkeypatch.setitem(sys.modules, "Quartz", q)
    monkeypatch.setattr(computer_mod.time, "sleep", lambda s: None)
    return SimpleNamespace(posted=posted, state=state, q=q)


def test_mouse_actions_post_the_right_events(quartz):
    b = MacBackend()
    assert b.screen_size() == (1440, 900)
    b.click(10, 20, "left", 2, ["cmd", "shift"])
    kinds = [(e["kind"], e.get("click_state")) for e in quartz.posted]
    assert kinds == [(5, None), (1, 1), (2, 1), (1, 2), (2, 2)]          # move, then two down/up pairs
    assert quartz.posted[1]["flags"] == (1 << 20) | (1 << 17)
    quartz.posted.clear()
    b.click(1, 2, "right", 1, [])
    b.click(1, 2, "middle", 1, [])
    assert [e["button"] for e in quartz.posted if e["kind"] != 5] == [1, 1, 2, 2]
    quartz.posted.clear()
    b.drag((0, 0), (100, 50), ["option"])
    assert [e["kind"] for e in quartz.posted] == [5, 1] + [6] * 20 + [2] and quartz.posted[-1]["point"] == (100, 50)
    quartz.posted.clear()
    b.scroll(5, 5, 0, -3, [])
    assert quartz.posted[-1] == {"kind": "scroll", "unit": 1, "dy": -3, "dx": 0}
    b.button(1, 1, True)
    b.button(1, 1, False)
    assert [e["kind"] for e in quartz.posted[-2:]] == [1, 2]
    assert b.cursor() == (100.5, 200.25)


def test_typing_and_keys(quartz):
    b = MacBackend()
    b.type_text("hi 👋" + "x" * 20)
    first = quartz.posted[0]
    assert first["text"] == "hi 👋" + "x" * 12 and first["length"] == 17          # the emoji is two UTF-16 units
    assert len(quartz.posted) == 4                                               # two chunks, down + up each
    quartz.posted.clear()
    b.key(["cmd", "shift"], "t")
    codes = [(e["code"], e["down"]) for e in quartz.posted]
    assert codes == [(55, True), (56, True), (17, True), (17, False), (56, False), (55, False)]
    quartz.posted.clear()
    b.key([], "é")                                                               # no key code: typed instead
    assert quartz.posted[0]["text"] == "é"
    quartz.posted.clear()
    b.key(["fn"], None, hold=0.5)                                               # fn has no key event of its own
    assert quartz.posted == []


def test_screenshots(quartz, monkeypatch, tmp_path):
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[0] == "screencapture":
            Path(cmd[-1]).write_bytes(b"\x89PNG" + b"\0" * (500 if not commands_empty else 10))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    commands_empty = False
    monkeypatch.setattr(computer_mod.subprocess, "run", run)
    b = MacBackend(tmp_dir=str(tmp_path))
    png = b.capture((10, 20, 300, 200), (1280, 800))
    assert png.startswith(b"\x89PNG")
    assert commands[0][:6] == ["screencapture", "-x", "-t", "png", "-m", "-R"] and commands[0][6] == "10,20,300,200"
    assert commands[1][:4] == ["sips", "-z", "800", "1280"]
    commands_empty = True
    with pytest.raises(RuntimeError, match="Screen Recording"):
        b.capture(None, (1280, 800))
    assert list(tmp_path.iterdir()) == []                                          # no screenshots left behind


def test_the_whole_screen_path_scales_and_reports_errors(quartz):
    c = Computer(MacBackend())
    assert c.run("left_click", {"coordinate": [640, 400]}).content == "OK"
    assert quartz.posted[0]["point"] == (720.0, 450.0)
    assert c.run("cursor_position", {}).content == "X=89,Y=178"
    assert "failed" in c.run("left_click", {"coordinate": "middle"}).content
    assert "failed" in c.run("zoom", {"region": [10, 10, 5, 5]}).content
    assert "failed" in c.run("type", {"text": "x" * 5000}).content
    assert "failed" in c.run("teleport", {}).content


def test_permission_checks(quartz, monkeypatch):
    monkeypatch.setitem(sys.modules, "ApplicationServices", module(
        "ApplicationServices", kAXTrustedCheckOptionPrompt="AXTrustedCheckOptionPrompt",
        AXIsProcessTrustedWithOptions=lambda opts: opts == {"AXTrustedCheckOptionPrompt": False}))
    assert computer_mod.check_permissions() == {"screen_recording": True, "accessibility": True}
    quartz.state["screen_ok"] = False
    assert computer_mod.request_permissions() == {"screen_recording": False, "accessibility": False}
    assert quartz.state["requested"]
    monkeypatch.setitem(sys.modules, "ApplicationServices", None)              # library missing
    assert computer_mod.check_permissions()["accessibility"] is False


def test_missing_pyobjc_means_no_screen_control(monkeypatch):
    monkeypatch.setitem(sys.modules, "Quartz", None)
    with pytest.raises(ImportError):
        MacBackend()


# ---- what's on screen and whether the Mac is locked ------------------------------------------------------
def test_lock_detection(quartz, monkeypatch):
    assert guard_mod.screen_locked() is False
    quartz.state["session"] = {"CGSSessionScreenIsLocked": True}
    assert guard_mod.screen_locked() is True
    quartz.state["session"] = None                                              # login window: treat as locked
    assert guard_mod.screen_locked() is True
    monkeypatch.setitem(sys.modules, "Quartz", None)
    assert guard_mod.screen_locked() is None


def test_front_and_visible_apps(quartz, monkeypatch):
    app = SimpleNamespace(bundleIdentifier=lambda: "com.agilebits.onepassword7", localizedName=lambda: "1Password 7")
    apps = {42: app, 7: SimpleNamespace(bundleIdentifier=lambda: None, localizedName=lambda: "Terminal")}
    appkit = module("AppKit",
                    NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(frontmostApplication=lambda: app)),
                    NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=apps.get))
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    assert guard_mod.frontmost_app() == ("com.agilebits.onepassword7", "1Password 7")
    quartz.state["windows"] = [{"kCGWindowLayer": 0, "kCGWindowOwnerName": "1Password 7", "kCGWindowOwnerPID": 42},
                               {"kCGWindowLayer": 25, "kCGWindowOwnerName": "Menu Bar", "kCGWindowOwnerPID": 1},
                               {"kCGWindowLayer": 0, "kCGWindowOwnerName": "Terminal", "kCGWindowOwnerPID": 7},
                               {"kCGWindowLayer": 0, "kCGWindowOwnerName": "Terminal", "kCGWindowOwnerPID": 7}]
    assert guard_mod.visible_apps() == ["1Password 7", "com.agilebits.onepassword7", "Terminal"]
    state = guard_mod.desktop_state()
    assert state["front_bundle"] == "com.agilebits.onepassword7" and "Terminal" in state["visible_apps"]


def test_front_app_falls_back_to_lsappinfo(monkeypatch):
    monkeypatch.setitem(sys.modules, "AppKit", None)
    outputs = {"front": 'ASN:0x0-0x1234:\n',
               "info": '"CFBundleIdentifier"="com.apple.Safari"\n"LSDisplayName"="Safari"\n'}

    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, outputs["front" if cmd[1] == "front" else "info"], "")
    monkeypatch.setattr(guard_mod.subprocess, "run", run)
    assert guard_mod.frontmost_app() == ("com.apple.Safari", "Safari")
    outputs["front"] = ""
    assert guard_mod.frontmost_app() is None
    monkeypatch.setitem(sys.modules, "Quartz", None)
    assert guard_mod.visible_apps() is None


# ---- approvals: dialog + Touch ID --------------------------------------------------------------------------
def la_module(can=True, reply=True, call_reply=True):
    calls = []

    class LAContext:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def canEvaluatePolicy_error_(self, policy, error):
            calls.append(("can", policy))
            return can, None

        def evaluatePolicy_localizedReason_reply_(self, policy, reason, callback):
            calls.append(("evaluate", policy, reason))
            if call_reply:
                threading.Thread(target=callback, args=(reply, None)).start()

        def invalidate(self):
            calls.append(("invalidate",))
    return module("LocalAuthentication", LAContext=LAContext), calls


def test_touch_id(monkeypatch):
    mod, calls = la_module()
    monkeypatch.setitem(sys.modules, "LocalAuthentication", mod)
    assert approvals_mod.touch_id("approve") is True and calls[0] == ("can", 2)
    monkeypatch.setitem(sys.modules, "LocalAuthentication", la_module(reply=False)[0])
    assert approvals_mod.touch_id("approve") is False
    monkeypatch.setitem(sys.modules, "LocalAuthentication", la_module(can=False)[0])
    assert approvals_mod.touch_id("approve") is False
    mod, calls = la_module(call_reply=False)                                   # nobody touches the sensor
    monkeypatch.setitem(sys.modules, "LocalAuthentication", mod)
    assert approvals_mod.touch_id("approve", timeout=0.2) is False and ("invalidate",) in calls
    monkeypatch.setitem(sys.modules, "LocalAuthentication", None)
    assert approvals_mod.touch_id("approve") is False


class FakeDialog:
    """Stands in for the osascript approval dialog (subprocess.Popen)."""

    def __init__(self, button: str = "", *, hang: bool = False, fail: Exception = None):
        self.button, self.hang, self.fail = button, hang, fail
        self.terminated = False
        self.cmd = None

    def __call__(self, cmd, **kw):
        if self.fail is not None:
            raise self.fail
        self.cmd = cmd
        self.stdout = io.StringIO(self.button + "\n")
        return self

    def poll(self):
        return None if self.hang and not self.terminated else 0

    def terminate(self):
        self.terminated = True


@pytest.mark.parametrize("button,touch,expected", [
    ("Allow once", True, ONCE), ("Allow for this task", True, TASK), ("Deny", True, DENY), ("", True, DENY),
    ("Allow once", False, DENY), ("Something else", True, DENY)])
def test_approval_dialog_answers(tmp_path, button, touch, expected):
    audit = Audit(tmp_path / "audit.jsonl")
    request = ApprovalRequest(run_id="r", command="rm notes.txt", description="deleting files",
                              choices=["once", "session", "deny"], pattern_key="shell:deleting files")
    approver = MacApprover(audit=audit, popen=FakeDialog(button), auth=lambda reason, cancel=None: touch)
    assert approver.decide(request) == expected
    if button == "Allow once" and not touch:
        assert "approval_auth_failed" in (tmp_path / "audit.jsonl").read_text()


@pytest.mark.parametrize("problem", [OSError("no window server"), FileNotFoundError("osascript")])
def test_a_dialog_that_cant_open_is_a_no(problem):
    request = ApprovalRequest(run_id="r", command="rm x", choices=["once", "deny"])
    assert MacApprover(popen=FakeDialog(fail=problem)).decide(request) == DENY


def test_a_dialog_nobody_answers_is_taken_away_and_is_a_no(monkeypatch):
    dialog = FakeDialog(hang=True)
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(approvals_mod.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(approvals_mod.time, "sleep", lambda s: None)
    request = ApprovalRequest(run_id="r", command="rm x", choices=["once", "deny"])
    assert MacApprover(timeout=60, popen=dialog).decide(request) == DENY
    assert dialog.terminated


def test_without_touch_id_the_dialog_alone_decides():
    request = ApprovalRequest(run_id="r", command="rm x", choices=["once", "deny"])
    approver = MacApprover(require_owner_auth=False, popen=FakeDialog("Allow once"),
                           auth=lambda reason, cancel=None: pytest.fail("Touch ID shouldn't be asked"))
    assert approver.decide(request) == ONCE


def test_paying_always_asks_for_touch_id_even_when_it_is_switched_off():
    asked = []
    request = ApprovalRequest(run_id="r", command="click Place order", choices=["once", "deny"], money=True)
    approver = MacApprover(require_owner_auth=False, popen=FakeDialog("Allow once"),
                           auth=lambda reason, cancel=None: asked.append(reason) or True)
    assert approver.decide(request) == ONCE and asked
    assert "involves paying" in approver.message(request)


# ---- the menu bar ------------------------------------------------------------------------------------------
@pytest.fixture
def rumps(monkeypatch):
    made = {}

    class MenuItem:
        def __init__(self, title, callback=None):
            self.title, self.callback, self.state = title, callback, 0

    class App:
        def __init__(self, name, title=None, quit_button="Quit"):
            self.name, self.title, self.quit_button, self.menu = name, title, quit_button, []

        def run(self):
            made["app"] = self

    quits = []
    mod = module("rumps", App=App, MenuItem=MenuItem, timer=lambda seconds: (lambda f: f),
                 quit_application=lambda: quits.append(True))
    monkeypatch.setitem(sys.modules, "rumps", mod)
    return SimpleNamespace(made=made, quits=quits)


def test_menu_bar_reflects_and_controls_argus(tmp_path, rumps, monkeypatch):
    from argus import app as app_mod
    argus, voice, _ = make_argus(tmp_path, [])
    glasses = SimpleNamespace(connected=True, app_alive=lambda: True)
    stopped, opened = [], []
    app = SimpleNamespace(argus=argus, settings=SimpleNamespace(audit_path=tmp_path / "audit.jsonl"),
                          glasses=glasses, stop=lambda: stopped.append(True))
    monkeypatch.setattr(app_mod.subprocess, "run", lambda cmd, **kw: opened.append(cmd))
    quit_event = threading.Event()
    app_mod.run_menubar(app, quit_event)
    menu = rumps.made["app"]
    items = {getattr(i, "title", None): i for i in menu.menu if i is not None}
    assert menu.quit_button is None and len([i for i in menu.menu if i is None]) == 3
    menu.refresh(None)
    assert menu.title == "◉" and "glasses on" in menu.status_item.title and menu.mic_item.state == 1
    items["Microphone"].callback(None)                                             # mic off
    menu.refresh(None)
    assert not argus.mic_wanted and menu.title == "◌" and menu.mic_item.state == 0
    items["Conversation mode (no name needed)"].callback(None)
    assert argus.listener.conversation
    items["Allow Argus to see and use the screen"].callback(None)
    assert argus.screen_private
    menu.refresh(None)
    assert menu.screen_item.state == 0
    [i for t, i in items.items() if t.startswith("Asleep")][0].callback(None)
    assert argus.listener.sleeping
    items["New conversation"].callback(None)
    items["Stop talking / cancel task"].callback(None)
    items["Open activity log"].callback(None)
    assert opened[0][:3] == ["open", "-a", "Console"]
    items["Quit Argus"].callback(None)
    assert quit_event.is_set() and stopped and rumps.quits


# ---- the microphone -----------------------------------------------------------------------------------------
@pytest.fixture
def sounddevice(monkeypatch):
    streams = []

    class InputStream:
        def __init__(self, **kwargs):
            self.kwargs, self.started, self.closed = kwargs, False, False
            streams.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "sounddevice", module("sounddevice", InputStream=InputStream))
    return streams


def test_microphone_opens_once_drops_old_audio_and_closes_cleanly(sounddevice):
    from argus.hearing import Microphone
    mic = Microphone("MacBook Air Microphone", block=512, max_backlog_seconds=512 * 2 / 16000)
    mic.open()
    mic.open()
    assert len(sounddevice) == 1 and mic.is_open
    stream = sounddevice[0]
    assert stream.kwargs["samplerate"] == 16000 and stream.kwargs["channels"] == 1 and stream.started
    for i in range(3):                                         # room for 2 blocks: the oldest is dropped
        stream.kwargs["callback"](np.full((512, 1), i, dtype=np.float32), 512, None, "input overflow" if i else None)
    assert mic.overflows == 2
    assert mic.read()[0] == 1 and mic.read()[0] == 2 and mic.read(timeout=0.01) is None
    stream.kwargs["callback"](np.zeros((512, 1), dtype=np.float32), 512, None, None)
    mic.close()
    assert stream.closed and not mic.is_open and mic.read(timeout=0.01) is None


# ---- the voices ---------------------------------------------------------------------------------------------
@pytest.fixture
def fake_say(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "say"
    script.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/args"\ncat > "{tmp_path}/stdin"\n'
                      f'[ -f "{tmp_path}/slow" ] && sleep 10\nexit 0\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return tmp_path


def test_say_voice_sends_text_on_stdin_never_as_arguments(fake_say):
    SayVoice("Daniel", 200).speak("rm -rf / is just words here")
    assert (fake_say / "args").read_text().split() == ["-r", "200", "-v", "Daniel"]
    assert (fake_say / "stdin").read_text() == "rm -rf / is just words here"


def test_stopping_a_voice_cuts_it_off(fake_say):
    (fake_say / "slow").write_text("")
    voice = SayVoice()
    t = threading.Thread(target=voice.speak, args=("a long speech",))
    started = time.monotonic()
    t.start()
    assert wait_for(lambda: voice._proc is not None)
    voice.stop()
    t.join(5)
    assert time.monotonic() - started < 5
    voice.speak("not spoken while stopped")
    assert (fake_say / "stdin").read_text() == "a long speech"
    voice.reset()
    (fake_say / "slow").unlink()
    voice.speak("spoken again")
    assert (fake_say / "stdin").read_text() == "spoken again"


def test_missing_piper_voice_falls_back_to_say(tmp_path):
    assert isinstance(make_voice("piper", piper_dir=str(tmp_path / "nothing")), SayVoice)
    assert isinstance(make_voice("say"), SayVoice)


@pytest.mark.skipif(not (MODELS / "vits-piper-en_US-ryan-medium").exists(), reason="voice model not available")
def test_piper_voice_plays_a_temporary_wav_and_removes_it(tmp_path):
    from argus.voice import PiperVoice
    player = tmp_path / "player"
    player.write_text(f'#!/bin/sh\nwc -c < "$1" > "{tmp_path}/size"\n')
    player.chmod(0o755)
    voice = PiperVoice(str(MODELS / "vits-piper-en_US-ryan-medium"), player=[str(player)], tmp_dir=str(tmp_path),
                       deterministic=True)
    voice.speak("Hello.")
    assert int((tmp_path / "size").read_text()) > 10_000
    assert not list(tmp_path.glob("argus-say-*.wav"))
    with pytest.raises(FileNotFoundError):
        PiperVoice(str(tmp_path))


# ---- the listening loop's edges ----------------------------------------------------------------------------
class OneChunkSource:
    def __init__(self):
        self.is_open, self.finished, self.chunks = False, False, [np.zeros(512, dtype=np.float32)]

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def read(self, timeout=0.25):
        if self.chunks:
            return self.chunks.pop()
        self.finished = True
        return None


def test_loop_ends_with_the_input_and_hears_claps(tmp_path):
    argus, voice, _ = make_argus(tmp_path, [])
    argus.source = OneChunkSource()
    argus.clap = SimpleNamespace(feed=lambda chunk: True)
    flushed = []
    argus.segmenter.flush = lambda: flushed.append(True) or []
    argus.run()
    argus.speaker.wait_idle(2)
    assert voice.spoken == ["Yes?"] and flushed


def test_greeting_is_spoken_when_listening_starts(tmp_path):
    argus, voice, _ = make_argus(tmp_path, [])
    argus.source = OneChunkSource()
    argus.source.chunks = []
    argus.greeting = "Online and listening."
    argus.run()
    assert argus.speaker.wait_idle(2) and voice.spoken == ["Online and listening."]


def test_a_voice_that_fails_doesnt_silence_argus_for_good(tmp_path):
    from argus.voice import RecordingVoice, Speaker

    class Flaky(RecordingVoice):
        def speak(self, text):
            if text == "First.":
                raise OSError("audio device busy")
            super().speak(text)
    voice = Flaky()
    speaker = Speaker(voice)
    speaker.say("First.")
    speaker.say("Second.")
    assert speaker.wait_idle(2) and voice.spoken == ["Second."]


def test_claps_are_ignored_while_busy_muted_or_locked(tmp_path):
    argus, voice, _ = make_argus(tmp_path, [])
    argus.set_mic(False)
    argus.on_clap()
    argus.set_mic(True)
    argus.set_locked(True)
    argus.on_clap()
    argus.speaker.wait_idle(2)
    assert voice.spoken == [] and "clap" not in (tmp_path / "audit.jsonl").read_text()


def test_argus_stop_interrupts_while_speaking(tmp_path):
    argus, voice, _ = make_argus(tmp_path, ["Argus stop"])
    argus.segmenter.speech_active = True
    argus.segmenter.recent_audio = lambda: SPEECH
    argus.speaker._idle.clear()                              # Argus is talking
    cancelled = []
    argus.brain.cancel = lambda: cancelled.append(True)
    argus._speech_since = time.monotonic() - 1               # talking for a second already
    argus._maybe_barge_in()
    argus._maybe_barge_in()                                  # checked at most every 0.5 s
    argus.speaker._idle.set()
    assert cancelled == [True] and "barge_in" in (tmp_path / "audit.jsonl").read_text()


def test_conversation_and_new_conversation_commands(tmp_path):
    argus, voice, _ = make_argus(tmp_path, ["Argus, let's chat", "that's all", "Argus, new conversation"])
    first = argus.brain.sessions.current()
    for _ in range(3):
        argus.handle_utterance(SPEECH)
        argus.speaker.wait_idle(2)
        argus.speaker._last_end -= 5          # the person answers a moment later, not over Argus's last word
    assert voice.spoken[0].startswith("Sure, I'm all ears") and "Okay." in voice.spoken and "Fresh start." in voice.spoken
    assert not argus.listener.conversation and argus.brain.sessions.current() != first


def test_status_while_speaking_and_working(tmp_path):
    argus, voice, _ = make_argus(tmp_path, [])
    argus.speaker._idle.clear()
    assert argus.status()["mode"] == "speaking"
    argus.speaker._idle.set()
    gate = threading.Event()
    argus.brain._thread = threading.Thread(target=gate.wait, args=(5,))
    argus.brain._thread.start()
    assert argus.status()["mode"] == "working"
    gate.set()
    argus.brain._thread.join(2)
    assert argus.status()["mode"] == "listening" and argus.guard_state()["mic"] == "listening"


# ---- argusctl ----------------------------------------------------------------------------------------------
@pytest.fixture
def ctl(tmp_path, monkeypatch):
    home = tmp_path / "argus-home"
    monkeypatch.setenv("ARGUS_HOME", str(home))
    from argus import config as config_mod
    settings = config_mod.load()
    config_mod.ensure_private_dirs(settings)
    argus, voice, script = make_argus(tmp_path, [], Script(say("Answer.")))
    handlers = argus.handlers()
    handlers["permissions"] = lambda req: {"ok": True, "screen_recording": True, "accessibility": False}
    server = ControlServer(settings.socket_path, handlers)
    server.start()
    yield SimpleNamespace(argus=argus, voice=voice, settings=settings, script=script)
    server.stop()


def cli(capsys, *args):
    from argus.__main__ import main
    code = main(list(args))
    return code, capsys.readouterr().out


def test_argusctl_commands_reach_the_running_app(ctl, capsys):
    code, out = cli(capsys, "status")
    assert code == 0 and json.loads(out)["mode"] == "listening"
    cli(capsys, "say", "Hello", "there")
    assert wait_for(lambda: ctl.voice.spoken == ["Hello there"])
    assert json.loads(cli(capsys, "mic", "off")[1])["mode"] == "microphone off"
    cli(capsys, "mic", "on")
    assert json.loads(cli(capsys, "conversation", "on")[1])["conversation"] is True
    assert json.loads(cli(capsys, "sleep")[1])["asleep"] is True
    assert json.loads(cli(capsys, "wake")[1])["asleep"] is False
    assert json.loads(cli(capsys, "screen", "block")[1])["screen_private"] is True
    assert json.loads(cli(capsys, "stop")[1])["ok"] is True
    assert json.loads(cli(capsys, "new")[1]) == {"ok": True}
    assert json.loads(cli(capsys, "ask", "what's", "up")[1]) == {"ok": True}
    assert wait_for(lambda: "Answer." in ctl.voice.spoken)
    assert json.loads(cli(capsys, "say", "")[1]) == {"ok": True}


def test_argusctl_log_token_and_usage(ctl, capsys):
    Audit(ctl.settings.audit_path).log("request", text="hello")
    code, out = cli(capsys, "log", "5")
    assert code == 0 and '"request"' in out
    token = cli(capsys, "phone-token")[1].strip()
    assert len(token) >= 32 and cli(capsys, "phone-token")[1].strip() == token
    assert "Today: $0.00" in cli(capsys, "usage")[1]


def test_doctor_lists_what_is_missing(ctl, capsys):
    code, out = cli(capsys, "doctor", "--offline")
    assert code == 1
    assert "FAIL  voice activity model" in out and "FAIL  Claude API key saved" in out
    assert "PASS  Argus running: listening" in out
    assert "PASS  Screen Recording allowed" in out and "FAIL  Accessibility not allowed yet" in out


def test_argusctl_when_argus_isnt_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "nothing-here"))
    with pytest.raises(SystemExit) as info:
        cli(capsys, "status")
    assert info.value.code == 1 and "Argus isn't running" in capsys.readouterr().out
    code, out = cli(capsys, "doctor", "--offline")
    assert code == 1 and "Argus app isn't running" in out


def test_argusctl_reports_broken_settings_plainly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[listening]\nclap = 'loud'\n")
    code, out = cli(capsys, "status")
    assert code == 2 and "problem in Argus's settings" in out and "clap" in out


def test_argusctl_rejects_unknown_commands(capsys):
    from argus.__main__ import main
    with pytest.raises(SystemExit):
        main(["launch-missiles"])
    with pytest.raises(SystemExit):
        main(["mic", "maybe"])


@pytest.mark.skipif(not (MODELS / "silero_vad.onnx").exists(), reason="speech models not available")
def test_transcribe_command_on_a_recording(tmp_path, monkeypatch, capsys):
    from argus.hearing import write_wav
    from argus.voice import PiperVoice
    home = tmp_path / "argus-home"
    monkeypatch.setenv("ARGUS_HOME", str(home))
    (home / "models").mkdir(parents=True)
    os.symlink(MODELS / "silero_vad.onnx", home / "models" / "silero_vad.onnx")
    os.symlink(MODELS / "sherpa-onnx-moonshine-base-en-int8", home / "models" / "asr")
    tts = PiperVoice(str(MODELS / "vits-piper-en_US-ryan-medium"), deterministic=True)
    samples, rate = tts.synthesize("Argus, what time is it?")
    wav = tmp_path / "q.wav"
    write_wav(str(wav), np.concatenate([np.zeros(8000, np.float32), samples, np.zeros(8000, np.float32)]), rate)
    code, out = cli(capsys, "transcribe", str(wav))
    assert code == 0 and json.loads(out.splitlines()[0])["addressed"] is True


# ---- the Mac API names really exist (optional: needs the macOS PyObjC wheels unpacked) ---------------------
WHEELS = os.environ.get("ARGUS_PYOBJC_WHEELS", "")


@pytest.mark.skipif(not WHEELS, reason="set ARGUS_PYOBJC_WHEELS to unpacked macOS PyObjC 13 wheels")
def test_pyobjc_names_exist():
    blob = "\n".join(p.read_text(errors="replace") for p in Path(WHEELS).rglob("*.py"))
    used = set(CONSTANTS) | {
        "CGMainDisplayID", "CGDisplayBounds", "CGEventCreateMouseEvent", "CGEventCreateScrollWheelEvent",
        "CGEventCreateKeyboardEvent", "CGEventKeyboardSetUnicodeString", "CGEventSetFlags",
        "CGEventSetIntegerValueField", "CGEventPost", "CGEventCreate", "CGEventGetLocation",
        "CGPreflightScreenCaptureAccess", "CGRequestScreenCaptureAccess", "CGSessionCopyCurrentDictionary",
        "CGWindowListCopyWindowInfo", "AXIsProcessTrustedWithOptions", "kAXTrustedCheckOptionPrompt",
        "NSWorkspace", "NSRunningApplication", "LAContext"}
    source = "\n".join(Path(computer_mod.__file__).parent.joinpath(f).read_text()
                       for f in ("computer.py", "guard.py", "approvals.py"))
    in_code = set(re.findall(r"\b(?:Q|Quartz|self\.Q)\.(\w+)", source))
    assert in_code <= used, f"Argus uses names the stand-ins don't cover: {in_code - used}"
    missing = [n for n in used if not re.search(rf"[$'\"]{n}[@$'\"]|\b{n}\s*=|def {n}\b", blob)]
    assert not missing, f"not in PyObjC: {missing}"
