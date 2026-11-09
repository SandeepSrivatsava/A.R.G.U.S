"""argusctl: control Argus from the Terminal."""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List, Optional

from . import config as config_mod

ENROLL_SENTENCES = [
    "Argus, what's the weather like today?",
    "Argus, open my calendar and check tomorrow.",
    "Hey Argus, play some music in the kitchen.",
    "Argus, remind me to call home at six.",
    "Argus, what's on my screen right now?",
]


def _send(cmd: str, **fields):
    from .control import send
    settings = config_mod.load()
    try:
        return send(settings.socket_path, cmd, **fields)
    except OSError:
        print("Argus isn't running. Start it with: open -a Argus   (or: argusctl run)")
        sys.exit(1)


def _record(prompts: List[str]):
    """Record one utterance per prompt from the default microphone."""
    from .hearing import Microphone, Segmenter, model_paths
    settings = config_mod.load()
    mic = Microphone(settings.mic_device or None)
    seg = Segmenter(model_paths(settings.models_dir)["vad"], threshold=settings.vad_threshold)
    clips = []
    mic.open()
    try:
        for i, prompt in enumerate(prompts, 1):
            print(f"\n[{i}/{len(prompts)}] Say: “{prompt}”")
            seg.reset()
            deadline = time.monotonic() + 15
            got = None
            while got is None and time.monotonic() < deadline:
                chunk = mic.read()
                if chunk is not None:
                    found = seg.feed(chunk)
                    got = max(found, key=len) if found else None
            if got is None:
                print("   didn't hear anything; skipping")
                continue
            clips.append((prompt, got))
            print(f"   got {len(got) / 16000:.1f}s of speech")
    finally:
        mic.close()
    return clips


def cmd_enroll(args) -> int:
    from .app import name_matcher
    from .hearing import SherpaTranscriber, VoicePrint, model_paths
    from .wake import NOT_THE_NAME, words
    settings = config_mod.load()
    paths = model_paths(settings.models_dir)
    running = True
    try:
        _send_quiet("sleep", on=True)
    except Exception:
        running = False
    print("Recording a few sentences so Argus learns your voice and how you say its name.")
    print("Speak normally, from where you usually sit.")
    clips = _record(ENROLL_SENTENCES)
    if running:
        _send_quiet("sleep", on=False)
    if len(clips) < 3:
        print("\nNot enough speech recorded. Check the microphone and try again.")
        return 1
    stt = SherpaTranscriber(paths["asr"])
    matcher = name_matcher(settings)
    learned = set()
    heard_ok = 0
    print("\nWhat the speech recogniser heard:")
    for prompt, clip in clips:
        text = stt.transcribe(clip)
        ok = matcher.hear(text).addressed
        heard_ok += ok
        print(f"   {'✓' if ok else '✗'} {text!r}")
        if not ok:
            spoken = words(prompt)
            got = words(text)
            idx = spoken.index(settings.name.lower()) if settings.name.lower() in spoken else -1
            if 0 <= idx < len(got) and got[idx] not in NOT_THE_NAME and len(got[idx]) >= 3:
                learned.add(got[idx])
    if learned:
        path = settings.learned_variants_path
        existing = set(path.read_text().split()) if path.exists() else set()
        path.write_text("\n".join(sorted(existing | learned)) + "\n")
        print(f"\nLearned how you say the name: {', '.join(sorted(learned))}")
    print(f"\nName recognised in {heard_ok} of {len(clips)} sentences.")
    if not args.no_voiceprint:
        vp = VoicePrint(paths["speaker"], str(settings.voiceprint_path), settings.voice_lock_threshold)
        vp.enroll([c for _, c in clips])
        scores = [vp.score(c) for _, c in clips]
        print(f"Voiceprint saved (self-match {min(scores):.2f}-{max(scores):.2f}; threshold "
              f"{settings.voice_lock_threshold}). Turn on voice lock in ~/.argus/config.toml:"
              f"  [listening] voice_lock = true")
    print("Restart Argus to use this: argusctl restart")
    return 0


def _send_quiet(cmd: str, **fields):
    from .control import send
    return send(config_mod.load().socket_path, cmd, **fields)


def cmd_transcribe(args) -> int:
    from .app import name_matcher
    from .hearing import Segmenter, SherpaTranscriber, model_paths, read_wav
    settings = config_mod.load()
    paths = model_paths(settings.models_dir)
    stt = SherpaTranscriber(paths["asr"])
    seg = Segmenter(paths["vad"], threshold=settings.vad_threshold)
    matcher = name_matcher(settings)
    audio = read_wav(args.file)
    pieces = seg.feed(audio) + seg.flush()
    for piece in pieces:
        text = stt.transcribe(piece)
        heard = matcher.hear(text)
        print(json.dumps({"seconds": round(len(piece) / 16000, 2), "text": text, "addressed": heard.addressed}))
    return 0


def cmd_log(args) -> int:
    from .audit import Audit
    for entry in Audit(config_mod.load().audit_path).tail(args.n):
        print(json.dumps(entry, ensure_ascii=False))
    return 0


def cmd_doctor(args) -> int:
    from .hearing import model_paths
    from .hermes import Hermes
    settings = config_mod.load()
    ok = True

    def line(passed: bool, text: str):
        nonlocal ok
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {text}")

    paths = model_paths(settings.models_dir)
    from pathlib import Path
    line(Path(paths["vad"]).exists(), "voice activity model")
    line(Path(paths["asr"]).is_dir(), "speech recognition model")
    line(settings.voice_engine != "piper" or Path(paths["tts"]).is_dir(), "voice model")
    try:
        hermes = Hermes.from_env(settings.hermes_env_file)
        line(hermes.healthy(), "Hermes API answering on 127.0.0.1")
    except ValueError as exc:
        line(False, f"Hermes API settings ({exc})")
    for module, what in (("sounddevice", "microphone library"), ("rumps", "menu bar library"),
                         ("LocalAuthentication", "Touch ID library"), ("Quartz", "screen-lock detection")):
        try:
            __import__(module)
            line(True, what)
        except Exception as exc:
            line(False, f"{what} ({exc.__class__.__name__})")
    try:
        status = _send_quiet("status")
        line(True, f"Argus running: {status.get('mode')}")
    except OSError:
        line(False, "Argus app isn't running (open -a Argus)")
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="argusctl", description="Control Argus, your voice assistant.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="what Argus is doing")
    s = sub.add_parser("say", help="speak text out loud")
    s.add_argument("text", nargs="+")
    s = sub.add_parser("ask", help="ask something as if you had said it")
    s.add_argument("text", nargs="+")
    for name, help_text in (("mic", "turn the microphone on or off"), ("conversation", "conversation mode on/off")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("state", choices=["on", "off"])
    sub.add_parser("sleep", help="stop sending anything until 'Argus, wake up'")
    sub.add_parser("wake", help="leave sleep mode")
    s = sub.add_parser("screen", help="allow or block Argus seeing and using the screen")
    s.add_argument("state", choices=["allow", "block"])
    sub.add_parser("stop", help="stop talking and cancel the current task")
    sub.add_parser("new", help="start a new conversation")
    s = sub.add_parser("log", help="recent activity")
    s.add_argument("n", nargs="?", type=int, default=20)
    s = sub.add_parser("enroll", help="teach Argus your voice (voice lock) and how you say its name")
    s.add_argument("--no-voiceprint", action="store_true", help="only learn how you say the name")
    s = sub.add_parser("transcribe", help="test speech recognition on a WAV file")
    s.add_argument("file")
    sub.add_parser("doctor", help="check that everything Argus needs is in place")
    sub.add_parser("phone-token", help="show the token for the iPhone Shortcuts")
    s = sub.add_parser("run", help="run Argus in the foreground")
    s.add_argument("--headless", action="store_true", help="no menu bar")
    sub.add_parser("phone", help="run the iPhone gateway in the foreground")
    sub.add_parser("hook", help="(used by Hermes) privacy check for one tool call")
    args = p.parse_args(argv)

    if args.cmd == "hook":
        from .hook import main as hook_main
        return hook_main()
    if args.cmd == "run":
        from .app import main as app_main
        return app_main(headless=args.headless)
    if args.cmd == "phone":
        from .phone import serve
        return serve()
    if args.cmd == "phone-token":
        from .phone import load_or_create_token
        print(load_or_create_token(config_mod.load().phone_token_path))
        return 0
    if args.cmd == "enroll":
        return cmd_enroll(args)
    if args.cmd == "transcribe":
        return cmd_transcribe(args)
    if args.cmd == "log":
        return cmd_log(args)
    if args.cmd == "doctor":
        return cmd_doctor(args)

    simple = {
        "status": lambda: _send("status"),
        "say": lambda: _send("say", text=" ".join(args.text)),
        "ask": lambda: _send("ask", text=" ".join(args.text)),
        "mic": lambda: _send("mic", on=args.state == "on"),
        "conversation": lambda: _send("conversation", on=args.state == "on"),
        "sleep": lambda: _send("sleep", on=True),
        "wake": lambda: _send("sleep", on=False),
        "screen": lambda: _send("screen", allow=args.state == "allow"),
        "stop": lambda: _send("stop"),
        "new": lambda: _send("new"),
    }
    print(json.dumps(simple[args.cmd](), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
