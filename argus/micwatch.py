"""Notice when you're on a call at the Mac, so Argus stays out of it.

The small argus-mic helper (macos/argus-mic.swift; setup builds it) reports which other apps have a microphone
open: FaceTime or the Phone app (calls from your iPhone through the Mac), Zoom, Teams, Webex, WhatsApp, a meeting
in a browser. While one does, Argus closes its own microphone, stops talking, and keeps reminders for later; a
few seconds after the call ends it listens again. Only app names are read: nothing is recorded.

Apple's own always-on listeners ("Hey Siri", dictation, accessibility features) don't count, and you can add
others to [listening] ignore_mic_apps. A call you hold to your ear on the iPhone itself can't be seen from the
Mac: say "Argus, I'm on a call" (and "Argus, I'm back" afterwards).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence

log = logging.getLogger("argus.micwatch")

# Apple's background listeners that hold a microphone without a call going on
ALWAYS_LISTENING = ("com.apple.corespeech", "com.apple.assistant", "com.apple.siri", "com.apple.speech",
                    "com.apple.SpeechRecognitionCore", "com.apple.accessibility", "com.apple.VoiceOver",
                    "com.apple.controlcenter", "com.apple.CoreSpeech", "com.apple.dictation",
                    "com.argus.voice", "org.python.python", "argus-ear")
NAMES = {"com.apple.FaceTime": "FaceTime", "com.apple.mobilephone": "Phone", "com.apple.TelephonyUtilities": "Phone",
         "com.apple.telephonyutilities.callservicesd": "Phone", "us.zoom.xos": "Zoom", "com.microsoft.teams": "Teams",
         "com.microsoft.teams2": "Teams", "com.cisco.webexmeetingsapp": "Webex", "net.whatsapp.WhatsApp": "WhatsApp",
         "desktop.WhatsApp": "WhatsApp", "com.google.Chrome": "Chrome", "com.apple.Safari": "Safari",
         "org.mozilla.firefox": "Firefox", "com.microsoft.edgemac": "Edge", "com.tinyspeck.slackmacgap": "Slack",
         "com.hnc.Discord": "Discord", "ru.keepcoder.Telegram": "Telegram", "com.skype.skype": "Skype",
         "com.apple.QuickTimePlayerX": "QuickTime", "com.apple.VoiceMemos": "Voice Memos"}
QUIET_AFTER = 3.0          # seconds the microphone must be free before Argus listens again


def friendly(app: str) -> str:
    return NAMES.get(app) or app.rsplit(".", 1)[-1] or app


def on_a_call(apps: Iterable[str], ignore: Sequence[str] = ()) -> List[str]:
    """The apps that mean you're on a call (or recording): everything but Apple's always-on listeners and the
    apps you told Argus to ignore."""
    skip = tuple(ALWAYS_LISTENING) + tuple(i for i in ignore if i)
    found = []
    for app in apps:
        app = str(app)
        if not app or any(app.lower().startswith(s.lower()) or friendly(app).lower() == s.lower() for s in skip):
            continue
        found.append(app)
    return sorted(set(found))


class MicWatch:
    """Runs argus-mic --watch and calls on_change(apps) when the apps holding a microphone change. A free
    microphone is reported only after it has stayed free for QUIET_AFTER seconds (no flapping mid-call)."""

    def __init__(self, exe: Path, on_change: Callable[[List[str]], None], *, ignore: Sequence[str] = (),
                 popen: Callable[..., Any] = subprocess.Popen, own_pid: Optional[int] = None,
                 quiet_after: float = QUIET_AFTER):
        self.exe, self.on_change, self.ignore, self.popen = Path(exe), on_change, list(ignore), popen
        self.own_pid = own_pid or os.getpid()
        self.quiet_after = quiet_after
        self.apps: List[str] = []
        self._proc: Any = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._stopped = False

    def available(self) -> bool:
        return self.exe.is_file() and os.access(self.exe, os.X_OK)

    def start(self) -> bool:
        if not self.available():
            return False
        try:
            self._proc = self.popen([str(self.exe), "--watch", "--except", str(self.own_pid)], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
        except OSError as exc:
            log.warning("couldn't start the microphone watcher: %s", exc)
            return False
        threading.Thread(target=self._read, name="argus-micwatch", daemon=True).start()
        return True

    def _read(self) -> None:
        for line in self._proc.stdout:
            try:
                apps = json.loads(line).get("apps") or []
            except (ValueError, AttributeError):
                continue
            self.seen(apps if isinstance(apps, list) else [])

    def seen(self, raw: Iterable[str]) -> None:
        apps = on_a_call(raw, self.ignore)
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if apps == self.apps or self._stopped:
                return
            if apps:
                self.apps = apps
                report = apps
            else:                                        # the call ended: wait a moment before listening again
                self._timer = threading.Timer(self.quiet_after, self._quiet)
                self._timer.daemon = True
                self._timer.start()
                return
        self.on_change(report)

    def _quiet(self) -> None:
        with self._lock:
            self._timer = None
            if not self.apps or self._stopped:
                return
            self.apps = []
        self.on_change([])

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
