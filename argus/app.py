"""Builds Argus from ~/.argus/config.toml and runs everything in one process: the listening loop,
the brain, the scheduler, the iPhone gateway and the menu bar."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from typing import Any, Optional

from . import config as config_mod
from .agent import Agent, Events, request_cost
from .approvals import MacApprover, VoiceGate, VoiceOrScreenApprover
from .audit import Audit
from .brain import Brain, Sessions
from .clap import ClapDetector
from .control import ControlServer
from .daemon import Argus
from .glasses import GlassesBridge
from .guard import Guard, desktop_state, element_at
from .lessons import Reflector
from .hearing import Microphone, Segmenter, SherpaTranscriber, VoicePrint, model_paths
from .listener import Listener
from .llm import Claude
from . import weather as weather_mod
from .maps import Router, phone_links, route_tool
from .news import format_headlines
from .phone import Gatekeeper, PhoneServer, bind_address, load_or_create_token
from .policy import Policy
from .push import push_from
from .runtime import setup_logging, single_instance
from .scheduler import Scheduler, sync_default_jobs
from .store import Store
from .toolbox import build_tools
from .voice import Speaker, make_voice
from .wake import DEFAULT_SOFT_VARIANTS, DEFAULT_VARIANTS, NameMatcher

log = logging.getLogger("argus")
IS_MAC = sys.platform == "darwin"
__all__ = ["App", "main", "name_matcher", "notify", "format_headlines", "setup_logging", "single_instance"]


def name_matcher(settings: config_mod.Settings) -> NameMatcher:
    name = settings.name.strip().lower()
    is_default = name == "argus"
    variants = (DEFAULT_VARIANTS if is_default else ()) + tuple(settings.all_variants())
    soft = [v for v in settings.soft_variants if is_default or v not in DEFAULT_SOFT_VARIANTS]
    return NameMatcher(name, variants=variants, soft_variants=soft)


def notify(text: str) -> None:
    """A quiet notification on the Mac (used while Argus is on a phone call and can't speak)."""
    if not IS_MAC:
        return
    try:
        subprocess.Popen(["osascript", "-e", "on run argv", "-e",
                          'display notification (item 1 of argv) with title "Argus"', "-e", "end run", text[:200]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


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
        self.argus: Optional[Argus] = None
        if computer is None and IS_MAC and settings.screen_control:
            try:
                from .computer import Computer, MacBackend
                computer = Computer(MacBackend(tmp_dir=str(settings.run_dir)))
            except Exception as exc:
                log.warning("screen control unavailable: %s", exc)
        self.computer = computer
        self.policy = Policy(settings, workspace=settings.workspace, desktop_state=self._desktop_state,
                             inspect_point=self._inspect_point)
        reflector = Reflector(self.llm, self.store, model=settings.small_model, audit=self.audit,
                              cost=request_cost) if settings.self_learning else None
        tools = build_tools(mac=IS_MAC or computer is not None, glasses=settings.glasses_enabled)
        self.router = Router(settings, secrets)
        tools["route"] = route_tool(self.router)
        self.weather, self.recent = weather_mod.attach(settings, self.router, tools)
        self.push = push_from(settings, secrets, self.audit)    # reminders reach the phone while you're away
        self.agent = Agent(self.llm, self.store, self.policy, tools,
                           settings=settings, audit=self.audit, computer=computer, glasses=self.glasses,
                           glasses_status=(self.glasses.describe if self.glasses else lambda: None),
                           secrets=self._secret_values(secrets), reflector=reflector, device="mac")
        self.scheduler = Scheduler(self.store, deliver=self.deliver, run_task=self.run_task, headlines=format_headlines,
                                   tz=settings.timezone or None,
                                   trip_check=weather_mod.make_trip_check(self.agent, self.store, settings.user_name))
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

    def _inspect_point(self, coordinate) -> dict:
        """What's under a click (for spotting Buy / Pay buttons). Screenshot coordinates -> screen points."""
        if not IS_MAC or self.computer is None:
            return {}
        x, y = self.computer._to_screen(coordinate)
        return element_at(x, y)

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
        gate = VoiceGate()
        if approver is None:
            screen = MacApprover(require_owner_auth=s.approval_needs_touch_id, audit=self.audit)
            approver = VoiceOrScreenApprover(screen, gate, audit=self.audit, voice_ok=lambda: bool(
                s.voice_approvals and self.argus is not None and self.argus.voice_approvals_available()))
        brain = Brain(self.agent, speaker, approver, self.audit,
                      Sessions(self.store, "voice", "mac", s.session_idle_minutes))
        voiceprint = None
        if s.voice_lock and s.voiceprint_path.exists():
            voiceprint = VoicePrint(paths["speaker"], str(s.voiceprint_path), s.voice_lock_threshold)
        transcriber = transcriber or SherpaTranscriber(paths["asr"])
        calls = self._build_calls(transcriber, speaker, paths) if s.calls_enabled else None
        return Argus(source=source or Microphone(s.mic_device or None),
                     segmenter=Segmenter(paths["vad"], threshold=s.vad_threshold, min_silence=s.end_of_speech_seconds),
                     transcriber=transcriber, listener=listener, speaker=speaker,
                     brain=brain, audit=self.audit, voiceprint=voiceprint, voice_lock=s.voice_lock,
                     greeting=s.greeting, clap=ClapDetector(s.clap) if s.clap != "off" else None,
                     gate=gate, voice_approval_threshold=s.voice_approval_threshold, guests=s.guests,
                     guest_minutes=s.guest_minutes, owner_name=s.user_name, calls=calls, notify=notify)

    def _build_calls(self, transcriber, speaker, paths, line_factory=None):
        """Phone calls from your own number (calls.py): the tool Argus uses, and the switch for "hang up"."""
        from .calls import CALL_TOOL_DESCRIPTION, CALL_TOOL_SCHEMA, CallManager, MacCallLine
        from .toolbox import Tool
        s = self.settings
        if not hasattr(speaker.voice, "synthesize"):
            log.warning("phone calls need a voice that can render speech; calls are off")
            return None
        manager = CallManager(
            s, line_factory or (lambda: MacCallLine(input_device=s.call_input_device, tap=s.call_tap,
                                                    monitor=s.call_monitor)),
            self.llm, transcriber, speaker.voice, audit=self.audit, speaker=speaker, cost=request_cost,
            segmenter_factory=lambda: Segmenter(paths["vad"], threshold=s.vad_threshold, min_silence=0.8))
        self.agent.tools["phone_call"] = Tool("phone_call", CALL_TOOL_DESCRIPTION, CALL_TOOL_SCHEMA, manager.tool,
                                              frozenset({"voice"}))
        return manager

    def deliver(self, text: str) -> None:
        """A reminder or scheduled result, spoken at the Mac. If the Mac is locked or the microphone is off,
        it waits and is spoken as soon as you're back, and (with push set up) it goes to your phone too."""
        if self.argus is not None:
            self.argus.announce(text.replace("•", ""))
        if self.push is not None and (self.argus is None or not self.argus.present()):
            self.push.send(text)

    def run_task(self, text: str) -> str:
        session = self.store.session_for("scheduler", "tasks", 60)
        turn = self.agent.run(session_id=session, channel="scheduler", content=f"Scheduled task: {text}", events=Events())
        return turn.text.strip() or f"(The scheduled task didn't produce an answer: {turn.error or 'no reply'})"

    def ask_phone(self, content: Any, location: Optional[dict] = None) -> dict:
        session = self.store.session_for("phone", "iphone", 30)
        self.recent.note(location)          # in memory: lets 'here' at the Mac mean where the phone is
        turn = self.agent.run(session_id=session, channel="phone", content=content, events=Events(), location=location)
        reply = turn.text.strip() or ("Sorry, " + turn.error if turn.error else "Done.")
        return dict(phone_links(turn.links), reply=reply)

    # ---- start / stop ------------------------------------------------------------------------------
    def start_services(self) -> None:
        s = self.settings
        sync_default_jobs(self.store, s.morning_headlines, s.morning_briefing, s.timezone or None)
        self.scheduler.start()
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
        self.llm.close()
        if self.phone is not None:
            self.phone.shutdown()
        if self.argus is not None:
            self.argus.stop()
            self.argus.brain.cancel()
            self.argus.source.close()


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
    if app.computer is not None:
        from .computer import check_permissions
        handlers["permissions"] = lambda req: {"ok": True, **check_permissions()}
    control = ControlServer(settings.socket_path, handlers)
    control.start()
    guard = Guard(settings.state_path, extra_state=argus.guard_state,
                  on_lock_change=lambda locked: settings.pause_when_locked and argus.set_locked(locked))
    guard.start()
    watcher = None
    if settings.pause_during_calls:                  # a call at the Mac: Argus keeps out of it (micwatch.py)
        from .micwatch import MicWatch
        watcher = MicWatch(settings.bin_dir / "argus-mic", argus.set_on_call, ignore=settings.ignore_mic_apps)
        if not watcher.start():
            log.info("the microphone watcher isn't built; calls at the Mac aren't noticed")
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
