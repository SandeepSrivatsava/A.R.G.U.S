"""Argus's voice: turns streamed reply text into sentences and speaks them on the Mac.

Default engine is Piper (on-device neural voice via sherpa-onnx); macOS `say` is the fallback.
Neither sends text anywhere.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, List, Optional, Tuple

import numpy as np

log = logging.getLogger("argus.voice")

_CODE_BLOCK = re.compile(r"```.*?(```|$)", re.S)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍⭐⬆↔-⇿]")
_MD_MARKS = re.compile(r"(\*\*|__|\*|`|^#+\s*|^\s*>\s?|^\s*[-*•]\s+)", re.M)
_ABBREVIATIONS = ("mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "u.s", "u.k", "a.m", "p.m", "no", "approx")


def speakable(text: str) -> str:
    """Strip what shouldn't be read aloud: markdown, links, emoji, code."""
    text = _CODE_BLOCK.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("a link", text)
    text = _MD_MARKS.sub("", text)
    text = _EMOJI.sub("", text)
    text = text.replace("|", " ").replace("—", ", ").replace("&", " and ")
    return " ".join(text.split()).strip()


def _boundary(buf: str) -> int:
    """Index just past the first sentence end in `buf`, or -1."""
    for m in re.finditer(r"[.!?]+[\"')\]]*(?=\s)|\n", buf):
        end = m.end()
        if m.group() == "\n":
            return end
        word = re.search(r"([A-Za-z.]+)[.!?]+[\"')\]]*$", buf[:end])
        if word and word.group(1).lower().rstrip(".") in _ABBREVIATIONS:
            continue
        return end
    return -1


class SentenceBuffer:
    """Collects streamed text deltas and releases whole sentences, so speech can start early. The very
    first piece may be a clause ("The top story today, ...") so a long opening sentence doesn't hold up
    the first word."""

    def __init__(self, max_chars: int = 260, first_clause_chars: int = 60):
        self.buf = ""
        self.max_chars = max_chars
        self.first_clause_chars = first_clause_chars
        self.started = False

    def feed(self, delta: str) -> List[str]:
        self.buf += delta
        out: List[str] = []
        while True:
            cut = _boundary(self.buf)
            if cut < 0:
                break
            out.append(self.buf[:cut])
            self.buf = self.buf[cut:]
        if not out and not self.started and len(self.buf) >= self.first_clause_chars:
            cut = max(self.buf.rfind(mark, 0, self.max_chars) for mark in (", ", "; ", ": ", " - "))
            if cut >= 25:
                out.append(self.buf[:cut + 1])
                self.buf = self.buf[cut + 1:]
        if len(self.buf) > self.max_chars:  # a very long sentence: break at a comma or space
            cut = max(self.buf.rfind(", ", 0, self.max_chars), self.buf.rfind(" ", 0, self.max_chars))
            if cut > 40:
                out.append(self.buf[:cut + 1])
                self.buf = self.buf[cut + 1:]
        spoken = [s for s in (speakable(x) for x in out) if s]
        self.started = self.started or bool(spoken)
        return spoken

    def flush(self) -> List[str]:
        rest, self.buf = self.buf, ""
        text = speakable(rest)
        return [text] if text else []


# ---- engines ---------------------------------------------------------------------------------------
class Voice:
    def speak(self, text: str) -> None:  # blocks until done or stopped
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class _ProcessVoice(Voice):
    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._stopped = False

    def _run(self, args: List[str], stdin_text: Optional[str] = None) -> None:
        with self._lock:
            if self._stopped:
                return
            self._proc = subprocess.Popen(args, stdin=subprocess.PIPE if stdin_text is not None else None,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self._proc.communicate(stdin_text.encode() if stdin_text is not None else None)
        finally:
            with self._lock:
                self._proc = None

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def reset(self) -> None:
        self._stopped = False


class SayVoice(_ProcessVoice):
    """macOS built-in speech (`say`). Text goes in on stdin, never on the command line."""

    def __init__(self, voice: str = "Daniel", rate: int = 190):
        super().__init__()
        self.args = ["say", "-r", str(int(rate))] + (["-v", voice] if voice else [])

    def speak(self, text: str) -> None:
        self._run(self.args, text)

    def synthesize(self, text: str) -> Tuple[np.ndarray, int]:
        """The sound as samples, for playing it somewhere else (into a phone call)."""
        import wave
        with tempfile.TemporaryDirectory(prefix="argus-say-") as tmp:
            out = Path(tmp) / "speech.wav"
            subprocess.run(self.args + ["-o", str(out), "--file-format=WAVE", "--data-format=LEI16@22050"],
                           input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, check=True)
            with wave.open(str(out)) as w:
                data, rate = w.readframes(w.getnframes()), w.getframerate()
        return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0, rate


class PiperVoice(_ProcessVoice):
    """On-device neural voice (Piper model through sherpa-onnx), played with afplay."""

    def __init__(self, model_dir: str, speed: float = 1.0, player: Optional[List[str]] = None,
                 tmp_dir: Optional[str] = None, deterministic: bool = False):
        super().__init__()
        import sherpa_onnx as so
        d = Path(model_dir).expanduser()
        onnx = sorted(p for p in d.glob("*.onnx"))
        if not onnx:
            raise FileNotFoundError(f"no voice model in {d}")
        cfg = so.OfflineTtsConfig(model=so.OfflineTtsModelConfig(
            vits=so.OfflineTtsVitsModelConfig(model=str(onnx[0]), tokens=str(d / "tokens.txt"),
                                              data_dir=str(d / "espeak-ng-data"),
                                              # tests want identical audio every time
                                              **({"noise_scale": 0.0, "noise_scale_w": 0.0} if deterministic else {})),
            num_threads=2), max_num_sentences=1)
        self._tts = so.OfflineTts(cfg)
        self.speed = speed
        self.player = player or (["afplay"] if shutil.which("afplay") else ["aplay", "-q"])
        self.tmp_dir = tmp_dir

    def synthesize(self, text: str) -> Tuple[np.ndarray, int]:
        audio = self._tts.generate(text, sid=0, speed=self.speed)
        return np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate)

    def speak(self, text: str) -> None:
        from .hearing import write_wav
        samples, rate = self.synthesize(text)
        if samples.size == 0:
            return
        fd, path = tempfile.mkstemp(prefix="argus-say-", suffix=".wav", dir=self.tmp_dir)
        os.close(fd)
        try:
            write_wav(path, samples, rate)
            self._run(self.player + [path])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class RecordingVoice(Voice):
    """Test double: records what would have been spoken."""

    def __init__(self, seconds_per_sentence: float = 0.0):
        self.spoken: List[str] = []
        self.delay = seconds_per_sentence
        self._stop = threading.Event()

    def speak(self, text: str) -> None:
        self.spoken.append(text)
        self._stop.clear()
        if self.delay:
            self._stop.wait(self.delay)

    def synthesize(self, text: str) -> Tuple[np.ndarray, int]:
        self.spoken.append(text)
        return np.full(1600, 0.05, dtype=np.float32), 16000

    def stop(self) -> None:
        self._stop.set()


def make_voice(engine: str, *, say_voice: str = "Daniel", rate: int = 190, piper_dir: str = "",
               speed: float = 1.0, tmp_dir: Optional[str] = None) -> Voice:
    if engine == "piper":
        try:
            return PiperVoice(piper_dir, speed=speed, tmp_dir=tmp_dir)
        except Exception as exc:  # missing model or library: fall back rather than go mute
            log.warning("Piper voice unavailable (%s); using macOS say", exc)
    return SayVoice(say_voice, rate)


# ---- the speaking queue ----------------------------------------------------------------------------
class Speaker:
    """Speaks sentences one after another on a worker thread; stop() silences it immediately."""

    def __init__(self, voice: Voice, echo_seconds: float = 15.0, clock: Callable[[], float] = time.monotonic):
        self.voice = voice
        self.clock = clock
        self.echo_seconds = echo_seconds
        self._queue: "queue.Queue[Tuple[int, str]]" = queue.Queue()
        self.muted = False
        self._generation = 0
        self._busy = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._recent: Deque[Tuple[float, str]] = deque(maxlen=20)
        self._last_end = float("-inf")
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._work, name="argus-speaker", daemon=True)
        self._thread.start()

    @property
    def speaking(self) -> bool:
        return not self._idle.is_set()

    def say(self, text: str) -> None:
        text = speakable(text)
        if not text or self.muted:      # muted during a phone call: the call would hear it
            return
        with self._lock:
            self._idle.clear()
            self._queue.put((self._generation, text))

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            if not self._busy.is_set():
                self._idle.set()
        self.voice.stop()

    def quiet_for(self) -> float:
        """Seconds since Argus last stopped talking (0 while talking)."""
        return 0.0 if self.speaking else self.clock() - self._last_end

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        return self._idle.wait(timeout)

    def recently_spoken(self) -> List[str]:
        cutoff = self.clock() - self.echo_seconds
        return [s for t, s in list(self._recent) if t >= cutoff]

    def _work(self) -> None:
        while True:
            generation, text = self._queue.get()
            with self._lock:
                if generation != self._generation:
                    if self._queue.empty():
                        self._idle.set()
                    continue
                self._busy.set()
            self._recent.append((self.clock(), text))
            try:
                if hasattr(self.voice, "reset"):
                    self.voice.reset()
                self.voice.speak(text)
            except Exception:
                log.exception("speaking failed")
            finally:
                with self._lock:
                    self._busy.clear()
                    self._last_end = self.clock()
                    if self._queue.empty():
                        self._idle.set()
