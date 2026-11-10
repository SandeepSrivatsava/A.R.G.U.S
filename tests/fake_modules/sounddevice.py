"""Stand-in for python-sounddevice in process-level tests: a microphone that hears silence."""
import threading

import numpy as np


class InputStream:
    def __init__(self, samplerate, channels, dtype, blocksize, device=None, callback=None):
        self.blocksize, self.callback = blocksize, callback
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        def run():
            block = np.zeros((self.blocksize, 1), dtype=np.float32)
            while not self._stop.wait(self.blocksize / 16000):
                self.callback(block, self.blocksize, None, None)
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def close(self):
        self._stop.set()
