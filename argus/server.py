"""Server mode: Argus on an always-on Linux machine (a small cloud server), for when the Mac is off or you're
away from it. Your phone talks to it over Tailscale with the same Shortcut (iPhone) or HTTP Shortcuts
(Android), and reminders arrive as push notifications (push.py).

Compared with the Mac:
* no microphone, speaker or screen, and no shell commands (there's no kernel sandbox here to run them in);
* files only inside its own workspace folder;
* anything that would need your OK is refused (approve risky things at the Mac or on Android);
* its own memory: facts and lessons aren't shared with the Mac.
systemd starts it (scripts/setup_server.sh) as an unprivileged user, with `python3 -I run.py server`.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
from typing import Any, Dict, Optional

from . import config as config_mod
from .agent import Agent, Events, request_cost
from .audit import Audit
from .control import ControlServer
from .lessons import Reflector
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

log = logging.getLogger("argus.server")
GATEWAY_RETRY = 15.0       # seconds between tries while Tailscale isn't up yet


class ServerApp:
    def __init__(self, settings: config_mod.Settings, *, secrets: Optional[Dict[str, str]] = None,
                 router: Optional[Router] = None, push=None):
        self.settings = settings
        secrets = secrets if secrets is not None else config_mod.read_secrets(settings.secrets_path)
        self.audit = Audit(settings.audit_path)
        self.store = Store(settings.db_path)
        self.llm = Claude(secrets.get("ANTHROPIC_API_KEY", ""), base_url=settings.api_base)
        self.policy = Policy(settings, workspace=settings.workspace, outside_workspace="none")
        tools = build_tools(mac=False, glasses=False, commands=False)
        self.router = router or Router(settings, secrets)
        tools["route"] = route_tool(self.router)
        self.weather, self.recent = weather_mod.attach(settings, self.router, tools)
        self.push = push if push is not None else push_from(settings, secrets, self.audit)
        reflector = Reflector(self.llm, self.store, model=settings.small_model, audit=self.audit,
                              cost=request_cost) if settings.self_learning else None
        self.agent = Agent(self.llm, self.store, self.policy, tools, settings=settings, audit=self.audit,
                           secrets=[v for v in secrets.values() if v], reflector=reflector, device="server")
        self.scheduler = Scheduler(self.store, deliver=self.deliver, run_task=self.run_task,
                                   headlines=format_headlines, tz=settings.timezone or None,
                                   trip_check=weather_mod.make_trip_check(self.agent, self.store, settings.user_name))
        self.phone: Optional[PhoneServer] = None
        self._stop = threading.Event()

    def deliver(self, text: str) -> None:
        """Reminders and scheduled results go to the phone as notifications."""
        if self.push is None or not self.push.send(text.replace("•", "-")):
            self.audit.log("delivery_failed", chars=len(text), push=self.push is not None)
            log.warning("couldn't deliver a reminder: push notifications %s",
                        "failed" if self.push is not None else "aren't set up")

    def run_task(self, text: str) -> str:
        session = self.store.session_for("scheduler", "tasks", 60)
        turn = self.agent.run(session_id=session, channel="scheduler", content=f"Scheduled task: {text}", events=Events())
        return turn.text.strip() or f"(The scheduled task didn't produce an answer: {turn.error or 'no reply'})"

    def ask_phone(self, content: Any, location: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        session = self.store.session_for("phone", "phone", 30)
        self.recent.note(location)          # in memory: lets 'here' at the Mac mean where the phone is
        turn = self.agent.run(session_id=session, channel="phone", content=content, events=Events(), location=location)
        reply = turn.text.strip() or ("Sorry, " + turn.error if turn.error else "Done.")
        return dict(phone_links(turn.links), reply=reply)

    def ask_cli(self, text: str) -> str:
        session = self.store.session_for("cli", "server", 30)
        turn = self.agent.run(session_id=session, channel="cli", content=text, events=Events())
        return turn.text.strip() or ("Sorry, " + turn.error if turn.error else "Done.")

    def status(self) -> Dict[str, Any]:
        gateway = None
        if self.phone is not None:
            host, port = self.phone.server_address[:2]
            gateway = f"http://{host}:{port}"
        return {"ok": True, "mode": "server", "phone_gateway": gateway, "push": self.push is not None,
                "maps": self.router.describe()}

    # ---- the phone gateway, once Tailscale is up --------------------------------------------------------
    def start_gateway(self) -> bool:
        host = bind_address(self.settings)
        if not host:
            return False
        token = load_or_create_token(self.settings.phone_token_path)
        self.agent.add_secret(token)
        self.phone = PhoneServer((host, self.settings.phone_port), self.ask_phone, Gatekeeper(token), self.audit)
        threading.Thread(target=self.phone.serve_forever, name="argus-phone", daemon=True).start()
        self.audit.log("phone_gateway_started", host=host, port=self.settings.phone_port)
        return True

    def _gateway_loop(self, retry: float = GATEWAY_RETRY) -> None:
        warned = False
        while not self._stop.is_set():
            try:
                if self.start_gateway():
                    return
            except OSError as exc:
                log.warning("the phone gateway couldn't start yet: %s", exc)
            if not warned:
                log.warning("waiting for Tailscale before opening the phone gateway")
                warned = True
            self._stop.wait(retry)

    def start(self, retry: float = GATEWAY_RETRY) -> None:
        s = self.settings
        sync_default_jobs(self.store, s.morning_headlines, s.morning_briefing, s.timezone or None)
        self.scheduler.start()
        if s.phone_enabled:
            threading.Thread(target=self._gateway_loop, args=(retry,), name="argus-gateway", daemon=True).start()
        self.store.prune_sessions(older_than_days=45)

    def stop(self) -> None:
        self._stop.set()
        self.scheduler.stop()
        self.llm.close()
        if self.phone is not None:
            self.phone.shutdown()

    def handlers(self):
        def ask(req):
            text = " ".join(str(req.get("text") or "").split())
            return {"ok": bool(text), "reply": self.ask_cli(text) if text else ""}

        def say(req):
            text = str(req.get("text") or "").strip()
            return {"ok": bool(text and self.push is not None and self.push.send(text))}
        return {"status": lambda req: self.status(), "ask": ask, "say": say}


def main(stop: Optional[threading.Event] = None) -> int:
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
        print(f"Argus could not start: no Claude API key in {settings.secrets_path}", file=sys.stderr)
        return 2
    lock = single_instance(settings)
    if lock is None:
        print("Argus is already running.")
        return 0
    app = ServerApp(settings, secrets=secrets)
    control = ControlServer(settings.socket_path, app.handlers())
    control.start()
    app.start()
    app.audit.log("started", mode="server")
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(1):
        pass
    app.stop()
    control.stop()
    app.audit.log("stopped", mode="server")
    return 0
