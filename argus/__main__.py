"""argusctl: control Argus from the Terminal."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
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


def _send(cmd: str, timeout: float = 10.0, **fields):
    from .control import send
    settings = config_mod.load()
    try:
        return send(settings.socket_path, cmd, timeout=timeout, **fields)
    except OSError:
        from .runtime import START_HINT, kind
        print("Argus isn't running.")
        if kind() == "mac":
            from .service import why_not
            for line in why_not(settings):
                print("  " + line)
        print(f"Start it with: {START_HINT[kind()]}")
        sys.exit(1)


def cmd_status(args) -> int:
    reply = _send("status")
    if args.json or "talk_over" not in reply:      # the always-on server and Android answer differently
        print(json.dumps(reply, indent=2))
        return 0
    from .service import describe_status
    for line in describe_status(reply):
        print(line)
    return 0


def cmd_restart(args) -> int:
    from . import service
    return service.restart(config_mod.load(), force=args.force, wait=args.wait)


def cmd_quit(args) -> int:
    from . import service
    return service.stop(config_mod.load())


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
    from .wake import misheard_name
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
            word = misheard_name(prompt, text, settings.name)
            if word:
                learned.add(word)
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
              f"{settings.voice_lock_threshold}).")
        if settings.voice_lock:
            print("Voice lock is on: Argus will answer only you (and ask you about anyone else), and you can "
                  "approve things by voice.")
        else:
            print("Voice lock is off. To answer only your voice, set voice_lock = true under [listening] "
                  "in ~/.argus/config.toml.")
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


def cmd_errors(args) -> int:
    from . import diagnose
    settings = config_mod.load()
    found = diagnose.problems(diagnose.read_entries(settings.audit_path))[-args.n:]
    for line in found:
        print(line)
    if not found:
        print("No problems in the activity log.")
    if args.watch:
        print("Watching for new problems (Ctrl-C to stop)...", flush=True)
        try:
            for line in diagnose.follow(settings.audit_path):
                print(line, flush=True)
        except KeyboardInterrupt:
            pass
    else:
        print("Live view: argusctl errors --watch    A file to send for help: argusctl report")
    return 0


def argus_version() -> str:
    from .runtime import version
    return version()


def cmd_report(args) -> int:
    from . import diagnose
    settings = config_mod.load()
    text = diagnose.build_report(settings, config_mod.read_secrets(settings.secrets_path), argus_version())
    if args.show:
        print(text, end="")
        return 0
    path = diagnose.write_report(text)
    print(f"Wrote {path}")
    print("It has recent problems, timings and settings, but not what you said, and no keys. Read it, then send it.")
    return 0


def cmd_ignore_app(args, *, restart=None) -> int:
    """Argus pauses while another app holds a microphone (a call). Some apps hold it with no call (WhatsApp
    after a voice note): this keeps Argus listening for that one."""
    settings = config_mod.load()
    app = " ".join(args.app.split())
    apps = [a for a in settings.ignore_mic_apps if a.lower() != app.lower()]
    if not args.undo:
        apps.append(app)
    config_mod.set_setting(settings.home / "config.toml", "listening", "ignore_mic_apps", json.dumps(apps))
    print(f"Argus {'pauses again' if args.undo else 'keeps listening'} while {app} uses the microphone"
          + ("" if args.undo else " (also during a call in it: say \"Argus, I'm on a call\" then)") + ".")
    from . import service
    return (restart or (lambda: service.restart(config_mod.load())))()


def cmd_logs(args) -> int:
    from . import logkeeper
    settings = config_mod.load()
    keeper = logkeeper.from_settings(settings)
    if keeper is None:
        print('Keeping the logs in a folder is off. Switch it on: folder = "~/Downloads/Logs" under [logs] in '
              "~/.argus/config.toml")
        return 1
    day = keeper.sync()
    if day is None:
        print(f"Couldn't write to {keeper.folder}. If macOS asked whether Argus may use the Downloads folder, "
              "allow it (System Settings > Privacy & Security > Files and Folders).")
        return 1
    print(f"Logs are in {day} (a folder per day; days older than {keeper.keep_days} are deleted by themselves).")
    return 0


def cmd_google(args, *, ask=input, ask_secret=None, open_browser=None, request=None) -> int:
    """Connect your Google account (read-only), check it, or disconnect it."""
    import getpass
    import webbrowser
    from . import google
    settings = config_mod.load()
    path = settings.secrets_path
    saved = config_mod.read_secrets(path) if path.exists() else {}
    ask_secret = ask_secret or getpass.getpass
    open_browser = open_browser or webbrowser.open
    request = request or google.send
    client_id, client_secret, refresh = (saved.get(k, "") for k in google.SECRET_KEYS)

    if args.action == "status":
        if not refresh:
            print("Not connected. To connect: argusctl google connect   (steps in docs/GOOGLE.md)")
            return 1
        auth = google.Auth(client_id, client_secret, refresh, request=request)
        try:
            auth.token()
        except google.GoogleError as exc:
            print(f"Connected before, but it isn't working now: {exc}")
            return 1
        names = [s.rsplit("/", 1)[-1] for s in auth.scopes if s in google.SCOPES]
        print("Connected (read-only): " + (", ".join(names) or "no Gmail, Drive or Calendar permission"))
        missing = [s.rsplit("/", 1)[-1] for s in google.SCOPES if s not in auth.scopes]
        if missing:
            print("Not allowed when you signed in: " + ", ".join(missing) + ". Run argusctl google connect to add them.")
        return 0

    if args.action == "disconnect":
        if refresh:
            try:
                google.revoke(refresh, request)
                print("Google access revoked.")
            except google.GoogleError as exc:
                print(f"Couldn't reach Google to revoke it ({exc}); also remove Argus at myaccount.google.com/connections.")
        for key in google.SECRET_KEYS:
            config_mod.write_secret(path, key, "")
        print("Removed from this Mac. Run argusctl restart.")
        return 0

    print("Connecting Argus to your Google account: Gmail, Drive and Calendar, read-only.")
    print("You need a Google Cloud \"Desktop app\" client first: docs/GOOGLE.md, steps 1 to 4 (about 10 minutes).")
    if client_id and client_secret:
        print(f"Using the saved client (ending ...{client_id[-14:]}). Type new ones to replace it, or press Enter.")
    new_id = ask("  Client ID (ends with .apps.googleusercontent.com): ").strip()
    if new_id:
        if not new_id.endswith(".apps.googleusercontent.com"):
            print("That isn't a client ID (it ends with .apps.googleusercontent.com).")
            return 1
        client_id = new_id
        client_secret = ask_secret("  Client secret (hidden as you type): ").strip()
    if not client_id:
        print("A client ID is needed. See docs/GOOGLE.md.")
        return 1
    print("Your browser will open. Choose your Google account. If Google says it hasn't verified this app, that's")
    print("your own app: click Advanced, then Go to Argus. Tick all three permissions, then Continue.")
    try:
        reply = google.sign_in(client_id, client_secret, open_browser=open_browser, request=request)
    except google.GoogleError as exc:
        print(f"Didn't connect: {exc}")
        return 1
    config_mod.write_secret(path, "GOOGLE_CLIENT_ID", client_id)
    config_mod.write_secret(path, "GOOGLE_CLIENT_SECRET", client_secret)
    config_mod.write_secret(path, "GOOGLE_REFRESH_TOKEN", str(reply["refresh_token"]))
    print("Connected. Argus can now read your Gmail, Drive and Calendar (never send, delete or change).")
    if reply.get("missing"):
        print("You didn't allow: " + ", ".join(reply["missing"]) + ". Argus works without them; connect again to add.")
    print("If your Google Cloud app is still in Testing, Google ends this sign-in after 7 days: publish it (docs/GOOGLE.md")
    print("step 5) so it lasts. Now run: argusctl restart")
    return 0


def describe_timing(entry: dict, end_of_speech: float = 0.6) -> str:
    """One line per spoken request: where the seconds between your last word and Argus's first word went."""
    bits = []
    if entry.get("speech_to_text_s") is not None:
        bits.append(f"pause to notice you'd finished {end_of_speech:.1f} s")
        bits.append(f"speech to text {entry['speech_to_text_s']:.1f} s")
    firsts, totals = entry.get("claude_first_s") or [], entry.get("claude_s") or []
    for i, first in enumerate(firsts):
        total = totals[i] if i < len(totals) else first
        bits.append(f"Claude #{i + 1} {first:.1f} s to start ({total:.1f} s in all)")
    for name, took in entry.get("tools") or []:
        bits.append(f"{name} {took:.1f} s")
    first_word = float(entry.get("first_word_s") or 0)
    total = first_word + float(entry.get("speech_to_text_s") or 0) + (end_of_speech if "speech_to_text_s" in entry else 0)
    when = str(entry.get("ts", ""))[11:19]
    return f"{when}  first word {total:.1f} s after you stopped talking: " + "; ".join(bits)


KNOWN_KEYS = {
    "GOOGLE_MAPS_API_KEY": "Google Maps: routes, and reviews with Places API (New) switched on",
    "TOMTOM_API_KEY": "TomTom: traffic incidents and a third route opinion",
    "FLIGHTAWARE_API_KEY": "FlightAware AeroAPI: a flight's delays and cancellations",
    "GEMINI_API_KEY": "Gemini (free): Google search and a second opinion",
    "OPENAI_API_KEY": "ChatGPT (pay-per-use, tiny): a second opinion",
    "XAI_API_KEY": "Grok (pay-per-use): a second opinion",
    "GROQ_API_KEY": "Groq (free): another opinion in the council",
    "MISTRAL_API_KEY": "Mistral (free): another opinion in the council",
    "OPENROUTER_API_KEY": "OpenRouter (free models): another opinion in the council",
    "ANTHROPIC_API_KEY": "Claude",
}


def cmd_key(args, *, ask_secret=None) -> int:
    """Save, replace or remove one key without running setup again. The key is typed hidden, never shown."""
    import getpass
    settings = config_mod.load()
    path = settings.secrets_path
    saved = config_mod.read_secrets(path) if path.exists() else {}
    name = (args.name or "").upper()
    if not name:
        print("Keys Argus can use (argusctl key NAME to add or replace one, argusctl key NAME --remove to delete):")
        for key, what in KNOWN_KEYS.items():
            value = saved.get(key, "")
            print(f"  {key:<22} {'saved (ending ...' + value[-4:] + ')' if value else 'not set':<26} {what}")
        return 0
    if name not in KNOWN_KEYS:
        print(f"Unknown key {name}. Run argusctl key to see the names.")
        return 1
    if args.remove:
        config_mod.write_secret(path, name, "")
        print(f"Removed {name}. Run argusctl restart.")
        return 0
    value = (ask_secret or getpass.getpass)(f"{name} (hidden as you type or paste; Enter cancels): ").strip()
    if not value:
        print("Nothing changed.")
        return 1
    if any(c.isspace() for c in value) or len(value) < 8:
        print("That doesn't look like a key (too short, or it has spaces).")
        return 1
    config_mod.write_secret(path, name, value)
    print(f"Saved {name} (ending ...{value[-4:]}). Run argusctl restart.")
    return 0


def cmd_brains(args, *, ask_secret=None, request=None) -> int:
    """The other AI models Argus asks for a second opinion: list them, add or remove one, test them."""
    from . import council
    from .maps import http_json
    settings = config_mod.load()
    saved = config_mod.read_secrets(settings.secrets_path) if settings.secrets_path.exists() else {}
    request = request or http_json
    if args.action in ("add", "remove"):
        if (args.name or "").lower() == "gemini":
            return cmd_key(SimpleNamespace(name="GEMINI_API_KEY", remove=args.action == "remove"), ask_secret=ask_secret)
        found = council.preset(args.name or "")
        if found is None:
            print("Choose one of: " + ", ".join(council.PRESETS) + ", gemini")
            return 1
        if args.action == "remove":
            return cmd_key(SimpleNamespace(name=found.key, remove=True))
        print(f"{found.name}: {found.cost}. Get a key at {found.where}.")
        if found.note:
            print(f"  Also: {found.note}.")
        code = cmd_key(SimpleNamespace(name=found.key, remove=False), ask_secret=ask_secret)
        if code == 0:
            fresh = config_mod.read_secrets(settings.secrets_path)
            member = next((m for m in council.members(settings, fresh) if m.short == found.short), None)
            if member is not None:
                ok, said, seconds = council.check(member, request)
                print(f"Test: {'it answered' if ok else 'it did NOT answer'} in {seconds:.1f} s"
                      + (f" ({said})" if not ok else "") + ".")
                if not ok:
                    return 1
        return code
    team = council.members(settings, saved)
    if args.action == "test":
        if not team:
            print("No other brains yet. Add one: argusctl brains add chatgpt   (or grok, gemini, groq, mistral, openrouter)")
            return 1
        failed = 0
        with ThreadPoolExecutor(max_workers=len(team)) as pool:
            for member, (ok, said, seconds) in zip(team, pool.map(lambda m: council.check(m, request), team)):
                failed += not ok
                print(f"  {'ok  ' if ok else 'FAIL'} {member.name:<42} {seconds:4.1f} s  {said if not ok else ''}".rstrip())
        return 1 if failed else 0
    print("Claude answers; these give it a second opinion (only the question is sent, without your name, numbers or "
          "addresses):")
    on = {m.short for m in team}
    for member in team:
        cost = council.PRESETS[member.short].cost if member.short in council.PRESETS else (
            "free" if member.short == "gemini" else "your own")
        print(f"  on   {member.name:<42} {cost}")
    if "gemini" not in on:
        print(f"  off  {'Gemini (free)':<42} add: argusctl brains add gemini   (key from aistudio.google.com/apikey)")
    for short, found in council.PRESETS.items():
        if short not in on:
            print(f"  off  {found.name:<42} add: argusctl brains add {short}   ({found.cost})")
    print("Check they answer: argusctl brains test")
    return 0


VOICE_SAMPLE = "Hey! I found three great ramen spots near you. Want the closest one, or the best rated?"


def cmd_voices(args, *, make=None) -> int:
    """Play the American voices one after another (or one by name), to pick one for [voice] kokoro_voice."""
    from .hearing import model_paths
    from .voice import KOKORO_VOICES, KokoroVoice
    settings = config_mod.load()
    if getattr(args, "natural", False):
        marker = settings.run_dir / "natural-voice-too-slow"
        marker.unlink(missing_ok=True)
        print("Argus will try the natural voice again after: argusctl restart   (or in one step: argusctl voice --natural)")
        return 0
    make = make or (lambda name, speed: KokoroVoice(model_paths(str(settings.models_dir))["kokoro"], voice=name,
                                                     speed=speed))
    names = [args.name] if args.name else [n for n in KOKORO_VOICES if n.startswith("a")]   # American
    for name in names:
        if name not in KOKORO_VOICES:
            print(f"Unknown voice {name}. Choose from: {', '.join(KOKORO_VOICES)}")
            return 1
        print(f"{name}{'  (yours now)' if name == settings.kokoro_voice else ''}", flush=True)
        try:
            make(name, args.speed or settings.speed).speak(VOICE_SAMPLE)
        except Exception as exc:
            print(f"Couldn't play it: {exc}. Run ./setup.sh to download the voice.")
            return 1
    print("To switch to one, type for example: argusctl voice af_sky   (faster or slower: argusctl voice --speed 1.1)")
    return 0


VOICE_HELLO = "Hi, this is my new voice."


def cmd_voice(args, *, make=None, restart=None, ask=input, hello=None) -> int:
    """Change Argus's voice and speed in one step: the setting, a restart, and a hello in the new voice."""
    from .voice import KOKORO_VOICES, KokoroVoice, voice_label
    settings = config_mod.load()
    path = settings.home / "config.toml"
    marker = settings.run_dir / "natural-voice-too-slow"
    name = (args.name or "").strip().lower()
    if not name and args.speed is None and not args.natural:
        now = settings.kokoro_voice if settings.voice_engine == "kokoro" else {"piper": "quick", "say": "mac"}.get(
            settings.voice_engine, settings.voice_engine)
        print(f"Argus speaks with: {now} at {settings.speed:g}x" + (
            " (using the quick voice for now: the natural one was too slow on this Mac)" if marker.exists() else ""))
        for voice in KOKORO_VOICES:
            print(f"  {voice:<12} {voice_label(voice)}{'   <- now' if voice == now else ''}")
        print(f"  {'quick':<12} quicker to make, a little less natural{'   <- now' if now == 'quick' else ''}")
        print(f"  {'mac':<12} the Mac's own voice{'   <- now' if now == 'mac' else ''}")
        print("Change it:      argusctl voice af_sky        (hear one first: argusctl voice af_sky --try)\n"
              "Faster/slower:  argusctl voice --speed 1.1   (1.0 is normal)\n"
              "Hear them all:  argusctl voices")
        return 0
    changes = []
    if name in ("quick", "piper"):
        changes.append(("engine", '"piper"'))
    elif name in ("mac", "say"):
        changes.append(("engine", '"say"'))
    elif name in KOKORO_VOICES:
        changes += [("engine", '"kokoro"'), ("kokoro_voice", json.dumps(name))]
    elif name:
        print(f"There's no voice called {name}. Choose from: {', '.join(KOKORO_VOICES)}, quick, mac")
        return 1
    if args.speed is not None:
        if not 0.5 <= args.speed <= 2.0:
            print("The speed goes from 0.5 to 2.0 (1.0 is normal, 1.2 a little faster).")
            return 1
        changes.append(("speed", f"{args.speed:g}"))
    if args.try_it and name in KOKORO_VOICES:
        from .hearing import model_paths
        make = make or (lambda voice, speed: KokoroVoice(model_paths(str(settings.models_dir))["kokoro"], voice=voice,
                                                         speed=speed))
        try:
            make(name, args.speed or settings.speed).speak(VOICE_SAMPLE)
        except Exception as exc:
            print(f"Couldn't play it: {exc}. Run ./setup.sh to download the voice.")
            return 1
        if ask(f"Use {name}? [Y/n] ").strip().lower() in ("n", "no"):
            print("Nothing changed.")
            return 0
    before = path.read_text(encoding="utf-8") if path.exists() else None
    for key, value in changes:
        config_mod.set_setting(path, "voice", key, value)
    try:
        config_mod.load()
    except ValueError as exc:                      # never leave a config Argus can't start with
        if before is None:
            path.unlink()
        else:
            path.write_text(before, encoding="utf-8")
        print(f"Couldn't change it: {exc}")
        return 1
    if args.natural or name in KOKORO_VOICES:      # you asked for a natural voice: try it again on this Mac
        marker.unlink(missing_ok=True)
    fresh = config_mod.load()
    print(f"Voice: {fresh.kokoro_voice if fresh.voice_engine == 'kokoro' else name or fresh.voice_engine} "
          f"at {fresh.speed:g}x.")
    if args.no_restart:
        print("It's used from the next start: argusctl restart")
        return 0
    from . import service
    code = (restart or (lambda: service.restart(fresh)))()
    if code == 0:
        try:
            (hello or (lambda: _send_quiet("say", text=VOICE_HELLO)))()
        except Exception:
            pass
    return code


def cmd_timing(args) -> int:
    from .audit import Audit
    settings = config_mod.load()
    entries = [e for e in Audit(settings.audit_path).tail(500) if e.get("event") == "timing"][-args.n:]
    if not entries:
        print("No timed answers yet. Ask Argus something out loud, then run: argusctl timing")
        return 0
    for entry in entries:
        print(describe_timing(entry, settings.end_of_speech_seconds))
    print("Claude's numbers include thinking and the network; tools are the lookups (weather, maps...).")
    return 0


def cmd_doctor(args) -> int:
    from pathlib import Path
    from .llm import Claude, LLMError
    from .maps import Router
    from .runtime import START_HINT, kind
    settings = config_mod.load()
    ok = True
    on_mac = kind() == "mac"

    def line(passed: bool, text: str):
        nonlocal ok
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {text}")

    print(f"Phone: {'iPhone' if settings.phone_type == 'iphone' else 'Android'} "
          f"(navigation opens in {'Apple' if settings.maps_link_app == 'apple' else 'Google'} Maps)")
    if on_mac:
        from .hearing import model_paths
        paths = model_paths(settings.models_dir)
        line(Path(paths["vad"]).exists(), "voice activity model")
        line(Path(paths["asr"]).is_dir(), "speech recognition model")
        line(settings.voice_engine != "piper" or Path(paths["tts"]).is_dir(), "voice model")
    try:
        secrets = config_mod.read_secrets(settings.secrets_path)
        line(bool(secrets.get("ANTHROPIC_API_KEY")), "Claude API key saved (private file)")
    except PermissionError as exc:
        line(False, str(exc))
        secrets = {}
    print(f"INFO  directions: {Router(settings, secrets).describe()}")
    if settings.push_enabled:
        line(bool(secrets.get("NTFY_TOPIC")), "push notifications set up" if secrets.get("NTFY_TOPIC")
             else "push notifications are on, but there's no NTFY_TOPIC in the secrets file")
    if secrets.get("ANTHROPIC_API_KEY") and not args.offline:
        try:
            Claude(secrets["ANTHROPIC_API_KEY"], base_url=settings.api_base, max_retries=1).create(
                {"model": settings.small_model, "max_tokens": 5, "messages": [{"role": "user", "content": "ping"}]})
            line(True, f"Claude API answers ({settings.model} for conversations)")
        except LLMError as exc:
            line(False, f"Claude API: {exc}")
    for module, what in (("sounddevice", "microphone library"), ("rumps", "menu bar library"),
                         ("LocalAuthentication", "Touch ID library"), ("Quartz", "screen and lock detection"),
                         ("ApplicationServices", "accessibility check"), ("truststore", "TLS trust store")):
        if not on_mac:
            break
        try:
            __import__(module)
            line(True, what)
        except Exception as exc:
            line(False, f"{what} ({exc.__class__.__name__})")
    if settings.calls_enabled:
        from .calls import MacCallLine
        problems = MacCallLine(input_device=settings.call_input_device, tap=settings.call_tap).check()
        line(not problems, "phone calls ready" if not problems else "phone calls: " + "; ".join(problems))
    try:
        status = _send_quiet("status")
        line(True, f"Argus running: {status.get('mode')}")
    except OSError:
        line(False, "Argus app isn't running (argusctl restart)" if on_mac else
             f"Argus isn't running ({START_HINT[kind()]})")
        return 0 if ok else 1
    if status.get("mode") in ("server", "android"):
        return 0 if ok else 1
    if settings.voice_lock:
        line(bool(status.get("voice_lock")), "voice lock is on" if status.get("voice_lock") else
             "voice lock is switched on, but there's no usable voiceprint: run argusctl enroll")
    if settings.screen_control:
        try:
            perms = _send_quiet("permissions")
        except OSError:
            perms = {}
        if perms.get("ok"):
            fix = "System Settings > Privacy & Security > {} > switch on Argus, then: argusctl restart"
            line(bool(perms.get("screen_recording")), "Screen Recording allowed" if perms.get("screen_recording")
                 else "Screen Recording not allowed yet: " + fix.format("Screen Recording"))
            line(bool(perms.get("accessibility")), "Accessibility allowed (clicks and typing)" if perms.get("accessibility")
                 else "Accessibility not allowed yet: " + fix.format("Accessibility"))
    return 0 if ok else 1


def cmd_lessons(args) -> int:
    from .store import Store
    store = Store(config_mod.load().db_path)
    if args.action == "clear":
        print(f"Forgot {store.clear_lessons()} lessons.")
        return 0
    if args.action == "forget":
        try:
            lesson_id = int(str(args.id or "").upper().lstrip("L"))
        except ValueError:
            print("Say which lesson, e.g.: argusctl lessons forget L3")
            return 2
        ok = store.forget_lesson(lesson_id)
        print(f"Forgot L{lesson_id}." if ok else f"There's no lesson L{lesson_id}.")
        return 0 if ok else 1
    rows = store.lessons(500)
    if not rows:
        print("No lessons yet. Argus learns them when you correct it, and from its own mistakes.")
        return 0
    for lesson_id, text, source in sorted(rows):
        print(f"L{lesson_id:<4} {'you ' if source == 'you' else 'self'}  {text}")
    return 0


def cmd_maps_token(args) -> int:
    """Make a long-lasting Apple Maps token from your Maps private key and save it (Argus then uses Apple Maps
    for live traffic, on the Mac and on the server)."""
    from .maps import make_apple_maps_token
    settings = config_mod.load()
    try:
        token = make_apple_maps_token(args.key, args.key_id, args.team_id, args.days)
    except (ValueError, OSError) as exc:
        print(f"Couldn't make the token: {exc}")
        return 1
    if args.print:
        print(token)
        return 0
    config_mod.write_secret(settings.secrets_path, "APPLE_MAPS_TOKEN", token)
    print(f"Saved an Apple Maps token that lasts {args.days} days to {settings.secrets_path}.")
    print("Restart Argus to use it" + (": argusctl restart" if sys.platform == "darwin" else
                                        " (server: sudo systemctl restart argus)."))
    return 0


def cmd_push(args) -> int:
    """Phone notifications for reminders while you're away (ntfy): on, off, or a test message."""
    from .push import TOPIC, check_server, new_topic, push_from
    settings = config_mod.load()
    path = config_mod.argus_home() / "config.toml"
    if args.action == "off":
        config_mod.set_setting(path, "push", "enabled", "false")
        print("Push notifications are off." + (" Restart Argus: argusctl restart" if sys.platform == "darwin" else ""))
        return 0
    secrets = config_mod.read_secrets(settings.secrets_path)
    if args.action == "test":
        push = push_from(settings, secrets)
        if push is None:
            print("Push notifications aren't on. Switch them on with: argusctl push on")
            return 1
        ok = push.send("Test from Argus: notifications work.")
        print("Sent. It should be on your phone now." if ok else f"Sending failed; check {settings.push_server}.")
        return 0 if ok else 1
    topic = args.topic or secrets.get("NTFY_TOPIC") or new_topic()
    if not TOPIC.fullmatch(topic):
        print("The topic must be 20 to 64 letters, digits, - or _ (long and random, like a password).")
        return 2
    server = args.server or settings.push_server
    problem = check_server(server)
    if problem:
        print(f"Can't use {server}: {problem}")
        return 2
    config_mod.write_secret(settings.secrets_path, "NTFY_TOPIC", topic)
    path.parent.mkdir(parents=True, exist_ok=True)
    config_mod.set_setting(path, "push", "enabled", "true")
    config_mod.set_setting(path, "push", "server", json.dumps(server))
    print("Push notifications are on. On your phone, install the ntfy app and subscribe to:")
    print(f"  server {server}\n  topic  {topic}")
    print("Keep the topic private: anyone who knows it can read your reminders.")
    print("Then: " + ("argusctl restart, and " if sys.platform == "darwin" else "") + "argusctl push test")
    return 0


def cmd_usage(args) -> int:
    """Claude API spend from the activity log (estimates from token counts)."""
    import time
    from .audit import Audit
    settings = config_mod.load()
    entries = [e for e in Audit(settings.audit_path).tail(100_000) if e.get("cost_usd")]
    today, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")

    def spent(prefix):
        return sum(float(e["cost_usd"]) for e in entries if str(e.get("ts", "")).startswith(prefix))

    requests = sum(1 for e in entries if e.get("event") == "turn" and str(e.get("ts", "")).startswith(today))
    searches = sum(int(e.get("web_searches") or 0) for e in entries if str(e.get("ts", "")).startswith(month))
    print(f"Today: ${spent(today):.2f} over {requests} requests")
    print(f"This month: ${spent(month):.2f} (including {searches} web searches)")
    print("(Estimates from token counts; the Claude console shows the exact bill.)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["android"]:                 # Argus on an Android phone (in Termux)
        from .android_app import main as android_main
        return android_main(argv[1:])
    p = argparse.ArgumentParser(prog="argusctl", description="Control Argus, your voice assistant.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="what Argus is doing, in plain words (--json for all of it)")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("restart", help="restart Argus and check that it really came back (--force: stop every copy first)")
    s.add_argument("--force", action="store_true", help="stop every copy of Argus, then start it fresh")
    s.add_argument("--wait", type=float, default=90.0, help="seconds to wait for it to answer (default 90)")
    sub.add_parser("quit", help="stop Argus until argusctl restart (or the next login)")
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
    s = sub.add_parser("errors", help="recent problems in plain words (--watch shows new ones as they happen)")
    s.add_argument("n", nargs="?", type=int, default=15)
    s.add_argument("--watch", action="store_true")
    s = sub.add_parser("ignore-app", help="keep listening while this app holds the microphone (e.g. WhatsApp)")
    s.add_argument("app")
    s.add_argument("--undo", action="store_true", help="pause for it again")
    sub.add_parser("logs", help="copy the logs to the logs folder now (~/Downloads/Logs) and show where it is")
    s = sub.add_parser("report", help="write a problem report to the Desktop that is safe to share")
    s.add_argument("--show", action="store_true", help="print it instead of writing a file")
    s = sub.add_parser("google", help="connect your Google account, read-only (Gmail, Drive, Calendar)")
    s.add_argument("action", nargs="?", choices=["connect", "status", "disconnect"], default="status")
    s = sub.add_parser("voice", help="see or change Argus's voice and speed (it restarts and says hello in it)")
    s.add_argument("name", nargs="?", help="e.g. af_sky, am_michael, quick, mac")
    s.add_argument("--speed", type=float, default=None, help="1.0 is normal, 1.2 a little faster")
    s.add_argument("--try", dest="try_it", action="store_true", help="hear it first, then choose")
    s.add_argument("--natural", action="store_true", help="go back to the natural voice after it switched to the quick one")
    s.add_argument("--no-restart", action="store_true", help="change the setting only")
    s = sub.add_parser("voices", help="hear the American voices, to pick one")
    s.add_argument("name", nargs="?")
    s.add_argument("--speed", type=float, default=None)
    s.add_argument("--natural", action="store_true", help="try the natural voice again after Argus switched to the quick one")
    s = sub.add_parser("key", help="list the keys, or add / replace / remove one (typed hidden)")
    s.add_argument("name", nargs="?")
    s.add_argument("--remove", action="store_true")
    s = sub.add_parser("brains", help="other AI models (ChatGPT, Grok, Gemini, free ones) Argus asks for a second opinion")
    s.add_argument("action", nargs="?", choices=["list", "add", "remove", "test"], default="list")
    s.add_argument("name", nargs="?")
    s = sub.add_parser("timing", help="how long recent spoken answers took, step by step")
    s.add_argument("n", nargs="?", type=int, default=10)
    s = sub.add_parser("enroll", help="teach Argus your voice (voice lock) and how you say its name")
    s.add_argument("--no-voiceprint", action="store_true", help="only learn how you say the name")
    s = sub.add_parser("transcribe", help="test speech recognition on a WAV file")
    s.add_argument("file")
    s = sub.add_parser("doctor", help="check that everything Argus needs is in place")
    s.add_argument("--offline", action="store_true", help="skip the check that calls the Claude API")
    sub.add_parser("usage", help="estimated Claude API spend today and this month")
    s = sub.add_parser("lessons", help="what Argus has learned from your corrections and its own mistakes")
    s.add_argument("action", nargs="?", choices=["list", "forget", "clear"], default="list")
    s.add_argument("id", nargs="?", help="lesson number to forget, e.g. 3 or L3")
    sub.add_parser("phone-token", help="show the token for the phone shortcuts and app")
    s = sub.add_parser("maps-token", help="make a long-lasting Apple Maps token from your Maps private key (.p8)")
    s.add_argument("--key", required=True, help="the AuthKey_XXXXXXXXXX.p8 file from your Apple Developer account")
    s.add_argument("--key-id", required=True, help="the key's 10-character Key ID")
    s.add_argument("--team-id", required=True, help="your 10-character Team ID (Membership details)")
    s.add_argument("--days", type=int, default=365, help="how long it lasts (default 365)")
    s.add_argument("--print", action="store_true", help="print the token instead of saving it")
    s = sub.add_parser("push", help="reminders on your phone while you're away (ntfy notifications)")
    s.add_argument("action", choices=["on", "off", "test"])
    s.add_argument("--topic", help="use this topic (e.g. the one your always-on server uses)")
    s.add_argument("--server", help="your own ntfy server (default https://ntfy.sh)")
    s = sub.add_parser("run", help="run Argus in the foreground")
    s.add_argument("--headless", action="store_true", help="no menu bar")
    sub.add_parser("server", help="run Argus in server mode (an always-on Linux machine; systemd starts this)")
    args = p.parse_args(argv)
    try:
        config_mod.load()
    except ValueError as exc:
        print(f"There's a problem in Argus's settings: {exc}")
        return 2

    if args.cmd == "run":
        from .app import main as app_main
        return app_main(headless=args.headless)
    if args.cmd == "server":
        from .server import main as server_main
        return server_main()
    if args.cmd == "maps-token":
        return cmd_maps_token(args)
    if args.cmd == "push":
        return cmd_push(args)
    if args.cmd == "usage":
        return cmd_usage(args)
    if args.cmd == "lessons":
        return cmd_lessons(args)
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
    if args.cmd == "timing":
        return cmd_timing(args)
    if args.cmd == "key":
        return cmd_key(args)
    if args.cmd == "brains":
        return cmd_brains(args)
    if args.cmd == "voices":
        return cmd_voices(args)
    if args.cmd == "voice":
        return cmd_voice(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "restart":
        return cmd_restart(args)
    if args.cmd == "quit":
        return cmd_quit(args)
    if args.cmd == "google":
        return cmd_google(args)
    if args.cmd == "errors":
        return cmd_errors(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "logs":
        return cmd_logs(args)
    if args.cmd == "ignore-app":
        return cmd_ignore_app(args)
    if args.cmd == "doctor":
        return cmd_doctor(args)

    simple = {
        "say": lambda: _send("say", text=" ".join(args.text)),
        "ask": lambda: _send("ask", timeout=300, text=" ".join(args.text)),
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
