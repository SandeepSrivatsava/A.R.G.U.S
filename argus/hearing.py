"""Microphone -> speech segments -> text, all on the Mac (sherpa-onnx: Silero VAD + Moonshine).

Audio is kept in memory only. Nothing here writes sound to disk or sends it over the network.
"""
from __future__ import annotations

import collections
import logging
import os
import queue
import threading
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

log = logging.getLogger("argus.hearing")

SAMPLE_RATE = 16000


def resample(samples: np.ndarray, rate: int, target: int = SAMPLE_RATE) -> np.ndarray:
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if rate == target or samples.size == 0:
        return samples
    n = int(round(samples.size * target / rate))
    return np.interp(np.linspace(0, samples.size - 1, n), np.arange(samples.size), samples).astype(np.float32)


def read_wav(path: str) -> np.ndarray:
    """Mono float32 at 16 kHz from a 16-bit PCM WAV file."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM WAV is supported")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return resample(data, rate)


def write_wav(path: str, samples: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


# ---- audio sources -----------------------------------------------------------------------------
class Microphone:
    """Live microphone via PortAudio (sounddevice). macOS shows the orange mic dot while open."""

    def __init__(self, device: Optional[str] = None, block: int = 512, max_backlog_seconds: float = 30.0):
        self.device = device or None
        self.block = block
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=int(max_backlog_seconds * SAMPLE_RATE / block))
        self._stream = None
        self._lock = threading.Lock()
        self.overflows = 0

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, frames, time_info, status):  # PortAudio thread: keep it tiny
        if status:
            self.overflows += 1
        try:
            self._queue.put_nowait(indata[:, 0].copy())
        except queue.Full:  # processing fell behind: drop the oldest audio rather than block
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(indata[:, 0].copy())
            except (queue.Empty, queue.Full):
                pass

    def open(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            import sounddevice as sd  # imported lazily: needs PortAudio (bundled in the macOS wheel)
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=self.block,
                                          device=self.device, callback=self._callback)
            self._stream.start()
            log.info("microphone open")

    def close(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
            log.info("microphone closed")
        while not self._queue.empty():
            self._queue.get_nowait()

    def read(self, timeout: float = 0.25) -> Optional[np.ndarray]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class AudioFile:
    """Stands in for the microphone in tests and `argus listen-file`: plays WAV files into the pipeline."""

    def __init__(self, paths: List[str], block: int = 512, gap_seconds=1.0,
                 on_chunk: Optional[Callable[[], None]] = None):
        gaps = list(gap_seconds) if isinstance(gap_seconds, (list, tuple)) else [gap_seconds] * len(paths)
        audio: List[np.ndarray] = []
        for p, gap in zip(paths, gaps):
            audio.append(read_wav(p))
            audio.append(np.zeros(int(gap * SAMPLE_RATE), dtype=np.float32))
        self._data = np.concatenate(audio) if audio else np.zeros(0, dtype=np.float32)
        self._pos = 0
        self.block = block
        self.on_chunk = on_chunk
        self.is_open = False

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    @property
    def finished(self) -> bool:
        return self._pos >= self._data.size

    def read(self, timeout: float = 0.25) -> Optional[np.ndarray]:
        if self.finished:
            return None
        chunk = self._data[self._pos:self._pos + self.block]
        self._pos += self.block
        if self.on_chunk:
            self.on_chunk()
        return chunk


# ---- voice activity detection --------------------------------------------------------------------
class Segmenter:
    """Cuts the audio stream into utterances with Silero VAD."""

    def __init__(self, model: str, threshold: float = 0.5, min_silence: float = 0.7, min_speech: float = 0.3,
                 max_speech: float = 20.0):
        import sherpa_onnx as so
        cfg = so.VadModelConfig()
        cfg.silero_vad.model = model
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_silence_duration = min_silence
        cfg.silero_vad.min_speech_duration = min_speech
        cfg.silero_vad.max_speech_duration = max_speech
        cfg.sample_rate = SAMPLE_RATE
        cfg.num_threads = 1
        self._vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=max(60.0, max_speech + 10))   # long talk (a TV) grows it anyway
        self._window = cfg.silero_vad.window_size
        self._pending = np.zeros(0, dtype=np.float32)
        self.recent = collections.deque(maxlen=int(3.0 * SAMPLE_RATE / self._window))  # ~3 s, for barge-in

    def feed(self, chunk: np.ndarray) -> List[np.ndarray]:
        self._pending = np.concatenate([self._pending, np.asarray(chunk, dtype=np.float32)])
        while self._pending.size >= self._window:
            window, self._pending = self._pending[:self._window], self._pending[self._window:]
            self._vad.accept_waveform(window)
            self.recent.append(window)
        return self._drain()

    def flush(self) -> List[np.ndarray]:
        self._vad.flush()
        return self._drain()

    def _drain(self) -> List[np.ndarray]:
        out = []
        while not self._vad.empty():
            out.append(np.array(self._vad.front.samples, dtype=np.float32))
            self._vad.pop()
        return out

    @property
    def speech_active(self) -> bool:
        return bool(self._vad.is_speech_detected())

    def recent_audio(self) -> np.ndarray:
        return np.concatenate(list(self.recent)) if self.recent else np.zeros(0, dtype=np.float32)

    def reset(self) -> None:
        self._vad.reset()
        self._pending = np.zeros(0, dtype=np.float32)
        self.recent.clear()


# ---- speech to text ----------------------------------------------------------------------------
class Transcriber:
    def transcribe(self, samples: np.ndarray) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class SherpaTranscriber(Transcriber):
    """Moonshine (default) or Whisper ONNX models, run on the CPU."""

    def __init__(self, model_dir: str, threads: int = 2):
        import sherpa_onnx as so
        d = Path(model_dir).expanduser()
        if not d.is_dir():
            raise FileNotFoundError(f"speech model folder not found: {d}")
        names = {p.name for p in d.iterdir()}

        def pick(*candidates: str) -> str:
            for c in candidates:
                if c in names:
                    return str(d / c)
            raise FileNotFoundError(f"{d}: missing one of {', '.join(candidates)}")

        if "preprocess.onnx" in names:  # Moonshine
            self._rec = so.OfflineRecognizer.from_moonshine(
                preprocessor=pick("preprocess.onnx"), encoder=pick("encode.int8.onnx", "encode.onnx"),
                uncached_decoder=pick("uncached_decode.int8.onnx", "uncached_decode.onnx"),
                cached_decoder=pick("cached_decode.int8.onnx", "cached_decode.onnx"),
                tokens=pick("tokens.txt"), num_threads=threads)
            self.kind = "moonshine"
        else:  # Whisper export: <name>-encoder(.int8).onnx, <name>-decoder(.int8).onnx, <name>-tokens.txt
            enc = sorted(n for n in names if n.endswith("-encoder.int8.onnx")) or sorted(
                n for n in names if n.endswith("-encoder.onnx"))
            dec = sorted(n for n in names if n.endswith("-decoder.int8.onnx")) or sorted(
                n for n in names if n.endswith("-decoder.onnx"))
            tok = sorted(n for n in names if n.endswith("-tokens.txt"))
            if not (enc and dec and tok):
                raise FileNotFoundError(f"{d}: not a Moonshine or Whisper model folder")
            self._rec = so.OfflineRecognizer.from_whisper(encoder=str(d / enc[0]), decoder=str(d / dec[0]),
                                                         tokens=str(d / tok[0]), language="en", num_threads=threads)
            self.kind = "whisper"
        self._lock = threading.Lock()

    def transcribe(self, samples: np.ndarray) -> str:
        if samples.size < SAMPLE_RATE // 10:
            return ""
        # A little silence on both ends stops the model from swallowing the last syllable
        # (it otherwise often hears a sentence-final "Argus" as "Arg").
        pad = np.zeros(int(0.3 * SAMPLE_RATE), dtype=np.float32)
        audio = np.concatenate([pad[: SAMPLE_RATE // 10], np.asarray(samples, dtype=np.float32), pad])
        with self._lock:
            stream = self._rec.create_stream()
            stream.accept_waveform(SAMPLE_RATE, audio)
            self._rec.decode_stream(stream)
            return stream.result.text.strip()


class ScriptedTranscriber(Transcriber):
    """Test double: returns queued transcripts in order (one per utterance)."""

    def __init__(self, transcripts: List[str]):
        self._items = list(transcripts)

    def transcribe(self, samples: np.ndarray) -> str:
        return self._items.pop(0) if self._items else ""


# ---- whose voice is it? (optional "voice lock") --------------------------------------------------
class VoicePrint:
    """Speaker check with an on-device embedding model. The voiceprint file never leaves the Mac."""

    def __init__(self, model: str, path: str, threshold: float = 0.45):
        import sherpa_onnx as so
        self._ex = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(model=model, num_threads=1))
        self.path = Path(path).expanduser()
        self.threshold = threshold
        self._lock = threading.Lock()
        self._ref: Optional[np.ndarray] = None
        if self.path.exists():
            try:
                ref = np.load(self.path)          # never unpickles: allow_pickle stays off
                if ref.ndim == 1 and ref.size >= 16 and np.isfinite(ref).all():
                    self._ref = ref.astype(np.float32)
                else:
                    log.error("the voiceprint in %s isn't valid; run argusctl enroll again", self.path)
            except (OSError, ValueError) as exc:
                log.error("couldn't read the voiceprint (%s); run argusctl enroll again", exc)

    @property
    def enrolled(self) -> bool:
        return self._ref is not None

    def embed(self, samples: np.ndarray) -> np.ndarray:
        with self._lock:
            stream = self._ex.create_stream()
            stream.accept_waveform(SAMPLE_RATE, np.asarray(samples, dtype=np.float32))
            stream.input_finished()
            v = np.array(self._ex.compute(stream), dtype=np.float32)
        return v / (np.linalg.norm(v) + 1e-9)

    def enroll(self, utterances: List[np.ndarray]) -> None:
        if len(utterances) < 3:
            raise ValueError("record at least 3 sentences")
        ref = np.mean([self.embed(u) for u in utterances], axis=0)
        ref /= np.linalg.norm(ref) + 1e-9
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp.npy")
        np.save(tmp, ref)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._ref = ref

    def score(self, samples: np.ndarray) -> float:
        if self._ref is None:
            return 0.0
        return float(np.dot(self.embed(samples), self._ref))

    def matches(self, samples: np.ndarray) -> bool:
        return self.score(samples) >= self.threshold


def model_paths(models_dir: str) -> Dict[str, str]:
    d = Path(models_dir).expanduser()
    return {
        "vad": str(d / "silero_vad.onnx"),
        "asr": str(d / "asr"),
        "speaker": str(d / "speaker.onnx"),
        "tts": str(d / "tts"),
        "kokoro": str(d / "kokoro"),
    }
