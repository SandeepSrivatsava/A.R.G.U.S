"""Builds Argus from ~/.argus/config.toml and runs everything in one process: the listening loop,
the brain, Telegram, the scheduler, the iPhone gateway and the menu bar."""
from __future__ import annotations

import fcntl
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any, Optional

from . import config as config_mod
from .agent import Agent, Events
from .approvals import MacApprover
from .audit import Audit
from .brain import Brain, Sessions
from .clap import ClapDetector
from .control import ControlServer
from .daemon import Argus
from .glasses import GlassesBridge
from .guard import Guard, desktop_state
from .hearing import Microphone, Segmenter, SherpaTranscriber, VoicePrint, model_paths
from .listener import Listener
from .llm import Claude
from .news import headlines
from .phone import Gatekeeper, PhoneServer, bind_address, load_or_create_token
from .policy import Policy
from .scheduler import Scheduler, sync_default_jobs
from .store import Store
from .telegram import TelegramAPI, TelegramBot
from .toolbox import build_tools
from .voice import Speaker, make_voice
from .wake import DEFAULT_SOFT_VARIANTS, DEFAULT_VARIANTS, NameMatcher

log = logging.getLogger("argus")
IS_MAC = sys.platform == "darwin"


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


def format_headlines() -> str:
    articles, _ = headlines(top=6)
    if not articles:
        return "Morning headlines: the news feeds couldn't be reached."
    lines = [f"Morning headlines, {datetime.now().strftime('%a %d %b')}:"]
    lines += [f"• {a.title} ({a.source})" for a in articles]
    return "\n".join(lines)


class App:
    """All of Argus, wired together. `voice=False` builds everything except the microphone (tests, CLI)."""

    def __init__(self, settings: config_mod.Settings, *, voice: bool = True, secrets: Optional[dict] = None,
                 computer=None, transcriber=None, source=None, speaker_voice=None, approver=None):
        self.settings = settings
        secrets = secrets if secrets is not None else config_mod.read_secrets(settings.secrets_path)
        self.audit = Audit(settings.audit_path)
        self.store = Store(settings.db_path)
        self.llm = Claude(secrets.get("ANTHROPIC_API_KEY", ""), base_url=settings.api_base)
        self.glasses = GlassesBridge() if settings.glasses_enabled else None
        self.bot: Optional[TelegramBot] = None
        self.argus: Optional[Argus] = None
        if computer is None and IS_MAC and settings.screen_control:
            try:
                from .computer import Computer, MacBackend
                computer = Computer(MacBackend(tmp_dir=str(settings.run_dir)))
            except Exception as exc:
                log.warning("screen control unavailable: %s", exc)
        self.computer = computer
        self.policy = Policy(settings, workspace=settings.workspace, desktop_state=self._desktop_state)
        self.agent = Agent(self.llm, self.store, self.policy,
                           build_tools(mac=IS_MAC or computer is not None, glasses=settings.glasses_enabled),
                           settings=settings, audit=self.audit, computer=computer, notify=self.notify,
                           glasses=self.glasses, glasses_status=(self.glasses.describe if self.glasses else lambda: None),
                           secrets=self._secret_values(secrets))
        token = secrets.get("TELEGRAM_BOT_TOKEN", "")
        if settings.telegram_enabled and token and settings.telegram_user_id:
            self.bot = TelegramBot(TelegramAPI(token, os.environ.get("ARGUS_TELEGRAM_API") or "https://api.telegram.org"),
                                   settings.telegram_user_id, self.agent, self.store, self.audit)
        self.scheduler = Scheduler(self.store, deliver=self.deliver, run_task=self.run_task, headlines=format_headlines,
                                   tz=settings.timezone or None)
        self.phone: Optional[PhoneServer] = None
        if voice:
            self.argus = self._build_voice(transcriber, source, speaker_voice, approver)

    # ---- pieces ------------------------------------------------------------------------------------
    def _secret_values(self, secrets: dict) -> list:
        values = [v for v in secrets.values() if v]
        try:
            values.append(self.settings.phone_token_path.read_text().strip())
        except OSError:
            pass
        return values

    def _desktop_state(self) -> dict:
        state = desktop_state() if IS_MAC else {"locked": False, "front_bundle": "", "front_name": "", "visible_apps": []}
        state["screen_private"] = bool(self.argus and self.argus.screen_private)
        return state

    def _build_voice(self, transcriber, source, speaker_voice, approver) -> Argus:
        s = self.settings
        paths = model_paths(s.models_dir)
        speaker = Speaker(speaker_voice or make_voice(s.voice_engine, say_voice=s.say_voice, rate=s.rate,
                                                      piper_dir=paths["tts"], speed=s.speed, tmp_dir=str(s.run_dir)))
        listener = Listener(name_matcher(s), followup_seconds=s.followup_seconds,
                            conversation_idle_seconds=s.conversation_idle_seconds)
        brain = Brain(self.agent, speaker, approver or MacApprover(require_owner_auth=s.approval_needs_touch_id,
                                                                   audit=self.audit),
                      self.audit, Sessions(self.store, "voice", "mac", s.session_idle_minutes))
        voiceprint = None
        if s.voice_lock and s.voiceprint_path.exists():
            voiceprint = VoicePrint(paths["speaker"], str(s.voiceprint_path), s.voice_lock_threshold)
        return Argus(source=source or Microphone(s.mic_device or None),
                     segmenter=Segmenter(paths["vad"], threshold=s.vad_threshold),
                     transcriber=transcriber or SherpaTranscriber(paths["asr"]), listener=listener, speaker=speaker,
                     brain=brain, audit=self.audit, voiceprint=voiceprint, voice_lock=s.voice_lock,
                     greeting=s.greeting, clap=ClapDetector(s.clap) if s.clap != "off" else None)

    def notify(self, text: str) -> bool:
        return bool(self.bot and self.bot.notify(text))

    def deliver(self, text: str) -> None:
        """A reminder or scheduled result: spoken if you're at the Mac, and sent to your phone."""
        if self.argus is not None and self.argus.mic_wanted and not self.argus.locked:
            self.argus.speaker.say(text.replace("•", ""))
        self.notify(text)

    def run_task(self, text: str) -> str:
        session = self.store.session_for("scheduler", "tasks", 60)
        turn = self.agent.run(session_id=session, channel="scheduler", content=f"Scheduled task: {text}", events=Events())
        return turn.text.strip() or f"(The scheduled task didn't produce an answer: {turn.error or 'no reply'})"

    def ask_phone(self, content: Any) -> str:
        session = self.store.session_for("phone", "iphone", 30)
        turn = self.agent.run(session_id=session, channel="phone", content=content, events=Events())
        return turn.text.strip() or ("Sorry, " + turn.error if turn.error else "Done.")

    # ---- start / stop ------------------------------------------------------------------------------
    def start_services(self) -> None:
        s = self.settings
        sync_default_jobs(self.store, s.morning_headlines, s.morning_briefing, s.timezone or None)
        self.scheduler.start()
        if self.bot is not None:
            self.bot.start()
        if s.phone_enabled:
            host = bind_address(s)
            if host:
                token = load_or_create_token(s.phone_token_path)
                self.agent.add_secret(token)
                gate = Gatekeeper(token)
                self.phone = PhoneServer((host, s.phone_port), self.ask_phone, gate, self.audit, self.glasses)
                threading.Thread(target=self.phone.serve_forever, name="argus-phone", daemon=True).start()
                self.audit.log("phone_gateway_started", host=host, port=s.phone_port)
            else:
                log.warning("iPhone gateway is on, but Tailscale isn't connected; it stays off for now")
        self.store.prune_sessions(older_than_days=45)

    def stop(self) -> None:
        self.scheduler.stop()
        if self.bot is not None:
            self.bot.stop()
        if self.phone is not None:
            self.phone.shutdown()
        if self.argus is not None:
            self.argus.stop()
            self.argus.brain.cancel()
            self.argus.source.close()


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


ICONS = {"listening": "◉", "follow-up": "◉", "conversation": "◉ ∞", "speaking": "◉ ))", "working": "◎",
         "asleep": "◌ z", "microphone off": "◌", "paused (Mac locked)": "◌"}


def run_menubar(app: App, quit_event: threading.Event) -> None:
    import rumps
    argus, settings = app.argus, app.settings

    class ArgusMenu(rumps.App):
        def __init__(self):
            super().__init__("Argus", title="◉", quit_button=None)
            self.status_item = rumps.MenuItem("Starting…")
            self.mic_item = rumps.MenuItem("Microphone", callback=self.toggle_mic)
            self.conversation_item = rumps.MenuItem("Conversation mode (no name needed)", callback=self.toggle_conversation)
            self.sleep_item = rumps.MenuItem("Asleep (only wakes to “Argus, wake up” or a double clap)", callback=self.toggle_sleep)
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
            glasses = " · glasses on" if app.glasses and app.glasses.connected and app.glasses.app_alive() else ""
            self.status_item.title = f"Argus: {st['mode']}{glasses}" + (" · voice lock" if st["voice_lock"] else "")
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
            app.stop()
            rumps.quit_application()

    ArgusMenu().run()


def main(headless: bool = False) -> int:
    try:
        settings = config_mod.load()
    except ValueError as exc:     # a typo in config.toml: say so plainly instead of a traceback
        print(f"Argus could not start: there's a problem in its settings: {exc}", file=sys.stderr)
        return 2
    config_mod.ensure_private_dirs(settings)
    setup_logging(settings)
    lock = single_instance(settings)
    if lock is None:
        print("Argus is already running.")
        return 0
    try:
        app = App(settings)
    except Exception as exc:
        log.exception("Argus could not start")
        print(f"Argus could not start: {exc}\nRun: argusctl doctor", file=sys.stderr)
        return 2
    argus = app.argus
    quit_event = threading.Event()
    handlers = argus.handlers()
    handlers["telegram"] = lambda req: {"ok": app.notify(str(req.get("text") or ""))}
    if app.computer is not None:
        from .computer import check_permissions
        handlers["permissions"] = lambda req: {"ok": True, **check_permissions()}
    control = ControlServer(settings.socket_path, handlers)
    control.start()
    guard = Guard(settings.state_path, extra_state=argus.guard_state,
                  on_lock_change=lambda locked: settings.pause_when_locked and argus.set_locked(locked))
    guard.start()
    if app.computer is not None:
        from .computer import request_permissions
        app.audit.log("permissions", **request_permissions())
    app.start_services()
    app.audit.log("started", name=settings.name)

    def on_term(signum, frame):  # launchd / `argusctl quit`: stop listening and leave cleanly
        quit_event.set()
        app.stop()
        control.stop()
        app.audit.log("stopped", signal=signum)
        os._exit(0)

    signal.signal(signal.SIGTERM, on_term)
    worker = threading.Thread(target=argus.run, name="argus-listen", daemon=True)
    worker.start()
    try:
        if not headless:
            try:
                run_menubar(app, quit_event)
                return 0
            except ImportError:
                log.warning("rumps not installed; running without a menu bar")
        while worker.is_alive() and not quit_event.is_set():
            worker.join(1.0)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        app.stop()
        guard.stop()
        control.stop()
        app.audit.log("stopped")
