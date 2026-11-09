"""Builds Argus from ~/.argus/config.toml and runs it (menu bar app on macOS)."""
from __future__ import annotations

import fcntl
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
from . import config as config_mod
from .approvals import MacApprover
from .audit import Audit
from .brain import VOICE_RULES, Brain, Sessions
from .control import ControlServer
from .daemon import Argus
from .guard import Guard
from .hearing import Microphone, Segmenter, SherpaTranscriber, VoicePrint, model_paths
from .hermes import Hermes
from .listener import Listener
from .voice import Speaker, make_voice
from .wake import DEFAULT_SOFT_VARIANTS, DEFAULT_VARIANTS, NameMatcher

log = logging.getLogger("argus")


def setup_logging(settings: config_mod.Settings, verbose: bool = False) -> None:
    path = settings.audit_path.parent / "argus.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    if verbose or sys.stderr.isatty():
        root.addHandler(logging.StreamHandler())
    root.setLevel(logging.INFO)
    os.chmod(path, 0o600)


def name_matcher(settings: config_mod.Settings) -> NameMatcher:
    name = settings.name.strip().lower()
    is_default = name == "argus"
    variants = (DEFAULT_VARIANTS if is_default else ()) + tuple(settings.all_variants())
    soft = [v for v in settings.soft_variants if is_default or v not in DEFAULT_SOFT_VARIANTS]
    return NameMatcher(name, variants=variants, soft_variants=soft)


def build(settings: config_mod.Settings) -> Argus:
    paths = model_paths(settings.models_dir)
    audit = Audit(settings.audit_path)
    speaker = Speaker(make_voice(settings.voice_engine, say_voice=settings.say_voice, rate=settings.rate,
                                 piper_dir=paths["tts"], speed=settings.speed, tmp_dir=str(settings.run_dir)))
    listener = Listener(name_matcher(settings), followup_seconds=settings.followup_seconds,
                        conversation_idle_seconds=settings.conversation_idle_seconds)
    brain = Brain(Hermes.from_env(settings.hermes_env_file), speaker,
                  MacApprover(require_owner_auth=settings.approval_needs_touch_id, audit=audit), audit,
                  instructions=VOICE_RULES.format(user=settings.user_name or "the user"),
                  sessions=Sessions(settings.session_idle_minutes))
    voiceprint = None
    if settings.voice_lock and settings.voiceprint_path.exists():
        voiceprint = VoicePrint(paths["speaker"], str(settings.voiceprint_path), settings.voice_lock_threshold)
    return Argus(source=Microphone(settings.mic_device or None),
                 segmenter=Segmenter(paths["vad"], threshold=settings.vad_threshold),
                 transcriber=SherpaTranscriber(paths["asr"]), listener=listener, speaker=speaker, brain=brain,
                 audit=audit, voiceprint=voiceprint, voice_lock=settings.voice_lock, greeting=settings.greeting)


ICONS = {"listening": "◉", "follow-up": "◉", "conversation": "◉ ∞", "speaking": "◉ ))", "working": "◎",
         "asleep": "◌ z", "microphone off": "◌", "paused (Mac locked)": "◌"}


def run_menubar(argus: Argus, settings: config_mod.Settings, quit_event: threading.Event) -> None:
    import rumps

    class ArgusMenu(rumps.App):
        def __init__(self):
            super().__init__("Argus", title="◉", quit_button=None)
            self.status_item = rumps.MenuItem("Starting…")
            self.mic_item = rumps.MenuItem("Microphone", callback=self.toggle_mic)
            self.conversation_item = rumps.MenuItem("Conversation mode (no name needed)", callback=self.toggle_conversation)
            self.sleep_item = rumps.MenuItem("Asleep (only wakes to “Argus, wake up”)", callback=self.toggle_sleep)
            self.screen_item = rumps.MenuItem("Allow Argus to see and use the screen", callback=self.toggle_screen)
            self.menu = [self.status_item, None, self.mic_item, self.conversation_item, self.sleep_item,
                         self.screen_item, None,
                         rumps.MenuItem("Stop talking / cancel task", callback=lambda _: argus.brain.cancel()),
                         rumps.MenuItem("New conversation", callback=lambda _: argus.brain.sessions.rotate()),
                         rumps.MenuItem("Open activity log", callback=self.open_log), None,
                         rumps.MenuItem("Quit Argus", callback=self.quit)]

        @rumps.timer(1)
        def refresh(self, _):
            st = argus.status()
            self.title = ICONS.get(st["mode"], "◉")
            self.status_item.title = f"Argus: {st['mode']}" + (" · voice lock on" if st["voice_lock"] else "")
            self.mic_item.state = int(argus.mic_wanted)
            self.conversation_item.state = int(st["conversation"])
            self.sleep_item.state = int(st["asleep"])
            self.screen_item.state = int(not st["screen_private"])

        def toggle_mic(self, _):
            argus.set_mic(not argus.mic_wanted)

        def toggle_conversation(self, _):
            argus.listener.set_conversation(not argus.listener.conversation)

        def toggle_sleep(self, _):
            argus.listener.set_sleeping(not argus.listener.sleeping)

        def toggle_screen(self, _):
            argus.set_screen_private(not argus.screen_private)

        def open_log(self, _):
            subprocess.run(["open", "-a", "Console", str(settings.audit_path)], check=False)

        def quit(self, _):
            quit_event.set()
            argus.stop()
            rumps.quit_application()

    ArgusMenu().run()


def single_instance(settings: config_mod.Settings):
    """An exclusive lock so a second copy (e.g. opened from Finder) exits instead of double-answering."""
    path = settings.run_dir / "argus.lock"
    fh = open(path, "w")
    os.chmod(path, 0o600)
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main(headless: bool = False) -> int:
    settings = config_mod.load()
    config_mod.ensure_private_dirs(settings)
    setup_logging(settings)
    lock = single_instance(settings)
    if lock is None:
        print("Argus is already running.")
        return 0
    try:
        argus = build(settings)
    except Exception as exc:
        log.exception("Argus could not start")
        print(f"Argus could not start: {exc}\nRun: argusctl doctor", file=sys.stderr)
        return 2
    quit_event = threading.Event()
    control = ControlServer(settings.socket_path, argus.handlers())
    control.start()
    guard = Guard(settings.state_path, on_lock_change=lambda locked: settings.pause_when_locked and argus.set_locked(locked),
                  extra_state=argus.guard_state)
    guard.start()
    argus.audit.log("started", name=settings.name)

    def on_term(signum, frame):  # launchd / `argusctl quit`: stop listening and leave cleanly
        quit_event.set()
        argus.stop()
        argus.brain.cancel()
        argus.source.close()
        control.stop()
        argus.audit.log("stopped", signal=signum)
        os._exit(0)

    signal.signal(signal.SIGTERM, on_term)
    worker = threading.Thread(target=argus.run, name="argus-listen", daemon=True)
    worker.start()
    try:
        if not headless:
            try:
                run_menubar(argus, settings, quit_event)
                return 0
            except ImportError:
                log.warning("rumps not installed; running without a menu bar")
        while worker.is_alive() and not quit_event.is_set():
            worker.join(1.0)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        argus.stop()
        guard.stop()
        control.stop()
        argus.source.close()
        argus.audit.log("stopped")
