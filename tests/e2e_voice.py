"""Voice end-to-end check, run by tests/e2e_hermes.sh after setup.sh and a real Hermes gateway are up.

Spoken sentences (synthesised with the installed Piper voice) go through the installed VAD and
speech models into the real Argus listening loop, which talks to the real Hermes Runs API (backed by
tests/mock_llm.py). Checks: unaddressed speech never leaves the Mac; a question gets a spoken answer;
a dangerous command is held for approval and denied; a blocked website is refused by the privacy hook.

Usage: python e2e_voice.py HERMES_ENV_FILE DANGER_DIR WORK_DIR
"""
import json
import sys
import threading
import time
from pathlib import Path

from argus import config as config_mod
from argus.app import name_matcher
from argus.approvals import ScriptedApprover
from argus.audit import Audit
from argus.brain import VOICE_RULES, Brain, Sessions
from argus.daemon import Argus
from argus.hearing import AudioFile, Segmenter, SherpaTranscriber, model_paths, resample, write_wav
from argus.hermes import Hermes
from argus.listener import Listener
from argus.voice import PiperVoice, RecordingVoice, Speaker

env_file, danger_dir, work = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
settings = config_mod.load()
paths = model_paths(settings.models_dir)
failed = 0


def check(desc, ok):
    global failed
    print(("PASS  " if ok else "FAIL  ") + desc)
    failed += not ok


lines = [
    ("I think we should repaint the kitchen this weekend.", 3.0),
    ("Argus, what's the top news today?", 7.0),
    ("Argus, please delete the old files in my workspace.", 9.0),
    ("Argus, open my bank statement.", 7.0),
]
tts = PiperVoice(paths["tts"], deterministic=True)
files = []
for i, (text, _) in enumerate(lines):
    samples, rate = tts.synthesize(text)
    path = work / f"voice{i}.wav"
    write_wav(path, resample(samples, rate))
    files.append(str(path))

# Each sentence is followed by enough silence for its request to finish; played in real time.
source = AudioFile(files, gap_seconds=[gap for _, gap in lines], on_chunk=lambda: time.sleep(512 / 16000))

voice = RecordingVoice()
speaker = Speaker(voice)
audit = Audit(settings.audit_path)
approver = ScriptedApprover(["deny"])
brain = Brain(Hermes.from_env(env_file), speaker, approver, audit,
              instructions=VOICE_RULES.format(user="Sandeep"), sessions=Sessions(30))
argus = Argus(source=source, segmenter=Segmenter(paths["vad"]), transcriber=SherpaTranscriber(paths["asr"]),
              listener=Listener(name_matcher(settings)), speaker=speaker, brain=brain, audit=audit)
runner = threading.Thread(target=argus.run)
runner.start()
runner.join(180)
brain.wait(60)
speaker.wait_idle(10)

print("spoken:", json.dumps(voice.spoken))
mock_log = (work / "mock.log").read_text()
check("speech without the name never reached the model", "kitchen" not in mock_log)
check("answered the spoken question with the news script's top story",
      "Top story: Storm closes Bay Area highways" in voice.spoken)
check("the voice rules reached the model", "Never say your own name" in mock_log)
check("dangerous command was held for approval", len(approver.seen) == 1 and "rm -rf" in approver.seen[0].command)
check("approval was denied and nothing was deleted", danger_dir.is_dir() and (danger_dir / "keep.txt").exists())
check("Argus said what happened", "Okay, I didn't delete anything." in voice.spoken)
if '"web_extract"' in mock_log:
    check("privacy hook blocked the banking site", any("privacy rules" in s for s in voice.spoken))
    entries = [json.loads(l) for l in settings.audit_path.read_text().splitlines()]
    check("the block is in the activity log",
          any(e.get("event") == "tool_blocked" and e.get("tool") == "web_extract" for e in entries))
else:
    print("SKIP  privacy hook check (web_extract isn't offered by this Hermes build)")
log_text = settings.audit_path.read_text()
check("activity log has requests but no unaddressed speech", "top news" in log_text and "kitchen" not in log_text)
sys.exit(1 if failed else 0)
