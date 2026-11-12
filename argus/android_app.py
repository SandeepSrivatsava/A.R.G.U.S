"""Argus running on an Android phone (in Termux). One background process ("serve", started at boot by
Termux:Boot) holds the brain, the reminders, the notification log and drive assist; the Argus home-screen
shortcut (Termux:Widget) runs "talk", which asks that process to listen, answer out loud and open any
navigation link.

    python -I run.py android serve      # the background process
    python -I run.py android talk       # push-to-talk (the home-screen shortcut)
    python -I run.py android ask TEXT   # typed request; the reply is printed (and spoken)
    python -I run.py android status

Compared with the Mac: no shell commands (Android gives Termux no sandbox to run them in), approvals are a
dialog plus your fingerprint, and listening is push-to-talk (Android doesn't let an app like Termux keep the
microphone open in the background)."""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import config as config_mod
from .agent import Agent, Events, request_cost
from .android import (AndroidApprover, MessageSearch, NotificationLog, Termux, TermuxError, WhatsAppBackup,
                      contact_names, phone_tools)
from .approvals import ApprovalRequest
from .audit import Audit
from .control import ControlServer, send
from .lessons import Reflector
from .llm import Claude
from . import weather as weather_mod
from .maps import MapsError, Place, Router, minutes, nav_link, route_tool
from .news import format_headlines
from .policy import Policy
from .runtime import setup_logging, single_instance
from .scheduler import Scheduler, sync_default_jobs
from .store import Store
from .toolbox import build_tools

log = logging.getLogger("argus.android")

POLL_SECONDS = 30            # how often the notification log looks for new notifications
DRIVE_CHECK_SECONDS = 60     # drive assist: how often it checks for driving (only while charging)
DRIVING_SPEED = 6.7          # metres a second (15 mph): faster than this while charging counts as driving
ASK_AGAIN_AFTER = 2 * 3600   # don't ask "where are you headed?" again for two hours
RECHECK_SECONDS = 10 * 60    # during a trip: look at the traffic again this often
BETTER_BY = 5 * 60           # announce another route only when it saves at least five minutes
NO_TRIP = ("", "nowhere", "no", "nothing", "never mind", "nevermind", "cancel", "home is fine")


class _PhoneEvents(Events):
    def __init__(self, approver: AndroidApprover):
        self.approver = approver

    def approve(self, request: ApprovalRequest) -> str:
        return self.approver.ask(request)


class AndroidApp:
    def __init__(self, settings: config_mod.Settings, *, secrets: Optional[Dict[str, str]] = None,
                 termux: Optional[Termux] = None, router: Optional[Router] = None,
                 clock: Callable[[], float] = time.time):
        self.settings, self.clock = settings, clock
        secrets = secrets if secrets is not None else config_mod.read_secrets(settings.secrets_path)
        self.termux = termux or Termux()
        self.audit = Audit(settings.audit_path)
        self.store = Store(settings.db_path)
        self.llm = Claude(secrets.get("ANTHROPIC_API_KEY", ""), base_url=settings.api_base)
        self.policy = Policy(settings, workspace=settings.workspace, outside_workspace="read")
        tools = build_tools(mac=False, glasses=False, commands=False)
        self.router = router or Router(settings, secrets)
        tools["route"] = route_tool(self.router)
        self.weather, self.recent = weather_mod.attach(settings, self.router, tools)
        self.notes: Optional[NotificationLog] = None
        self.whatsapp: Optional[WhatsAppBackup] = None
        search = None
        if settings.android_messages:
            self.notes = NotificationLog(settings.private_dir / "notifications.db", clock=clock)
            if secrets.get("WHATSAPP_BACKUP_KEY"):
                self.whatsapp = WhatsAppBackup(secrets["WHATSAPP_BACKUP_KEY"], settings.private_dir / "whatsapp.db",
                                               tool=settings.home / "wa-crypt-tools" / "bin" / "wadecrypt",
                                               folder=settings.android_whatsapp_dir, contacts=contact_names(self.termux))
            search = MessageSearch(self.termux, self.notes, self.whatsapp, clock=clock)
        tools.update(phone_tools(self.termux, search))
        self.approver = AndroidApprover(self.termux, self.audit)
        reflector = Reflector(self.llm, self.store, model=settings.small_model, audit=self.audit,
                              cost=request_cost) if settings.self_learning else None
        self.agent = Agent(self.llm, self.store, self.policy, tools, settings=settings, audit=self.audit,
                           secrets=[v for v in secrets.values() if v], reflector=reflector, device="android")
        self.scheduler = Scheduler(self.store, deliver=self.deliver, run_task=self.run_task,
                                   headlines=format_headlines, tz=settings.timezone or None,
                                   trip_check=weather_mod.make_trip_check(self.agent, self.store, settings.user_name))
        self.busy = threading.Lock()
        self._stop = threading.Event()
        self.trip: Optional[Dict[str, Any]] = None

    # ---- answering -------------------------------------------------------------------------------------
    def locate(self) -> Optional[Dict[str, float]]:
        """Where the phone is (a quick network fix, else the last known one)."""
        for provider, request in (("network", "once"), ("gps", "last")):
            try:
                found = self.termux.location(provider, request, timeout=30)
            except TermuxError as exc:
                log.info("no location from %s: %s", provider, exc)
                continue
            if found:
                return found
        return None

    def ask(self, text: str, *, speak: bool = False, open_links: bool = True,
            location: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        text = " ".join(str(text).split())[:4000]
        if not text:
            return {"ok": False, "reply": ""}
        if not self.busy.acquire(timeout=1):
            return {"ok": False, "reply": "I'm still working on your last request."}
        try:
            session = self.store.session_for("android", "phone", self.settings.session_idle_minutes)
            turn = self.agent.run(session_id=session, channel="android", content=text, events=_PhoneEvents(self.approver),
                                  location=location, locate=None if location else self.locate)
        finally:
            self.busy.release()
        reply = turn.text.strip() or ("Sorry, " + turn.error if turn.error else "Done.")
        if speak and self.settings.android_speak:
            self._say(reply)
        link = turn.links[0] if turn.links else None
        if link and open_links:
            try:
                self.termux.open_url(link)
            except TermuxError as exc:
                log.warning("couldn't open the navigation link: %s", exc)
        return {"ok": not turn.error, "reply": reply, "open": link}

    def listen_and_answer(self) -> Dict[str, Any]:
        """Push-to-talk: listen once, answer out loud."""
        try:
            heard = self.termux.listen()
        except TermuxError as exc:
            return {"ok": False, "reply": f"I couldn't listen: {exc}"}
        if not heard:
            self._say("I didn't catch that.")
            return {"ok": False, "reply": ""}
        self.audit.log("heard", channel="android", chars=len(heard))
        return dict(self.ask(heard, speak=True), heard=heard)

    def _say(self, text: str) -> None:
        try:
            self.termux.speak(text)
        except TermuxError as exc:
            log.warning("couldn't speak: %s", exc)

    def deliver(self, text: str) -> None:
        """A reminder or scheduled result: a notification, and spoken out loud."""
        clean = text.replace("•", "-")
        try:
            self.termux.notify("Argus", clean, nid=f"argus-{int(self.clock())}")
        except TermuxError as exc:
            log.warning("couldn't show a notification: %s", exc)
        if self.settings.android_speak:
            self._say(clean)

    def run_task(self, text: str) -> str:
        session = self.store.session_for("scheduler", "tasks", 60)
        turn = self.agent.run(session_id=session, channel="scheduler", content=f"Scheduled task: {text}", events=Events())
        return turn.text.strip() or f"(The scheduled task didn't produce an answer: {turn.error or 'no reply'})"

    # ---- the notification log --------------------------------------------------------------------------
    def poll_notifications(self) -> int:
        if self.notes is None:
            return 0
        try:
            return self.notes.record(self.termux.notifications())
        except TermuxError as exc:
            log.info("couldn't read notifications: %s", exc)
            return 0

    # ---- drive assist (opt-in): while charging and moving, ask where you're headed and watch the traffic ----
    def drive_tick(self) -> Optional[str]:
        """One look. Returns what happened (for the log and tests)."""
        try:
            battery = self.termux.battery()
        except TermuxError:
            return None
        if str(battery.get("plugged") or "UNPLUGGED").upper() == "UNPLUGGED":
            self.trip = None
            return "not charging"
        try:
            here = self.termux.location("gps", "once", timeout=40)
        except TermuxError:
            return None
        if not here:
            return None
        moving = here.get("speed", 0) >= DRIVING_SPEED
        now = self.clock()
        if self.trip is not None:
            if now - self.trip["checked"] >= RECHECK_SECONDS:
                return self._recheck(here, now)
            return "on a trip"
        if not moving:
            return "not moving"
        try:
            last = float(self.store.get("drive_asked_at") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if now - last < ASK_AGAIN_AFTER:
            return "asked recently"
        self.store.set("drive_asked_at", str(now))
        self._say("Where are you headed?")
        try:
            destination = self.termux.listen(timeout=20)
        except TermuxError:
            destination = ""
        if destination.strip().lower().strip(".!") in NO_TRIP:
            return "no destination"
        self.audit.log("drive_assist", step="destination")
        result = self.ask(f"I'm driving to {destination} now. Check the live traffic for the best route from here "
                          f"and tell me briefly.", speak=True, location=here)
        self.trip = {"destination": destination, "checked": now, "via": None}
        try:
            _, routes = self.router.plan(Place.current(here), Place.from_text(destination))
            if routes:
                self.trip["via"] = routes[0].via       # the route Argus suggested: watched from now on
        except MapsError:
            pass
        return "trip started" if result.get("ok") else "trip started (no answer)"

    def _recheck(self, here: Dict[str, float], now: float) -> str:
        trip = self.trip
        trip["checked"] = now
        if here.get("speed", 0) < 1:
            trip["still"] = trip.get("still", 0) + 1
            if trip["still"] >= 2:         # parked for a while: the trip is over
                self.trip = None
                return "trip over"
            return "stopped"
        trip["still"] = 0
        try:
            _, routes = self.router.plan(Place.current(here), Place.from_text(trip["destination"]))
        except MapsError as exc:
            log.info("drive assist couldn't check traffic: %s", exc)
            return "no traffic data"
        current = next((r for r in routes if trip.get("via") and r.via == trip["via"]), None)
        if not routes or current is None:
            return "route unknown"
        fastest = routes[0]
        if fastest is not current and current.seconds - fastest.seconds >= BETTER_BY:
            trip["via"] = fastest.via
            via = f"via {fastest.via}" if fastest.via else "another way"
            self._say(f"Traffic has changed. Going {via} is now {minutes(current.seconds - fastest.seconds)} faster, "
                      f"{minutes(fastest.seconds)} in all. I'll open it in Maps.")
            try:
                self.termux.open_url(nav_link(self.settings.maps_link_app, Place.current(here),
                                              Place.from_text(trip["destination"])))
            except TermuxError:
                pass
            self.audit.log("drive_assist", step="faster_route", saves_seconds=current.seconds - fastest.seconds)
            return "faster route"
        return "no better route"

    # ---- running -----------------------------------------------------------------------------------------
    def _every(self, seconds: float, work: Callable[[], Any]) -> None:
        while not self._stop.wait(seconds):
            try:
                work()
            except Exception:
                log.exception("background job failed")

    def start(self) -> None:
        s = self.settings
        sync_default_jobs(self.store, s.morning_headlines, s.morning_briefing, s.timezone or None)
        self.scheduler.start()
        jobs: List[Any] = []
        if self.notes is not None:
            self.notes.prune(s.android_notification_days)
            jobs.append((POLL_SECONDS, self.poll_notifications))
            jobs.append((24 * 3600, lambda: self.notes.prune(s.android_notification_days)))
        if s.android_drive_assist:
            jobs.append((DRIVE_CHECK_SECONDS, self.drive_tick))
        for seconds, work in jobs:
            threading.Thread(target=self._every, args=(seconds, work), daemon=True).start()
        self.store.prune_sessions(older_than_days=45)
        try:
            self.termux.wake_lock()        # keeps Android from freezing the reminders while the screen is off
        except TermuxError as exc:
            log.warning("no wake lock: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        self.scheduler.stop()
        self.llm.close()
        if self.notes is not None:
            self.notes.close()

    def status(self) -> Dict[str, Any]:
        return {"ok": True, "mode": "android", "busy": self.busy.locked(), "maps": self.router.describe(),
                "whatsapp": (self.whatsapp.problem() or "ready") if self.whatsapp else "not set up",
                "drive_assist": self.settings.android_drive_assist}

    def handlers(self) -> Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]:
        def talk(req):
            if req.get("text"):
                return self.ask(str(req["text"]), speak=bool(req.get("speak")))
            return self.listen_and_answer()
        def say(req):
            self._say(str(req.get("text") or ""))
            return {"ok": True}
        return {"status": lambda req: self.status(), "ask": talk, "talk": talk, "say": say}


def serve() -> int:
    try:
        settings = config_mod.load()
    except ValueError as exc:
        print(f"Argus could not start: there's a problem in its settings: {exc}", file=sys.stderr)
        return 2
    config_mod.ensure_private_dirs(settings)
    setup_logging(settings)
    try:
        secrets = config_mod.read_secrets(settings.secrets_path)
    except PermissionError as exc:
        print(f"Argus could not start: {exc}", file=sys.stderr)
        return 2
    if not secrets.get("ANTHROPIC_API_KEY"):
        print(f"Argus could not start: no Claude API key in {settings.secrets_path} (run ./setup.sh)", file=sys.stderr)
        return 2
    lock = single_instance(settings)
    if lock is None:
        print("Argus is already running.")
        return 0
    app = AndroidApp(settings, secrets=secrets)
    control = ControlServer(settings.socket_path, app.handlers())
    control.start()
    app.start()
    app.audit.log("started", mode="android")
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.wait(1):
        pass
    app.stop()
    control.stop()
    app.audit.log("stopped", mode="android")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="argus android", description="Argus on this Android phone (Termux).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run Argus in the background (Termux:Boot starts this)")
    sub.add_parser("talk", help="listen once and answer out loud (the home-screen shortcut)")
    s = sub.add_parser("ask", help="ask in writing; the reply is printed")
    s.add_argument("text", nargs="+")
    s.add_argument("--speak", action="store_true", help="also say the reply out loud")
    sub.add_parser("status", help="is Argus running, and what's set up")
    args = p.parse_args(argv)
    if args.cmd == "serve":
        return serve()
    try:
        settings = config_mod.load()
    except ValueError as exc:
        print(f"There's a problem in Argus's settings: {exc}")
        return 2
    fields: Dict[str, Any] = {}
    if args.cmd == "ask":
        fields = {"text": " ".join(args.text), "speak": args.speak}
    try:
        reply = send(settings.socket_path, "status" if args.cmd == "status" else "talk", timeout=300, **fields)
    except OSError:
        print("Argus isn't running. Start it with: argus-android serve &   (it starts by itself after a restart)")
        if args.cmd == "talk":
            Termux().speak("Argus isn't running yet.")
        return 1
    if args.cmd == "status":
        print(json.dumps(reply, indent=2))
    elif reply.get("reply"):
        print(reply["reply"])
    return 0 if reply.get("ok") else 1
