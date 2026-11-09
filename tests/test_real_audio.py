"""Real speech through the real on-device models: Piper speaks test sentences, Silero VAD cuts them,
Moonshine transcribes them, and the listening loop decides what reaches the brain.

Needs the model files; set ARGUS_TEST_MODELS to a folder holding silero_vad.onnx,
sherpa-onnx-moonshine-base-en-int8/ and vits-piper-en_US-ryan-medium/ (setup.sh downloads the same files).
"""
import os
import threading
import time
from pathlib import Path

import pytest

MODELS = Path(os.environ.get("ARGUS_TEST_MODELS", "/nonexistent"))
pytestmark = pytest.mark.skipif(not (MODELS / "silero_vad.onnx").exists(), reason="speech models not available")


@pytest.fixture(scope="module")
def tts():
    from argus.voice import PiperVoice
    return PiperVoice(str(MODELS / "vits-piper-en_US-ryan-medium"), deterministic=True)


@pytest.fixture(scope="module")
def stt():
    from argus.hearing import SherpaTranscriber
    return SherpaTranscriber(str(MODELS / "sherpa-onnx-moonshine-base-en-int8"))


def wav_for(tts, tmp_path, i, text):
    from argus.hearing import resample, write_wav
    samples, rate = tts.synthesize(text)
    path = tmp_path / f"{i:02d}.wav"
    write_wav(path, resample(samples, rate))
    return str(path)


def test_transcribe_and_detect_name(tts, stt, tmp_path):
    from argus.hearing import read_wav
    from argus.wake import NameMatcher
    m = NameMatcher()
    for i, (text, addressed) in enumerate([("Argus, what's the top news today?", True),
                                           ("Can you open Safari for me, Argus?", True),
                                           ("I was telling my friend about the weather.", False)]):
        heard = stt.transcribe(read_wav(wav_for(tts, tmp_path, i, text)))
        assert m.hear(heard).addressed is addressed, heard


def test_voiceprint_tells_voices_apart(tmp_path):
    from argus.hearing import VoicePrint, resample
    from argus.voice import PiperVoice
    vp = VoicePrint(str(MODELS / "wespeaker_en_voxceleb_resnet34_LM.onnx"), str(tmp_path / "vp.npy"))
    ryan, amy = PiperVoice(str(MODELS / "vits-piper-en_US-ryan-medium"), deterministic=True), PiperVoice(str(MODELS / "vits-piper-en_US-amy-low"))

    def clip(voice, text):
        s, r = voice.synthesize(text)
        return resample(s, r)

    vp.enroll([clip(ryan, t) for t in ("Argus, what's the weather like today?", "Argus, open my calendar.",
                                       "Hey Argus, play some music in the kitchen.")])
    same = vp.score(clip(ryan, "Argus, remind me to call home at six."))
    other = vp.score(clip(amy, "Argus, remind me to call home at six."))
    assert same > other + 0.2, (same, other)
    assert (tmp_path / "vp.npy").stat().st_mode & 0o777 == 0o600


def test_listening_loop_end_to_end(tts, stt, tmp_path):
    from fakes import Script, make_agent, say
    from argus.approvals import ScriptedApprover
    from argus.audit import Audit
    from argus.brain import Brain, Sessions
    from argus.daemon import Argus
    from argus.hearing import AudioFile, Segmenter
    from argus.listener import Listener
    from argus.voice import RecordingVoice, Speaker
    from argus.wake import NameMatcher, words

    lines = [
        "I think we should repaint the kitchen this weekend.",   # not for Argus
        "Argus, what's the top news today?",                    # request 1
        "And what about the sports news?",                      # follow-up, no name needed
        "Argus, go to sleep.",                                  # local command
        "Argus, what's the weather like?",                      # ignored while asleep
        "Argus, wake up.",                                      # local command
    ]
    files = [wav_for(tts, tmp_path, i, t) for i, t in enumerate(lines)]
    script = Script(say("A storm closed roads in the Bay Area."), say("The Warriors won last night."))
    agent, _ = make_agent(tmp_path, script)
    voice = RecordingVoice()
    speaker = Speaker(voice)
    audit = Audit(tmp_path / "audit.jsonl")
    brain = Brain(agent, speaker, ScriptedApprover([]), audit, Sessions(agent.store))
    # play the audio in real time so the follow-up window behaves as it would live
    source = AudioFile(files, gap_seconds=1.2, on_chunk=lambda: time.sleep(512 / 16000))
    argus = Argus(source=source, segmenter=Segmenter(str(MODELS / "silero_vad.onnx")), transcriber=stt,
                  listener=Listener(NameMatcher(), followup_seconds=8), speaker=speaker, brain=brain, audit=audit)
    t = threading.Thread(target=argus.run)
    t.start()
    t.join(90)
    brain.wait(10)
    speaker.wait_idle(5)
    asked = [words(b["messages"][-1]["content"][0]["text"]) for b in script.bodies]
    assert asked == [words("Argus, what's the top news today?"), words("And what about the sports news?")]
    assert voice.spoken[:2] == ["A storm closed roads in the Bay Area.", "The Warriors won last night."]
    assert any(s.startswith("Going quiet") for s in voice.spoken) and "I'm back." in voice.spoken
    assert argus.ignored >= 2
    log = (tmp_path / "audit.jsonl").read_text()
    assert "kitchen" not in log and "weather" not in log
