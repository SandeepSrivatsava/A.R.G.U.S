"""Privacy guard: knows when the Mac is locked, which app is in front and which apps are visible,
and matches apps and web addresses against the "never touch these" lists that policy.py enforces."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("argus.guard")


def screen_locked() -> Optional[bool]:
    """True/False on macOS; None when it can't be determined."""
    try:
        import Quartz  # pyobjc-framework-Quartz
        info = Quartz.CGSessionCopyCurrentDictionary()
        if info is None:
            return True  # no window-server session (e.g. at the login window)
        return bool(info.get("CGSSessionScreenIsLocked", False))
    except Exception:
        return None


def frontmost_app() -> Optional[Tuple[str, str]]:
    """(bundle id, name) of the app in front, or None."""
    try:
        from AppKit import NSWorkspace  # pyobjc-framework-Cocoa
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is not None:
            return str(app.bundleIdentifier() or ""), str(app.localizedName() or "")
    except Exception:
        pass
    try:  # fallback without PyObjC
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
        if asn:
            info = subprocess.run(["lsappinfo", "info", "-only", "bundleid", "-only", "name", asn],
                                  capture_output=True, text=True, timeout=3).stdout
            bundle = re.search(r'"(?:CFBundleIdentifier|bundleID)"\s*=\s*"([^"]+)"', info)
            name = re.search(r'"(?:LSDisplayName|name)"\s*=\s*"([^"]+)"', info)
            if bundle or name:
                return (bundle.group(1) if bundle else ""), (name.group(1) if name else "")
    except Exception:
        pass
    return None


def visible_apps() -> Optional[List[str]]:
    """Names and bundle ids of apps with windows on screen right now (no Screen Recording needed)."""
    try:
        import Quartz
        from AppKit import NSRunningApplication
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    except Exception:
        return None
    seen: List[str] = []
    for w in windows or []:
        if int(w.get("kCGWindowLayer", 0)) != 0:
            continue
        name = str(w.get("kCGWindowOwnerName") or "")
        pid = int(w.get("kCGWindowOwnerPID") or 0)
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
        for value in (name, str(app.bundleIdentifier() or "") if app is not None else ""):
            if value and value not in seen:
                seen.append(value)
    return seen


def desktop_state() -> Dict[str, Any]:
    """Fresh look at the screen's privacy-relevant state, for the policy check before a desktop action."""
    front = frontmost_app()
    return {"locked": screen_locked(), "front_bundle": front[0] if front else None,
            "front_name": front[1] if front else None, "visible_apps": visible_apps() or []}


def is_blocked_app(app: str, blocked: Iterable[str]) -> bool:
    """`app` may be a name ("1Password 7") or a bundle id; entries likewise. Errs on the side of blocking."""
    a = " ".join((app or "").lower().split())
    if not a:
        return False
    for entry in blocked:
        e = " ".join(entry.lower().split())
        if not e:
            continue
        if a == e or a.startswith(e + ".") or a.startswith(e + " ") or e.startswith(a + " "):
            return True
    return False


def _domain_pattern(domain: str) -> re.Pattern:
    d = re.escape(domain.strip().lower().lstrip("*.").rstrip("/"))
    return re.compile(r"(?<![a-z0-9-])(?:[a-z0-9-]+\.)*" + d + r"(?![a-z0-9-])")


def mentions_blocked_site(value: Any, blocked_sites: Iterable[str]) -> Optional[str]:
    """The first blocked site mentioned anywhere in a tool's arguments (URLs, typed text, tasks)."""
    patterns = [(s, _domain_pattern(s)) for s in blocked_sites if s and s.strip()]
    if not patterns:
        return None
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str):
            low = item.lower()
            for site, pattern in patterns:
                if pattern.search(low):
                    return site
    return None


def read_state(path: Path, max_age: float = 5.0) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or time.time() - float(data.get("updated", 0)) > max_age:
        return None
    return data


def write_state(path: Path, state: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(dict(state, updated=time.time()), fh)
    os.replace(tmp, path)


class Guard:
    """Polls lock state and the front app once a second; tells the daemon when the lock changes."""

    def __init__(self, state_path: Path, on_lock_change: Callable[[bool], None],
                 extra_state: Callable[[], Dict[str, Any]] = dict, interval: float = 1.0,
                 locked_fn: Callable[[], Optional[bool]] = screen_locked,
                 front_fn: Callable[[], Optional[Tuple[str, str]]] = frontmost_app):
        self.state_path = state_path
        self.on_lock_change = on_lock_change
        self.extra_state = extra_state
        self.interval = interval
        self.locked_fn, self.front_fn = locked_fn, front_fn
        self.locked: Optional[bool] = None
        self.front: Optional[Tuple[str, str]] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="argus-guard", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> None:
        locked = self.locked_fn()
        front = self.front_fn()
        if locked is not None and locked != self.locked:
            previous, self.locked = self.locked, locked
            if previous is not None or locked:
                try:
                    self.on_lock_change(locked)
                except Exception:
                    log.exception("lock handler failed")
        self.front = front
        write_state(self.state_path, {
            "locked": self.locked, "front_bundle": front[0] if front else None,
            "front_name": front[1] if front else None, **self.extra_state()})

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("privacy guard poll failed")
            self._stop.wait(self.interval)
