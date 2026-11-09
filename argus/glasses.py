"""Bridge to the Argus iPhone app, which talks to your Meta glasses (Meta Wearables Device Access
Toolkit). The app reports when the glasses are connected and sends a photo when Argus asks for one."""
from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime
from typing import Dict, Optional


class GlassesBridge:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._cond = threading.Condition()
        self.connected = False
        self.device = ""
        self.since = 0.0
        self.app_seen = 0.0
        self._wanted: Optional[str] = None
        self._frames: Dict[str, bytes] = {}

    def update_status(self, connected: bool, device: str = "") -> None:
        with self._cond:
            if connected and not self.connected:
                self.since = self.clock()
            self.connected, self.device = bool(connected), str(device or "")[:60]
            self.app_seen = self.clock()

    def app_alive(self, within: float = 90.0) -> bool:
        return self.clock() - self.app_seen <= within

    def describe(self) -> Optional[str]:
        """A line for the brain's context, or None if the app hasn't been in touch."""
        if not self.app_alive():
            return None
        if not self.connected:
            return "not connected right now"
        since = datetime.fromtimestamp(self.since).strftime("%H:%M")
        return f"{self.device or 'glasses'} connected since {since}; glasses_look can see what the user sees"

    # The iPhone app long-polls next_request(); Argus calls capture() and waits for deliver().
    def next_request(self, wait: float = 25.0) -> Optional[str]:
        deadline = self.clock() + wait
        with self._cond:
            self.app_seen = self.clock()
            while self._wanted is None:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 1.0))
            return self._wanted

    def deliver(self, request_id: str, jpeg: bytes) -> bool:
        with self._cond:
            self.app_seen = self.clock()
            if request_id != self._wanted:
                return False
            self._frames[request_id] = jpeg
            self._wanted = None
            self._cond.notify_all()
            return True

    def capture(self, timeout: float = 25.0, cancel: Optional[threading.Event] = None) -> bytes:
        if not self.app_alive():
            raise RuntimeError("the Argus iPhone app isn't open, so the glasses camera isn't reachable")
        if not self.connected:
            raise RuntimeError("the glasses aren't connected to the phone right now")
        request_id = secrets.token_hex(8)
        deadline = self.clock() + timeout
        with self._cond:
            self._wanted = request_id
            self._cond.notify_all()
            while request_id not in self._frames:
                if (cancel is not None and cancel.is_set()) or self.clock() > deadline:
                    if self._wanted == request_id:
                        self._wanted = None
                    raise RuntimeError("the glasses didn't send a picture in time")
                self._cond.wait(0.25)
            return self._frames.pop(request_id)
