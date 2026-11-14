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
from typing import Any, Dict, Callable, Deque, List, Optional, Tuple

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


class _NeuralVoice(_ProcessVoice):
    """A voice made on the Mac (sherpa-onnx), played with afplay. While one sentence plays, the next one is
    already being made (prepare), so there's no gap between sentences."""

    def _init_playback(self, player: Optional[List[str]], tmp_dir: Optional[str]) -> None:
        self.player = player or (["afplay"] if shutil.which("afplay") else ["aplay", "-q"])
        self.tmp_dir = tmp_dir
        self._ready: Dict[str, "threading.Thread"] = {}
        self._made: Dict[str, Tuple[np.ndarray, int]] = {}
        self._made_lock = threading.Lock()
        self.upcoming: List[str] = []      # set by the Speaker: what comes after the sentence being spoken
        self.timings: Deque[Tuple[float, float, float]] = deque(maxlen=60)   # (when, seconds to make, seconds of sound)
        self._making = threading.Lock()    # one sentence at a time: two at once each take twice as long

    def _timed(self, text: str) -> Tuple[np.ndarray, int]:
        with self._making:
            began = time.monotonic()
            samples, rate = self.synthesize(text)
        if rate and samples.size:
            self.timings.append((began, time.monotonic() - began, samples.size / rate))
        return samples, rate

    def synthesize(self, text: str) -> Tuple[np.ndarray, int]:  # pragma: no cover - interface
        raise NotImplementedError

    def prepare(self, text: str) -> None:
        """Start making `text` now, in the background."""
        with self._made_lock:
            if text in self._ready or text in self._made or len(self._made) > 8:
                return

            def make():
                try:
                    out = self._timed(text)
                except Exception:          # speak() will try again and report it
                    return
                with self._made_lock:
                    self._made[text] = out
            thread = threading.Thread(target=make, name="argus-voice-prepare", daemon=True)
            self._ready[text] = thread
        thread.start()

    def _sound_for(self, text: str) -> Tuple[np.ndarray, int]:
        with self._made_lock:
            thread = self._ready.pop(text, None)
        if thread is not None:
            thread.join(30)
        with self._made_lock:
            made = self._made.pop(text, None)
        return made if made is not None else self._timed(text)

    def stop(self) -> None:
        with self._made_lock:
            self._made.clear()
            self._ready.clear()
        super().stop()

    def speak(self, text: str) -> None:
        from .hearing import write_wav
        samples, rate = self._sound_for(text)
        for later in self.upcoming[:1]:    # this one is ready: make the next one while it plays
            self.prepare(later)
        if samples.size == 0 or self._stopped:
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


# Kokoro's English voices (kokoro-en-v0_19), by speaker number. a = American, b = British; f/m = woman/man.
KOKORO_VOICES = {"af": 0, "af_bella": 1, "af_nicole": 2, "af_sarah": 3, "af_sky": 4, "am_adam": 5,
                 "am_michael": 6, "bf_emma": 7, "bf_isabella": 8, "bm_george": 9, "bm_lewis": 10}


def voice_label(name: str) -> str:
    """"af_sky" -> "American woman" (for lists people choose from)."""
    accent = {"a": "American", "b": "British"}.get(name[:1], "")
    who = {"f": "woman", "m": "man"}.get(name[1:2], "voice")
    return f"{accent} {who}".strip()


def describe_voice(voice: Any) -> str:
    """The voice Argus is speaking with right now, in a few words (status and report)."""
    current = getattr(voice, "current", voice)
    if isinstance(current, KokoroVoice):
        name = next((n for n, sid in KOKORO_VOICES.items() if sid == current.sid), str(current.sid))
        return f"natural ({name}, {current.speed:g}x)"
    if isinstance(current, PiperVoice):
        switched = current is not voice and isinstance(voice, AdaptiveVoice)
        return "quick" + (" (switched: the natural one was too slow here)" if switched else "")
    if isinstance(current, SayVoice):
        return "the Mac's own voice"
    return type(current).__name__


class KokoroVoice(_NeuralVoice):
    """Kokoro (Apache-2.0): a natural, expressive voice made on the Mac, with American voices."""

    def __init__(self, model_dir: str, voice: str = "am_michael", speed: float = 1.2,
                 player: Optional[List[str]] = None, tmp_dir: Optional[str] = None, threads: int = 3):
        super().__init__()
        import sherpa_onnx as so
        d = Path(model_dir).expanduser()
        onnx = sorted(d.glob("model*.onnx"))
        if not onnx or not (d / "voices.bin").exists():
            raise FileNotFoundError(f"no Kokoro voice model in {d}")
        if voice not in KOKORO_VOICES:
            raise ValueError(f"unknown Kokoro voice {voice!r}; one of: {', '.join(KOKORO_VOICES)}")
        cfg = so.OfflineTtsConfig(model=so.OfflineTtsModelConfig(
            kokoro=so.OfflineTtsKokoroModelConfig(model=str(onnx[0]), voices=str(d / "voices.bin"),
                                                  tokens=str(d / "tokens.txt"), data_dir=str(d / "espeak-ng-data")),
            num_threads=threads), max_num_sentences=1)
        self._tts = so.OfflineTts(cfg)
        self.sid = KOKORO_VOICES[voice]
        self.speed = speed
        self._init_playback(player, tmp_dir)

    def synthesize(self, text: str) -> Tuple[np.ndarray, int]:
        audio = self._tts.generate(text, sid=self.sid, speed=self.speed)
        return np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate)


class PiperVoice(_NeuralVoice):
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
        self._init_playback(player, tmp_dir)

    def synthesize(self, text: str) -> Tuple[np.ndarray, int]:
        audio = self._tts.generate(text, sid=0, speed=self.speed)
        return np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate)


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


class AdaptiveVoice:
    """The natural voice when this computer can make it fast enough, otherwise the quick one.

    Making a sentence has to take clearly less time than saying it, or there are gaps between sentences. After
    a few sentences, if the natural voice is too slow here, Argus switches to the quick voice for good (a note
    is left in `marker`; `argusctl voices --natural` removes it, to try the natural voice again)."""
    SAMPLES = 4
    TOO_SLOW = 1.0       # seconds of work per second of speech: more than that leaves gaps between sentences

    def __init__(self, natural, quick, marker: Optional[Path] = None,
                 on_switch: Callable[[float], None] = lambda _rate: None):
        self.natural, self.quick, self.marker, self.on_switch = natural, quick, marker, on_switch
        self.current = natural

    @property
    def timings(self):
        return self.current.timings

    @property
    def upcoming(self) -> List[str]:
        return self.current.upcoming

    @upcoming.setter
    def upcoming(self, value: List[str]) -> None:
        self.current.upcoming = value

    def reset(self) -> None:
        self.current.reset()

    def stop(self) -> None:
        self.current.stop()

    def speak(self, text: str) -> None:
        self.current.speak(text)
        if self.current is self.natural:
            recent = list(self.natural.timings)[1:][-self.SAMPLES:]     # the first one warms the model up
            if len(recent) == self.SAMPLES:
                rate = sorted(made / max(sound, 0.1) for _, made, sound in recent)[self.SAMPLES // 2]
                if rate > self.TOO_SLOW:
                    log.warning("the natural voice is too slow on this computer (%.2f s of work per second of "
                                "speech); switching to the quick voice", rate)
                    self.current = self.quick
                    self.natural.stop()
                    if self.marker is not None:
                        try:
                            self.marker.write_text(f"{rate:.2f}\n")
                        except OSError:
                            pass
                    self.on_switch(rate)


def make_voice(engine: str, *, say_voice: str = "Daniel", rate: int = 190, piper_dir: str = "",
               speed: float = 1.0, tmp_dir: Optional[str] = None, kokoro_dir: str = "",
               kokoro_voice: str = "am_michael", keep_natural: bool = False, on_switch: Callable[[float], None] = lambda _rate: None) -> Voice:
    quick: Optional[Voice] = None
    if engine in ("kokoro", "piper"):
        try:        # Piper's pace at 1.0 is already brisk; go a little easier on the speed-up
            quick = PiperVoice(piper_dir, speed=1.0 + (speed - 1.0) / 2 if engine == "kokoro" else speed, tmp_dir=tmp_dir)
        except Exception as exc:  # missing model or library: fall back rather than go mute
            log.warning("Piper voice unavailable (%s)", exc)
    if engine == "kokoro":
        marker = Path(tmp_dir) / "natural-voice-too-slow" if tmp_dir else None
        if keep_natural:                 # you chose this voice: keep it, small gaps and all
            marker = None
        if marker is not None and marker.exists() and quick is not None:
            log.info("using the quick voice: the natural one was too slow here (%s)", marker)
            return quick
        try:
            natural = KokoroVoice(kokoro_dir, voice=kokoro_voice, speed=speed, tmp_dir=tmp_dir)
            return AdaptiveVoice(natural, quick, marker, on_switch) if quick is not None and not keep_natural else natural
        except Exception as exc:
            log.warning("Kokoro voice unavailable (%s); using the Piper voice", exc)
    if quick is not None:
        return quick
    log.warning("using macOS say")
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

    def voice_timings(self, since: float) -> List[Tuple[float, float]]:
        """(seconds to make, seconds of sound) for each sentence made since `since` (time.monotonic)."""
        return [(round(made, 2), round(sound, 2)) for at, made, sound in list(getattr(self.voice, "timings", ()))
                if at >= since]

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
                if hasattr(self.voice, "upcoming"):
                    with self._lock:
                        self.voice.upcoming = [t for g, t in list(self._queue.queue)[:1] if g == self._generation]
                self.voice.speak(text)
            except Exception:
                log.exception("speaking failed")
            finally:
                with self._lock:
                    self._busy.clear()
                    self._last_end = self.clock()
                    if self._queue.empty():
                        self._idle.set()
