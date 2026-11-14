"""Echo cancellation: hearing you clearly while Argus talks, and not hearing itself.

On a laptop the speakers are close to the microphone, so Argus's own voice comes back in loud. That made
talking over it slow and unreliable (it had to tell your voice from its own by voiceprint), and sometimes
made it react to its own words. The argus-ear helper (macos/argus-ear.swift) turns on Apple's voice
processing, the echo cancellation and noise suppression FaceTime uses, and plays Argus's voice through the
same audio engine, so the canceller knows exactly what to take out of the microphone.

If the helper isn't built, can't start, or stops sending sound, Argus quietly goes back to the plain
microphone and afplay, as before. [listening] echo_cancel = false turns it off.
"""
from __future__ import annotations

import itertools
import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

import numpy as np

from .hearing import SAMPLE_RATE

log = logging.getLogger("argus.ear")


class Ear:
    """The argus-ear helper: the microphone (after echo cancellation) and a speaker for Argus's voice."""

    def __init__(self, binary: Path, *, voice_rate: int = 24000, popen=subprocess.Popen, ready_timeout: float = 6.0,
                 block: int = 1024, max_backlog_seconds: float = 30.0, on_error: Callable[[str], None] = lambda m: None):
        self.binary, self.voice_rate, self.popen = Path(binary), voice_rate, popen
        self.ready_timeout, self.block = ready_timeout, block
        self.on_error = on_error
        self._proc: Optional[subprocess.Popen] = None
        self._ready = threading.Event()
        self._done: Dict[str, threading.Event] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=int(max_backlog_seconds * SAMPLE_RATE / (block // 2)))
        self.last_sound = 0.0
        self.error = ""

    # ---- the helper process ----
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._ready.is_set()

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return True
            if not self.binary.exists():
                self.error = f"{self.binary.name} isn't built yet (scripts/update.sh builds it)"
                return False
            self._ready.clear()
            try:
                proc = self.popen([str(self.binary), str(SAMPLE_RATE), str(self.voice_rate)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            except OSError as exc:
                self.error = f"couldn't start {self.binary.name}: {exc}"
                return False
            self._proc = proc
            threading.Thread(target=self._read_events, args=(proc,), name="argus-ear-events", daemon=True).start()
            threading.Thread(target=self._read_audio, args=(proc,), name="argus-ear-audio", daemon=True).start()
        if self._ready.wait(self.ready_timeout) and proc.poll() is None:
            self.last_sound = time.monotonic()
            log.info("echo cancellation on (argus-ear)")
            return True
        self.error = self.error or "argus-ear didn't get ready"
        self.stop_process()
        return False

    def stop_process(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._ready.clear()
        if proc is not None:
            try:
                proc.stdin.write(b"quit\n")
                proc.stdin.flush()
            except (OSError, ValueError, AttributeError):
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
        for done in list(self._done.values()):
            done.set()                             # nothing more will play

    def _send(self, line: str) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False
        try:
            proc.stdin.write((line + "\n").encode())
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _read_events(self, proc) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line == "ready":
                self._ready.set()
            elif line.startswith("done "):
                done = self._done.get(line[5:].strip())
                if done is not None:
                    done.set()
            elif line.startswith("error "):
                self.error = line[6:]
                log.warning("argus-ear: %s", self.error)
                self.on_error(self.error)
        self._ready.clear()
        for done in list(self._done.values()):
            done.set()

    def _read_audio(self, proc) -> None:
        while True:
            data = proc.stdout.read(self.block)
            if not data:
                break
            samples = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
            if samples.size:
                self.last_sound = time.monotonic()
                try:
                    self._queue.put_nowait(samples)
                except queue.Full:                # fell behind: drop the oldest audio rather than block
                    try:
                        self._queue.get_nowait()
                        self._queue.put_nowait(samples)
                    except (queue.Empty, queue.Full):
                        pass

    # ---- the microphone side (same shape as hearing.Microphone) ----
    def read(self, timeout: float = 0.25) -> Optional[np.ndarray]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    # ---- the speaker side ----
    def play(self, path: str, stopped: Callable[[], bool] = lambda: False, timeout: float = 120.0) -> bool:
        """Play a WAV file through the echo-cancelled speaker; blocks until it's done or stopped. False if the
        helper isn't running (then play it some other way)."""
        if not self.running:
            return False
        key = str(next(self._ids))
        done = threading.Event()
        self._done[key] = done
        try:
            if not self._send(f"play {key} {path}"):
                return False
            deadline = time.monotonic() + timeout
            while not done.wait(0.05):
                if stopped():
                    self.stop()
                    done.wait(1.0)
                    break
                if time.monotonic() > deadline or not self.running:
                    break
            return True
        finally:
            self._done.pop(key, None)

    def stop(self) -> None:
        self._send("stop")


class EchoCancelledMicrophone:
    """The microphone Argus listens with: argus-ear when it works, the plain microphone otherwise."""
    SILENT_AFTER = 4.0          # no sound at all from the helper for this long: it's stuck, use the plain mic

    def __init__(self, ear: Ear, fallback, audit=None):
        self.ear, self.fallback, self.audit = ear, fallback, audit
        self.using_ear = False
        self.gave_up = False
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def overflows(self) -> int:
        return getattr(self.fallback, "overflows", 0)

    def open(self) -> None:
        if self._open:
            return
        if not self.gave_up and self.ear.start():
            self.using_ear = True
        else:
            self._give_up(self.ear.error or "not available")
            self.fallback.open()
        self._open = True

    def _give_up(self, why: str) -> None:
        if not self.gave_up:
            self.gave_up = True
            log.warning("echo cancellation off: %s", why)
            if self.audit is not None:
                self.audit.log("echo_cancel_off", why=why[:200])
        self.using_ear = False

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        if self.using_ear:
            self.ear.stop_process()
            self.ear.drain()
            self.using_ear = False
        else:
            self.fallback.close()

    def read(self, timeout: float = 0.25) -> Optional[np.ndarray]:
        if not self._open:
            return None
        if not self.using_ear:
            return self.fallback.read(timeout)
        chunk = self.ear.read(timeout)
        if chunk is None and (not self.ear.running or time.monotonic() - self.ear.last_sound > self.SILENT_AFTER):
            self.ear.stop_process()                # stuck or gone: carry on with the plain microphone
            self._give_up(self.ear.error or "argus-ear stopped sending sound")
            self.fallback.open()
        return chunk
