"""Microphone capture.

Records mono 16 kHz audio into an in-memory buffer while the hotkey is held,
then hands the raw float32 samples to the transcription engine.
"""

from __future__ import annotations

import threading

import numpy as np
import sounddevice as sd


class Recorder:
    def __init__(self, sample_rate: int = 16000, input_device: int | None = None):
        self.sample_rate = sample_rate
        self.input_device = input_device
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        # status carries xruns etc.; we keep going but the data is still valid.
        with self._lock:
            self._frames.append(indata.copy())

    def start(self) -> None:
        with self._lock:
            self._frames = []
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self.input_device,
            callback=self._callback,
            blocksize=0,  # let the driver pick a low-latency block size
        )
        self._stream.start()

    def _concat(self) -> np.ndarray:
        with self._lock:
            chunks = list(self._frames)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32, copy=False)

    def snapshot(self) -> np.ndarray:
        """Audio captured so far, WITHOUT stopping — used for live partials."""
        return self._concat()

    def stop(self) -> np.ndarray:
        """Stop recording and return the full take as a 1-D float32 array."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        return self._concat()

    @staticmethod
    def list_devices() -> str:
        return str(sd.query_devices())
