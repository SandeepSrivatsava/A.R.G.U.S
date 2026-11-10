"""End-to-end check, run by tests/e2e.sh after setup.sh has installed Argus into a throwaway home.

Builds the real Argus app from the installed settings, secrets and speech models, with three stand-ins:
the microphone plays synthesised speech (and a double clap), the speakers record what would be said,
and Claude is a local mock server. Then it talks to Argus every way Sandeep would.

Usage: python e2e_voice.py DANGER_DIR WORK_DIR
"""
import http.client
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from fakes import wait_for  # noqa: E402

from argus import config as config_mod  # noqa: E402
from argus.app import App  # noqa: E402
from argus.approvals import ScriptedApprover  # noqa: E402
from argus.hearing import AudioFile, model_paths, resample, write_wav  # noqa: E402
from argus.voice import PiperVoice, RecordingVoice  # noqa: E402

danger_dir, work = Path(sys.argv[1]), Path(sys.argv[2])
failed = 0


def check(desc, ok):
    global failed
    print(("PASS  " if ok else "FAIL  ") + desc, flush=True)
    failed += not ok


settings = config_mod.load()
paths = model_paths(settings.models_dir)

# ---- what the microphone will hear --------------------------------------------------------------------
tts = PiperVoice(paths["tts"], deterministic=True)
rng = np.random.default_rng(3)


def clip(text):
    s, r = tts.synthesize(text)
    return resample(s, r)


def clap():
    t = np.arange(int(0.08 * 16000))
    return (rng.standard_normal(len(t)) * 0.5 * np.exp(-t / 192)).astype(np.float32)


def quiet(seconds):
    return (rng.standard_normal(int(seconds * 16000)) * 0.002).astype(np.float32)


parts = [
    (clip("I think we should repaint the kitchen this weekend."), 3.0),       # not for Argus
    (clip("Argus, what's the top news today?"), 7.0),                         # answered with a tool
    (clip("Argus, please delete the old files in my workspace."), 8.0),       # held for approval, denied
    (clip("Argus, open my bank statement."), 6.0),                            # blocked site
    (np.concatenate([clap(), quiet(0.35), clap()]), 2.0),                     # clap clap: listening...
    (clip("What's the weather in Portland?"), 6.0),                            # ...so no name needed
]
files = []
for i, (audio, gap) in enumerate(parts):
    path = work / f"mic{i}.wav"
    write_wav(path, np.concatenate([quiet(0.6), audio]))
    files.append(str(path))
source = AudioFile(files, gap_seconds=[gap for _, gap in parts], on_chunk=lambda: time.sleep(512 / 16000))

voice = RecordingVoice()
approver = ScriptedApprover(["deny"])
app = App(settings, source=source, speaker_voice=voice, approver=approver)
app.start_services()
argus = app.argus
argus.greeting = ""
argus.run()
argus.brain.wait(60)
argus.speaker.wait_idle(10)

mock_log = (work / "mock.log").read_text()
requests = [json.loads(line) for line in mock_log.splitlines()]
print("spoken:", json.dumps(voice.spoken), flush=True)
check("speech without the name never reached Claude", "kitchen" not in mock_log)
check("answered the spoken question by running the news tool",
      "Top story: Storm closes Bay Area highways" in voice.spoken)
check("Claude got Argus's persona, voice rules and Sandeep's city",
      "You are Argus, the personal assistant of Sandeep" in mock_log and "Portland" in mock_log
      and "Never say your own name" in mock_log)
check("the dangerous delete was held for approval", len(approver.seen) == 1 and "rm -rf" in approver.seen[0].command)
check("denied, and nothing was deleted", (danger_dir / "keep.txt").exists())
check("Argus said what happened", "Okay, I didn't delete anything." in voice.spoken)
check("the banking site was blocked by the privacy rules", any("privacy rules" in s for s in voice.spoken))
check("double clap opened listening without the name", "weather in portland" in mock_log.lower())
check("tools offered include the screen-free set on Linux and web search",
      any(t.get("name") == "web_search" for t in requests[0]["tools"]))
audit = settings.audit_path.read_text()
check("activity log: requests, approval, block, clap, costs", all(k in audit for k in (
    '"event": "approval"', '"event": "tool_blocked"', '"event": "clap"', '"cost_usd"')))
check("activity log never has the unaddressed speech", "kitchen" not in audit)

# ---- reminders -----------------------------------------------------------------------------------------
app.store.add_task("stretch your legs", "reminder", due=time.time() - 1)
app.scheduler.tick()
check("reminder spoken at the Mac", wait_for(lambda: "Reminder: stretch your legs" in voice.spoken, 10))
app.argus.set_locked(True)                                       # you walked away and the Mac locked
app.store.add_task("call home", "reminder", due=time.time() - 1)
app.scheduler.tick()
time.sleep(0.5)
kept_quiet = "Reminder: call home" not in voice.spoken
app.argus.set_locked(False)                                      # ...and you're back
check("a reminder due while the Mac was locked is spoken when you're back",
      kept_quiet and wait_for(lambda: "Reminder: call home" in voice.spoken, 10) and "While you were away:" in voice.spoken)

# ---- iPhone gateway --------------------------------------------------------------------------------------
token = (settings.phone_token_path).read_text().strip()


def phone(path, body, tok):
    conn = http.client.HTTPConnection("127.0.0.1", settings.phone_port, timeout=60)
    conn.request("POST", path, body=json.dumps(body), headers={"Authorization": f"Bearer {tok}"})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read() or b"{}")


check("iPhone: wrong token refused", phone("/ask", {"text": "hi"}, "nope")[0] == 401)
check("iPhone: Siri-style question answered",
      phone("/ask", {"text": "What are the top headlines today?"}, token) ==
      (200, {"reply": "Top story: Storm closes Bay Area highways"}))
check("iPhone: phone token file is private", oct(settings.phone_token_path.stat().st_mode & 0o777) == "0o600")
status, body = phone("/ask", {"text": "Fastest way to the airport?", "location": {"lat": 45.52, "lon": -122.68}}, token)
check("iPhone: directions from the phone's location come back with an Apple Maps link",
      status == 200 and body.get("reply") == "Opening the route to the airport in Maps."
      and body.get("open") == "https://maps.apple.com/?daddr=Portland+Airport%2C+OR&dirflg=d")
check("iPhone: the phone's location never reached Claude or the activity log",
      "45.52" not in (work / "mock.log").read_text() and "45.52" not in settings.audit_path.read_text())

app.stop()
sys.exit(1 if failed else 0)
