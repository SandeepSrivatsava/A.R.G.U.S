"""Echo cancellation through the argus-ear helper (ear.py), with a stand-in helper that speaks its protocol."""
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from argus import ear as ear_mod
from argus.ear import EchoCancelledMicrophone, Ear

FAKE = r'''
import sys, threading, time
mode = "MODE"
if mode == "broken":
    sys.stderr.write("error voice processing isn't available\n"); sys.stderr.flush(); sys.exit(2)
if mode == "slow":
    time.sleep(5); sys.exit(0)
sys.stderr.write("ready\n"); sys.stderr.flush()
def mic():
    chunk = (b"\x00\x10" * 512)
    for _ in range(40 if mode != "mute" else 0):
        sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush(); time.sleep(0.01)
    if mode == "mute":
        time.sleep(30)
threading.Thread(target=mic, daemon=True).start()
pending = []
for line in sys.stdin:
    parts = line.split()
    if not parts:
        continue
    if parts[0] == "play":
        pending.append(parts[1])
        if mode != "hold":
            time.sleep(0.05); sys.stderr.write("done %s\n" % parts[1]); sys.stderr.flush(); pending.clear()
    elif parts[0] == "stop":
        for key in pending:
            sys.stderr.write("done %s\n" % key)
        sys.stderr.flush(); pending.clear()
    elif parts[0] == "quit":
        break
    else:
        sys.stderr.write("error unknown command\n"); sys.stderr.flush()
'''


def helper(tmp_path, mode="ok"):
    path = tmp_path / f"argus-ear-{mode}"
    path.write_text(f"#!{sys.executable}\n" + FAKE.replace("MODE", mode))
    path.chmod(0o755)
    return path


def test_listening_and_playing_through_the_helper(tmp_path):
    errors = []
    ear = Ear(helper(tmp_path), on_error=errors.append)
    assert ear.start() and ear.running and ear.start()
    chunk = ear.read(2)
    assert chunk is not None and chunk.dtype == np.float32 and abs(chunk[0] - 4096 / 32768) < 1e-6
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"")
    assert ear.play(str(wav)) is True
    ear._send("dance")
    deadline = time.monotonic() + 3
    while not errors and time.monotonic() < deadline:
        time.sleep(0.05)
    assert errors == ["unknown command"]
    ear.stop_process()
    assert not ear.running and ear.play(str(wav)) is False
    ear.drain()
    assert ear.read(0.01) is None


def test_playing_stops_when_argus_is_stopped(tmp_path):
    ear = Ear(helper(tmp_path, "hold"))
    assert ear.start()
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    began = time.monotonic()
    assert ear.play("/tmp/x.wav", stopped=stop.is_set) is True
    assert time.monotonic() - began < 2
    assert ear.play("/tmp/x.wav", timeout=0.2) is True             # nobody answers: gives up after the timeout
    ear.stop_process()


def test_a_helper_that_is_missing_broken_or_slow(tmp_path):
    missing = Ear(tmp_path / "nope")
    assert not missing.start() and "isn't built" in missing.error
    broken = Ear(helper(tmp_path, "broken"), ready_timeout=3)
    assert not broken.start() and "voice processing" in broken.error
    slow = Ear(helper(tmp_path, "slow"), ready_timeout=0.3)
    assert not slow.start() and "didn't get ready" in slow.error
    def boom(*a, **k):
        raise OSError("no exec")
    assert not Ear(helper(tmp_path), popen=boom).start()


class PlainMic:
    def __init__(self):
        self.opened = 0
        self.closed = 0
        self.overflows = 3

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def read(self, timeout=0.25):
        return np.zeros(4, dtype=np.float32)


def test_the_microphone_falls_back_when_echo_cancellation_cant_run(tmp_path):
    audit = []
    plain = PlainMic()
    mic = EchoCancelledMicrophone(Ear(tmp_path / "nope"), plain, audit=type("A", (), {"log": lambda s, e, **k: audit.append(e)})())
    assert mic.read() is None
    mic.open()
    mic.open()
    assert mic.is_open and not mic.using_ear and plain.opened == 1 and audit == ["echo_cancel_off"]
    assert mic.read().size == 4 and mic.overflows == 3
    mic.close()
    mic.close()
    assert plain.closed == 1
    mic.open()                                          # gave up for good: no second try, no second log
    assert audit == ["echo_cancel_off"]


def test_the_microphone_uses_the_helper_and_drops_it_when_it_goes_silent(tmp_path, monkeypatch):
    plain = PlainMic()
    ear = Ear(helper(tmp_path, "mute"))
    mic = EchoCancelledMicrophone(ear, plain)
    mic.open()
    assert mic.using_ear and plain.opened == 0
    monkeypatch.setattr(EchoCancelledMicrophone, "SILENT_AFTER", 0.0)
    ear.last_sound = time.monotonic() - 1
    assert mic.read(0.05) is None
    assert not mic.using_ear and plain.opened == 1 and mic.gave_up
    mic.close()
    good = EchoCancelledMicrophone(Ear(helper(tmp_path)), PlainMic())
    good.open()
    assert good.read(2) is not None
    good.close()
    assert not good.using_ear


def test_the_voice_plays_through_the_helper_or_falls_back(tmp_path):
    from argus.voice import RecordingVoice, _NeuralVoice, attach_sink

    class Tone(_NeuralVoice):
        def __init__(self):
            super().__init__()
            self._init_playback(["true"], str(tmp_path))
            self.ran = []

        def synthesize(self, text):
            return np.zeros(2400, dtype=np.float32), 24000

        def _run(self, args, stdin_text=None):
            self.ran.append(args[0])

    class Sink:
        def __init__(self, ok):
            self.ok, self.played, self.stopped = ok, [], 0

        def play(self, path, stopped=lambda: False):
            self.played.append(path)
            return self.ok

        def stop(self):
            self.stopped += 1
    voice = Tone()
    assert attach_sink(voice, Sink(True)) == 1 and attach_sink(RecordingVoice(), Sink(True)) == 0
    voice.speak("Hello.")
    assert voice.sink.played and voice.ran == []
    voice.stop()
    assert voice.sink.stopped == 1
    voice.reset()
    voice.sink = Sink(False)                              # helper not running: afplay as before
    voice.speak("Hello again.")
    assert voice.ran == ["true"]


def test_argus_ear_is_never_taken_for_a_call():
    from argus.micwatch import on_a_call
    assert on_a_call(["argus-ear", "us.zoom.xos"]) == ["us.zoom.xos"]


def test_the_swift_helper_and_build_steps_exist():
    repo = Path(__file__).resolve().parent.parent
    source = (repo / "macos" / "argus-ear.swift").read_text()
    assert "setVoiceProcessingEnabled(true)" in source and '"ready"' in source and "done \\(id)" in source
    assert "argus-ear.swift" in (repo / "setup.sh").read_text()
    assert "argus-ear.swift" in (repo / "scripts" / "update.sh").read_text()
