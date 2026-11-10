"""Argus's own Mac control for Claude's computer-use toolset (`computer_toolset_20260801`): screenshots
with `screencapture`, mouse and keyboard with Quartz events. Needs Screen Recording and Accessibility
permission for Argus.app. Every input action is approved by you first (policy.py + approvals.py)."""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .toolbox import ToolOutput

TOOLSET = {"type": "computer_toolset_20260801"}
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."

KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "backspace": 51, "delete": 51, "esc": 53, "escape": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "page_up": 116, "pagedown": 121,
    "page_down": 121, "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111,
}
MODIFIERS = {"cmd": "cmd", "command": "cmd", "super": "cmd", "meta": "cmd", "shift": "shift", "ctrl": "ctrl",
             "control": "ctrl", "alt": "option", "option": "option", "opt": "option", "fn": "fn"}
KEY_ALIASES = {"arrowleft": "left", "arrowright": "right", "arrowup": "up", "arrowdown": "down", "kp_enter": "enter",
               "page_up": "pageup", "page_down": "pagedown", "bksp": "backspace", "del": "forwarddelete"}


def parse_combo(text: str) -> Tuple[List[str], Optional[str]]:
    """"cmd+shift+t" -> (["cmd", "shift"], "t"). Key names follow xdotool style ("Return", "ctrl+s")."""
    parts = [p.strip() for p in str(text).split("+") if p.strip()] if text.strip() != "+" else ["+"]
    mods: List[str] = []
    key: Optional[str] = None
    for part in parts:
        low = part.lower()
        if low in MODIFIERS:
            mods.append(MODIFIERS[low])
        else:
            key = KEY_ALIASES.get(low, low)
    return mods, key


def fit(width: int, height: int, max_w: int = 1280, max_h: int = 800) -> Tuple[int, int]:
    scale = min(max_w / width, max_h / height, 1.0)
    return max(1, int(width * scale)), max(1, int(height * scale))


class Backend:
    """What the executor needs from the OS. MacBackend is the real one; tests use a fake."""

    def screen_size(self) -> Tuple[int, int]: ...                         # logical points of the main display
    def capture(self, region: Optional[Tuple[int, int, int, int]], size: Tuple[int, int]) -> bytes: ...  # PNG
    def move(self, x: float, y: float) -> None: ...
    def click(self, x: float, y: float, button: str, count: int, mods: Sequence[str]) -> None: ...
    def button(self, x: float, y: float, down: bool) -> None: ...
    def drag(self, start: Tuple[float, float], end: Tuple[float, float], mods: Sequence[str]) -> None: ...
    def scroll(self, x: float, y: float, dx: int, dy: int, mods: Sequence[str]) -> None: ...
    def type_text(self, text: str) -> None: ...
    def key(self, mods: Sequence[str], key: Optional[str], hold: float = 0.0) -> None: ...
    def cursor(self) -> Tuple[float, float]: ...


class Computer:
    def __init__(self, backend: Backend, max_w: int = 1280, max_h: int = 800):
        self.backend = backend
        self.max_w, self.max_h = max_w, max_h

    def _shot_size(self) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        screen = self.backend.screen_size()
        return screen, fit(screen[0], screen[1], self.max_w, self.max_h)

    def _to_screen(self, coord: Any) -> Tuple[float, float]:
        if not (isinstance(coord, (list, tuple)) and len(coord) == 2):
            raise ValueError("coordinate must be [x, y]")
        (sw, sh), (iw, ih) = self._shot_size()
        x, y = float(coord[0]), float(coord[1])
        if not (0 <= x <= iw and 0 <= y <= ih):
            raise ValueError(f"coordinate {coord} is outside the {iw}x{ih} screenshot")
        return x * sw / iw, y * sh / ih

    def _image(self, png: bytes, note: str = "") -> ToolOutput:
        blocks: List[Dict[str, Any]] = []
        if note:
            blocks.append({"type": "text", "text": note})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                   "data": base64.b64encode(png).decode()}})
        return ToolOutput(blocks)

    def run(self, member: str, args: Dict[str, Any]) -> ToolOutput:
        try:
            return self._run(member, args or {})
        except Exception as exc:  # the model gets a clear error and can adapt
            return ToolOutput.error(f"{member} failed: {exc}")

    def _run(self, member: str, a: Dict[str, Any]) -> ToolOutput:
        b = self.backend
        mods = parse_combo(str(a.get("text") or ""))[0] if member not in ("type", "key", "hold_key") else []
        if member == "screenshot":
            _, size = self._shot_size()
            return self._image(b.capture(None, size))
        if member == "zoom":
            region = a.get("region")
            if not (isinstance(region, (list, tuple)) and len(region) == 4):
                raise ValueError("region must be [x0, y0, x1, y1]")
            x0, y0 = self._to_screen(region[:2])
            x1, y1 = self._to_screen(region[2:])
            if x1 <= x0 or y1 <= y0:
                raise ValueError("region must have x1 > x0 and y1 > y0")
            w, h = x1 - x0, y1 - y0
            return self._image(b.capture((int(x0), int(y0), int(w) or 1, int(h) or 1),
                                         fit(int(w * 2) or 1, int(h * 2) or 1, self.max_w, self.max_h)))
        if member == "cursor_position":
            (sw, sh), (iw, ih) = self._shot_size()
            x, y = b.cursor()
            return ToolOutput(f"X={round(x * iw / sw)},Y={round(y * ih / sh)}")
        if member == "wait":
            time.sleep(max(0.0, min(float(a.get("duration") or 1), 300)))
            return ToolOutput("OK")
        if member == "mouse_move":
            b.move(*self._to_screen(a.get("coordinate")))
            return ToolOutput("OK")
        if member in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
            x, y = self._to_screen(a["coordinate"]) if a.get("coordinate") is not None else b.cursor()
            button = {"right_click": "right", "middle_click": "middle"}.get(member, "left")
            count = {"double_click": 2, "triple_click": 3}.get(member, 1)
            b.click(x, y, button, count, mods)
            return ToolOutput("OK")
        if member == "left_click_drag":
            b.drag(self._to_screen(a.get("start_coordinate")), self._to_screen(a.get("coordinate")), mods)
            return ToolOutput("OK")
        if member in ("left_mouse_down", "left_mouse_up"):
            x, y = b.cursor()
            b.button(x, y, member == "left_mouse_down")
            return ToolOutput("OK")
        if member == "scroll":
            direction = str(a.get("scroll_direction") or "down")
            amount = max(1, min(int(a.get("scroll_amount") or 3), 50))
            x, y = self._to_screen(a["coordinate"]) if a.get("coordinate") is not None else b.cursor()
            dx, dy = {"up": (0, amount), "down": (0, -amount), "left": (amount, 0), "right": (-amount, 0)}.get(
                direction, (0, -amount))
            b.scroll(x, y, dx, dy, mods)
            return ToolOutput("OK")
        if member == "type":
            text = str(a.get("text") or "")
            if len(text) > 4000:
                raise ValueError("text is too long to type in one go")
            b.type_text(text)
            return ToolOutput("OK")
        if member in ("key", "hold_key"):
            kmods, key = parse_combo(str(a.get("text") or ""))
            if key is not None and key not in KEYCODES and len(key) != 1:
                raise ValueError(f"unknown key {key!r}")
            if member == "hold_key":
                b.key(kmods, key, hold=max(0.0, min(float(a.get("duration") or 1), 300)))
            else:
                for _ in range(max(1, min(int(a.get("repeat") or 1), 100))):
                    b.key(kmods, key)
            return ToolOutput("OK")
        raise ValueError(f"unknown action {member}")


class MacBackend(Backend):
    """Quartz events and screencapture. Imported only on macOS."""

    def __init__(self, tmp_dir: Optional[str] = None):
        import Quartz  # noqa: F401  (fail early if PyObjC is missing)
        self.Q = Quartz
        self.tmp_dir = tmp_dir

    def screen_size(self) -> Tuple[int, int]:
        bounds = self.Q.CGDisplayBounds(self.Q.CGMainDisplayID())
        return int(bounds.size.width), int(bounds.size.height)

    def capture(self, region, size) -> bytes:
        fd, path = tempfile.mkstemp(prefix="argus-shot-", suffix=".png", dir=self.tmp_dir)
        os.close(fd)
        try:
            cmd = ["screencapture", "-x", "-t", "png", "-m"]
            if region:
                cmd += ["-R", ",".join(str(v) for v in region)]
            subprocess.run(cmd + [path], check=True, timeout=15, capture_output=True)
            subprocess.run(["sips", "-z", str(size[1]), str(size[0]), path], check=True, timeout=15,
                           capture_output=True)
            with open(path, "rb") as fh:
                data = fh.read()
            if len(data) < 100:
                raise RuntimeError("empty screenshot: grant Screen Recording to Argus in System Settings")
            return data
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _flags(self, mods: Sequence[str]) -> int:
        Q = self.Q
        table = {"cmd": Q.kCGEventFlagMaskCommand, "shift": Q.kCGEventFlagMaskShift,
                 "option": Q.kCGEventFlagMaskAlternate, "ctrl": Q.kCGEventFlagMaskControl,
                 "fn": Q.kCGEventFlagMaskSecondaryFn}
        flags = 0
        for m in mods:
            flags |= table.get(m, 0)
        return flags

    def _post(self, event, flags: int = 0) -> None:
        if flags:
            self.Q.CGEventSetFlags(event, flags)
        self.Q.CGEventPost(self.Q.kCGHIDEventTap, event)

    def move(self, x, y):
        Q = self.Q
        self._post(Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (x, y), Q.kCGMouseButtonLeft))

    def click(self, x, y, button, count, mods):
        Q = self.Q
        down, up, btn = {"left": (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGMouseButtonLeft),
                         "right": (Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp, Q.kCGMouseButtonRight),
                         "middle": (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGMouseButtonCenter)}[button]
        flags = self._flags(mods)
        self.move(x, y)
        time.sleep(0.03)
        for n in range(1, count + 1):
            for kind in (down, up):
                ev = Q.CGEventCreateMouseEvent(None, kind, (x, y), btn)
                Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, n)
                self._post(ev, flags)
            time.sleep(0.02)

    def button(self, x, y, down):
        Q = self.Q
        kind = Q.kCGEventLeftMouseDown if down else Q.kCGEventLeftMouseUp
        self._post(Q.CGEventCreateMouseEvent(None, kind, (x, y), Q.kCGMouseButtonLeft))

    def drag(self, start, end, mods):
        Q = self.Q
        flags = self._flags(mods)
        self.move(*start)
        self._post(Q.CGEventCreateMouseEvent(None, Q.kCGEventLeftMouseDown, start, Q.kCGMouseButtonLeft), flags)
        steps = 20
        for i in range(1, steps + 1):
            p = (start[0] + (end[0] - start[0]) * i / steps, start[1] + (end[1] - start[1]) * i / steps)
            self._post(Q.CGEventCreateMouseEvent(None, Q.kCGEventLeftMouseDragged, p, Q.kCGMouseButtonLeft), flags)
            time.sleep(0.01)
        self._post(Q.CGEventCreateMouseEvent(None, Q.kCGEventLeftMouseUp, end, Q.kCGMouseButtonLeft), flags)

    def scroll(self, x, y, dx, dy, mods):
        Q = self.Q
        self.move(x, y)
        self._post(Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine, 2, dy, dx), self._flags(mods))

    def type_text(self, text):
        Q = self.Q
        for i in range(0, len(text), 16):
            chunk = text[i:i + 16]
            for down in (True, False):
                ev = Q.CGEventCreateKeyboardEvent(None, 0, down)
                # The length is counted in UTF-16 units, so emoji (two units each) aren't cut short.
                Q.CGEventKeyboardSetUnicodeString(ev, len(chunk.encode("utf-16-le")) // 2, chunk)
                self._post(ev)
            time.sleep(0.01)

    def key(self, mods, key, hold=0.0):
        Q = self.Q
        flags = self._flags(mods)
        code = KEYCODES.get(key, None) if key else None
        if key and code is None:   # a single character with no fixed key: type it
            self.type_text(key)
            return
        mod_codes = [{"cmd": 55, "shift": 56, "option": 58, "ctrl": 59, "fn": 63}[m] for m in mods if m != "fn"]
        for mc in mod_codes:
            self._post(Q.CGEventCreateKeyboardEvent(None, mc, True), flags)
        if code is not None:
            self._post(Q.CGEventCreateKeyboardEvent(None, code, True), flags)
            if hold:
                time.sleep(hold)
            self._post(Q.CGEventCreateKeyboardEvent(None, code, False), flags)
        elif hold:
            time.sleep(hold)
        for mc in reversed(mod_codes):
            self._post(Q.CGEventCreateKeyboardEvent(None, mc, False))

    def cursor(self):
        loc = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        return float(loc.x), float(loc.y)


def check_permissions(prompt: bool = False) -> Dict[str, bool]:
    """Whether this process (Argus.app) may record the screen and send clicks and keys (Accessibility).
    With prompt=True, macOS shows its request dialogs for whatever is missing (it does so only once)."""
    result = {"screen_recording": False, "accessibility": False}
    try:
        import Quartz
        granted = bool(Quartz.CGPreflightScreenCaptureAccess())
        if not granted and prompt:
            granted = bool(Quartz.CGRequestScreenCaptureAccess())
        result["screen_recording"] = granted
    except Exception:
        pass
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        result["accessibility"] = bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: bool(prompt)}))
    except Exception:
        pass
    return result


def request_permissions() -> Dict[str, bool]:
    """Ask macOS for Screen Recording and Accessibility (shows the system prompts once)."""
    return check_permissions(prompt=True)
