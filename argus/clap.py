"""Clap to wake Argus. Ported from adewaskar/jarvis (src/lib/clap.ts, MIT, Aditya Dewaskar) to work
on the microphone stream in Python, and extended to need two claps by default (one clap is too easy
to fake by accident: a door, a dropped book).

A clap is not loud, it is SHARP: silence just before, a near-instant rise, and a collapse within about
a tenth of a second. Speech, music and doors fail at least one of those tests.
"""
from __future__ import annotations

from collections import deque
from typing import Callable, Optional

import numpy as np

FRAME = 256                 # 16 ms at 16 kHz
PEAK_OVER_FLOOR = 7.0       # how far above the room's noise floor a candidate must rise
MIN_PEAK = 0.055            # ... and an absolute floor, so a silent room can't make a whisper a clap
QUIET_BEFORE = 0.16         # the room must have been this quiet (relative to the peak) just before
LOOKBACK = 5                # frames back that "just before" means (~80 ms)
DECAY_TO = 0.35             # confirmed when the level falls this far below the peak...
DECAY_MS = 130              # ...within this long
GIVE_UP_MS = 260            # still loud after this: something sustained, not a clap
GAP_MIN_MS, GAP_MAX_MS = 120, 900   # two claps must be this far apart
QUIET_AROUND_MS = 600       # ...with no other clap shortly before the first one,
CONFIRM_MS = 400            # ...and none shortly after the last one (so applause and rhythms don't count)
REFRACTORY_MS = 1500        # after a trigger, ignore claps for this long
# Added for the microphone (Argus): a clap is broadband noise with an attack of a few milliseconds;
# voices and music are mostly low-frequency harmonics that swell over tens of milliseconds.
HIGH_FREQ_SHARE = 0.25      # share of the strike's energy above 2 kHz
ATTACK_MS = 6.0             # from a quarter of the peak to the peak, at most
SETTLE_FRAMES = 4           # ...and the room stays quiet for ~64 ms after the strike


class ClapDetector:
    def __init__(self, mode: str = "double", on_clap: Optional[Callable[[], None]] = None):
        self.mode = mode
        self.on_clap = on_clap
        self.history: deque = deque(maxlen=20)
        self.floor = 0.01
        self.candidate: Optional[list] = None   # [start_ms, peak]
        self.settling = None                    # (peak, frames left) while checking the quiet after a strike
        self.noisy = False                      # sound between two claps disqualifies the pair
        self.now_ms = 0.0
        self.last_clap_ms = -1e9
        self.clap_times: deque = deque(maxlen=4)
        self.pending_ms: Optional[float] = None
        self.quiet_until = -1e9
        self._rest = np.zeros(0, dtype=np.float32)
        self._samples = np.zeros(FRAME, dtype=np.float32)
        self.claps = 0

    def feed(self, chunk: np.ndarray) -> bool:
        """Feed microphone samples (16 kHz float). Returns True when a (double) clap is recognised."""
        if self.mode == "off":
            return False
        data = np.concatenate([self._rest, np.asarray(chunk, dtype=np.float32)])
        fired = False
        n = len(data) // FRAME
        for i in range(n):
            frame = data[i * FRAME:(i + 1) * FRAME]
            self._samples = frame
            fired |= self._frame(float(np.sqrt(np.mean(frame * frame))))
        self._rest = data[n * FRAME:]
        return fired

    def _frame(self, level: float) -> bool:
        self.now_ms += FRAME / 16.0
        before = list(self.history)
        self.history.append(level)
        if self.pending_ms is not None and self.now_ms >= self.pending_ms:
            self.pending_ms = None
            self.quiet_until = self.now_ms + REFRACTORY_MS
            self.clap_times.clear()
            if self.on_clap:
                self.on_clap()
            return True
        if self.candidate is None and self.settling is None and level < self.floor * 3:  # track the room
            self.floor = max(self.floor + (level - self.floor) * 0.05, 0.002)
        if self.settling is not None:
            # After the strike the room must stay quiet for a moment: a voice carries on after a "p" or "k".
            peak, frames_left = self.settling
            if level > peak * DECAY_TO:
                self.settling = None
                self.noisy = True
                return False
            if frames_left <= 1:
                self.settling = None
                return self._clap()
            self.settling = (peak, frames_left - 1)
            return False
        if self.candidate is not None:
            start, peak = self.candidate
            if level < peak * DECAY_TO and self.now_ms - start < DECAY_MS:
                self.candidate = None
                self.settling = (peak, SETTLE_FRAMES)
            elif self.now_ms - start > GIVE_UP_MS:
                self.candidate = None
                self.noisy = True
            else:
                self.candidate[1] = max(peak, level)
            return False
        if self._strike(level, before):
            self.candidate = [self.now_ms, level]
        elif level > max(self.floor * 4, MIN_PEAK * 0.5):
            self.noisy = True          # something else is going on between claps
        return False

    def _strike(self, level: float, before: list) -> bool:
        """Is this frame the start of a clap?"""
        if self.now_ms - self.last_clap_ms < GAP_MIN_MS:
            return False
        if level < MIN_PEAK or level < self.floor * PEAK_OVER_FLOOR:
            return False
        if len(before) < LOOKBACK or before[-LOOKBACK] > level * QUIET_BEFORE:
            return False
        if self.now_ms - self.last_clap_ms > GAP_MAX_MS:
            # The first clap of a pair needs a quiet room for a third of a second before it.
            if len(before) < 15 or float(np.mean(before[-15:])) > max(self.floor * 3, MIN_PEAK * 0.3):
                return False
        return self._sharp_and_broadband(self._samples)

    @staticmethod
    def _sharp_and_broadband(frame: np.ndarray) -> bool:
        spectrum = np.abs(np.fft.rfft(frame * np.hanning(len(frame)))) ** 2
        freqs = np.fft.rfftfreq(len(frame), 1 / 16000)
        total = float(spectrum.sum()) or 1e-12
        if float(spectrum[freqs >= 2000].sum()) / total < HIGH_FREQ_SHARE:
            return False
        win = 32                                          # 2 ms windows
        env = np.sqrt(np.mean(frame[: len(frame) // win * win].reshape(-1, win) ** 2, axis=1))
        peak_at = int(np.argmax(env))
        above = np.nonzero(env[: peak_at + 1] >= env[peak_at] * 0.25)[0]
        return (peak_at - int(above[0])) * 2.0 <= ATTACK_MS

    def _clap(self) -> bool:
        self.claps += 1
        noisy, self.noisy = self.noisy, False
        self.last_clap_ms = self.now_ms
        times = self.clap_times
        if noisy:
            times.clear()
        times.append(self.now_ms)
        if self.pending_ms is not None or self.now_ms < self.quiet_until:
            # another clap right after a (double) clap, or during the quiet period: applause or a rhythm
            self.pending_ms = None
            self.quiet_until = max(self.quiet_until, self.now_ms + 1000)
            return False
        if self.mode == "single":
            ok = len(times) < 2 or times[-1] - times[-2] > QUIET_AROUND_MS
        else:
            ok = (len(times) >= 2 and GAP_MIN_MS <= times[-1] - times[-2] <= GAP_MAX_MS
                  and (len(times) < 3 or times[-2] - times[-3] > QUIET_AROUND_MS))
        if ok:
            self.pending_ms = self.now_ms + CONFIRM_MS
        return False
