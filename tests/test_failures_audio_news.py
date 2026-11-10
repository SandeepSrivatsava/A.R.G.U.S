"""Negative and error cases for the news reader, audio files, speech models, the voiceprint, the voice brain and
`argusctl enroll` (run end to end against a stand-in microphone when the speech models are available)."""
import os
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from argus import news
from argus.agent import Turn
from argus.approvals import ApprovalRequest
from argus.audit import Audit
from argus.brain import Brain, Sessions
from argus.hearing import read_wav, resample, write_wav
from argus.prompts import system_blocks
from argus.voice import RecordingVoice, Speaker

from fakes import Script, make_agent, say, wait_for

FIXTURES = Path(__file__).parent / "fixtures"
MODELS = Path(os.environ.get("ARGUS_TEST_MODELS", "/nonexistent"))
needs_models = pytest.mark.skipif(not (MODELS / "silero_vad.onnx").exists(), reason="speech models not available")


# ---- news ----------------------------------------------------------------------------------------------------
class Feeds(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _FeedHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.server_port}{path}"

    def handle_error(self, request, client_address):
        pass


class _FeedHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/good":
            body, status = (FIXTURES / "google_news.xml").read_bytes(), 200
        elif self.path == "/huge":
            body, status = b"<rss>" + b" " * (news.MAX_FEED_BYTES + 10) + b"</rss>", 200
        elif self.path == "/junk":
            body, status = b"<html>not a feed", 200
        else:
            body, status = b"server error", 500
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_one_broken_feed_doesnt_sink_the_news():
    server = Feeds()
    try:
        articles, errors = news.collect([("Good", server.url("/good")), ("Down", server.url("/down")),
                                         ("Huge", server.url("/huge")), ("Junk", server.url("/junk"))], 5)
    finally:
        server.shutdown()
    assert len(articles) == 3                     # everything the working feed had
    assert {e.split(":")[0] for e in errors} == {"Down", "Huge", "Junk"}
    assert any("larger than 5 MB" in e for e in errors) and any("not valid RSS" in e for e in errors)


def test_topic_and_custom_feeds(monkeypatch):
    seen = []
    monkeypatch.setattr(news, "collect", lambda feeds, top: seen.append(feeds) or ([], []))
    news.headlines("electric cars & trucks", 3)
    assert seen[-1][0][1].startswith("https://news.google.com/rss/search?q=electric+cars+%26+trucks")
    monkeypatch.setenv("ARGUS_NEWS_FEEDS", "Local|https://example.org/rss; broken-entry ;Other|https://x.org/f")
    news.headlines(None, 3)
    assert seen[-1] == [("Local", "https://example.org/rss"), ("Other", "https://x.org/f")]
    monkeypatch.setenv("ARGUS_NEWS_FEEDS", "no pipes at all")
    news.headlines(None, 3)
    assert seen[-1] == news.DEFAULT_FEEDS


def test_feed_details_that_go_wrong():
    assert news.iso_date("not a date") == "not a date" and news.iso_date("") is None
    items = news.parse_feed(b"<rss><channel><title>Mine</title><item><title></title></item>"
                            b"<item><title>Only title</title></item></channel></rss>")
    assert [(a.title, a.source) for a in items] == [("Only title", "Mine")]
    assert news.merge([], 5) == []


def test_news_tls_falls_back(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)
    assert news.ssl_context() is not None
    monkeypatch.setitem(sys.modules, "certifi", None)
    assert news.ssl_context() is not None


# ---- audio files --------------------------------------------------------------------------------------------
def test_wav_files(tmp_path):
    path = tmp_path / "eight-bit.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(16000)
        w.writeframes(b"\x80" * 100)
    with pytest.raises(ValueError, match="16-bit"):
        read_wav(str(path))
    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(32000)
        w.writeframes(np.array([16384, 0] * 3200, dtype="<i2").tobytes())
    mono = read_wav(str(stereo))
    assert mono.size == 1600 and abs(float(mono.mean()) - 0.25) < 0.01      # averaged and resampled to 16 kHz
    write_wav(str(tmp_path / "loud.wav"), np.array([5.0, -5.0], dtype=np.float32))
    assert np.allclose(read_wav(str(tmp_path / "loud.wav")), [32767 / 32768, -32767 / 32768])   # clipped, not wrapped
    assert resample(np.zeros(0), 8000).size == 0


@needs_models
def test_speech_model_folders_that_are_wrong(tmp_path):
    from argus.hearing import Segmenter, SherpaTranscriber
    with pytest.raises(FileNotFoundError, match="not found"):
        SherpaTranscriber(str(tmp_path / "missing"))
    moon = tmp_path / "half-moonshine"
    moon.mkdir()
    (moon / "preprocess.onnx").write_bytes(b"")
    with pytest.raises(FileNotFoundError, match="missing one of"):
        SherpaTranscriber(str(moon))
    other = tmp_path / "other"
    other.mkdir()
    (other / "readme.txt").write_text("hi")
    with pytest.raises(FileNotFoundError, match="not a Moonshine or Whisper"):
        SherpaTranscriber(str(other))
    stt = SherpaTranscriber(str(MODELS / "sherpa-onnx-moonshine-base-en-int8"))
    assert stt.transcribe(np.zeros(800, dtype=np.float32)) == ""            # too short to be speech
    seg = Segmenter(str(MODELS / "silero_vad.onnx"))
    seg.feed(np.zeros(16000, dtype=np.float32))
    assert seg.recent_audio().size > 0 and not seg.speech_active
    seg.reset()
    assert seg.recent_audio().size == 0 and seg.flush() == []


@needs_models
def test_voiceprint_problems(tmp_path):
    from argus.hearing import VoicePrint
    model = str(MODELS / "wespeaker_en_voxceleb_resnet34_LM.onnx")
    vp = VoicePrint(model, str(tmp_path / "vp.npy"))
    assert not vp.enrolled and vp.score(np.zeros(16000, dtype=np.float32)) == 0.0
    with pytest.raises(ValueError, match="at least 3"):
        vp.enroll([np.zeros(16000, dtype=np.float32)] * 2)
    (tmp_path / "corrupt.npy").write_bytes(b"\x93NUMPY garbage")
    assert not VoicePrint(model, str(tmp_path / "corrupt.npy")).enrolled
    np.save(tmp_path / "wrong-shape.npy", np.zeros((2, 2)))
    assert not VoicePrint(model, str(tmp_path / "wrong-shape.npy")).enrolled
    np.save(tmp_path / "nan.npy", np.full(256, np.nan))
    assert not VoicePrint(model, str(tmp_path / "nan.npy")).enrolled


# ---- the spoken brain ---------------------------------------------------------------------------------------
class SlowStart(Script):
    def __call__(self, body):
        time.sleep(0.2)
        return super().__call__(body)


def make_brain(tmp_path, script, approver=None, progress_after=3.0):
    agent, mock = make_agent(tmp_path, script)
    voice = RecordingVoice()
    speaker = Speaker(voice)
    brain = Brain(agent, speaker, approver, Audit(tmp_path / "audit.jsonl"), Sessions(agent.store),
                  progress_after=progress_after)
    return brain, voice, agent


def test_brain_says_one_moment_for_slow_tools(tmp_path):
    from fakes import call
    brain, voice, _ = make_brain(tmp_path, SlowStart(call("recall", {}), say("Nothing yet.")), progress_after=0.1)
    brain.ask("what do you remember")
    brain.wait(5)
    assert wait_for(lambda: voice.spoken == ["One moment.", "Nothing yet."])


def test_brain_survives_a_crashing_agent(tmp_path):
    brain, voice, agent = make_brain(tmp_path, Script())

    def crash(**kwargs):
        raise RuntimeError("could not reach the database connection")
    agent.run = crash
    brain.ask("hello")
    brain.wait(5)
    assert wait_for(lambda: voice.spoken == ["I can't reach my brain right now. Check the internet connection."])
    assert "database" in brain.last_error


def test_new_request_cancels_the_one_still_running(tmp_path):
    gate = threading.Event()
    started = []

    def run(**kwargs):
        started.append(kwargs["content"])
        kwargs["cancel"].wait(5)
        gate.set()
        return Turn(stopped=True)

    brain, voice, agent = make_brain(tmp_path, Script())
    agent.run = run
    brain.ask("first")
    assert wait_for(lambda: started == ["first"])
    brain.ask("second")
    assert gate.wait(5) and wait_for(lambda: started == ["first", "second"])
    assert "run_stopped" in (tmp_path / "audit.jsonl").read_text()


def test_cancelled_requests_never_ask_for_approval(tmp_path):
    from argus.brain import _VoiceEvents
    brain, voice, _ = make_brain(tmp_path, Script(), approver=None)
    cancelled = threading.Event()
    cancelled.set()
    events = _VoiceEvents(brain, cancelled)
    request = ApprovalRequest(run_id="r", command="rm notes.txt", choices=["once", "deny"])
    assert events.approve(request) == "deny"
    assert voice.spoken == []


def test_prompt_includes_glasses_and_remembered_facts():
    blocks = system_blocks(user="Sandeep", city="", timezone="", workspace="/w", channel="phone",
                           facts=[(3, "Likes tea")], glasses="Ray-Ban Meta connected since 09:00", now=0)
    assert "cache_control" in blocks[0] and "an unspecified city" in blocks[0]["text"]
    assert "Meta glasses: Ray-Ban Meta" in blocks[1]["text"] and "#3 Likes tea" in blocks[1]["text"]
    assert "Siri or the Argus iPhone app" in blocks[1]["text"]
    assert "Plain text" in system_blocks(user="", city="", timezone="", workspace="/w", channel="unknown")[1]["text"]


# ---- argusctl enroll, end to end with a stand-in microphone ------------------------------------------------
@needs_models
def test_enroll_learns_the_voice_and_saves_a_private_voiceprint(tmp_path, monkeypatch, capsys):
    from argus import __main__ as cli
    from argus import hearing
    from argus.voice import PiperVoice
    home = tmp_path / "argus-home"
    monkeypatch.setenv("ARGUS_HOME", str(home))
    models = home / "models"
    models.mkdir(parents=True)
    for name, target in (("silero_vad.onnx", "silero_vad.onnx"), ("asr", "sherpa-onnx-moonshine-base-en-int8"),
                         ("speaker.onnx", "wespeaker_en_voxceleb_resnet34_LM.onnx")):
        os.symlink(MODELS / target, models / name)
    tts = PiperVoice(str(MODELS / "vits-piper-en_US-ryan-medium"), deterministic=True)
    audio = []
    for sentence in cli.ENROLL_SENTENCES:
        samples, rate = tts.synthesize(sentence)
        audio += [np.zeros(8000, np.float32), resample(samples, rate), np.zeros(24000, np.float32)]
    stream = np.concatenate(audio)

    class StandInMicrophone:
        def __init__(self, device=None):
            self.pos = 0

        def open(self):
            pass

        def close(self):
            pass

        def read(self, timeout=0.25):
            if self.pos >= stream.size:
                return None
            chunk = stream[self.pos:self.pos + 512]
            self.pos += 512
            return chunk

    monkeypatch.setattr(hearing, "Microphone", StandInMicrophone)
    code = cli.main(["enroll"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Name recognised in" in out and "Voiceprint saved" in out
    vp = home / "voiceprint.npy"
    assert vp.exists() and oct(vp.stat().st_mode & 0o777) == "0o600"


@needs_models
def test_enroll_with_a_silent_microphone_gives_up_politely(tmp_path, monkeypatch, capsys):
    from argus import __main__ as cli
    from argus import hearing
    home = tmp_path / "argus-home"
    monkeypatch.setenv("ARGUS_HOME", str(home))
    (home / "models").mkdir(parents=True)
    os.symlink(MODELS / "silero_vad.onnx", home / "models" / "silero_vad.onnx")

    class Silent:
        def __init__(self, device=None):
            pass

        def open(self):
            pass

        def close(self):
            pass

        def read(self, timeout=0.25):
            return np.zeros(512, dtype=np.float32)

    clock = [0.0]

    def fake_monotonic():
        clock[0] += 1.0                                    # each read "takes" a second, so 15 s pass quickly
        return clock[0]
    monkeypatch.setattr(hearing, "Microphone", Silent)
    monkeypatch.setattr(cli.time, "monotonic", fake_monotonic)
    assert cli.main(["enroll", "--no-voiceprint"]) == 1
    assert "Not enough speech recorded" in capsys.readouterr().out
