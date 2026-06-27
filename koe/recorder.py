"""Microphone capture with preroll.

A single input stream runs continuously while Koe is active. Audio always flows
into a short ring buffer; when you start a take, the recent ~preroll seconds are
prepended so the **first word is never clipped** by driver start-up latency (the
classic "…ello" instead of "Hello"). The same callback tracks the signal level
and peak, cheaply, for level metering and clipping / no-signal detection.

Set preroll_sec=0 (or enable_preroll=False) to fall back to opening the stream
only while recording — no always-on microphone, at the cost of possible head
clipping.
"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np
import sounddevice as sd


class Recorder:
    def __init__(
        self,
        sample_rate: int = 16000,
        input_device: int | None = None,
        preroll_sec: float = 0.3,
        enable_preroll: bool = True,
    ):
        self.sample_rate = sample_rate
        self.input_device = input_device
        self.preroll_sec = max(0.0, preroll_sec)
        self.enable_preroll = enable_preroll and self.preroll_sec > 0
        self._preroll_samples = int(self.preroll_sec * sample_rate)

        self._lock = threading.Lock()
        self._recording = False
        self._frames: list[np.ndarray] = []
        # Ring of recent pre-trigger audio (only used in preroll mode).
        self._ring: deque[np.ndarray] = deque()
        self._ring_samples = 0
        # Cheap running signal stats for the meter / diagnostics.
        self._level = 0.0   # RMS of the most recent block (0..~1)
        self._peak = 0.0     # max |sample| since the take started
        self._stream: sd.InputStream | None = None

    # --- audio callback ---------------------------------------------------
    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        chunk = indata.copy().reshape(-1)
        if chunk.size:
            self._level = float(np.sqrt(np.mean(chunk * chunk)))
        with self._lock:
            if self._recording:
                self._frames.append(chunk)
                if chunk.size:
                    self._peak = max(self._peak, float(np.max(np.abs(chunk))))
            elif self.enable_preroll:
                self._ring.append(chunk)
                self._ring_samples += chunk.size
                # Trim the ring to ~preroll length.
                while (self._ring
                       and self._ring_samples - self._ring[0].size >= self._preroll_samples):
                    self._ring_samples -= self._ring.popleft().size

    def _open_stream(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self.input_device,
            callback=self._callback,
            blocksize=0,  # let the driver choose (most compatible)
        )
        self._stream.start()

    # --- lifecycle --------------------------------------------------------
    def begin(self) -> None:
        """Start the always-on stream (preroll mode). Call once when Koe starts.
        If the mic can't be opened, fall back to opening it per-take so dictation
        still works (just without preroll)."""
        if self.enable_preroll and self._stream is None:
            try:
                self._open_stream()
            except Exception:
                self.enable_preroll = False

    def start(self) -> None:
        """Begin a dictation take, seeding it with the preroll audio."""
        with self._lock:
            self._peak = 0.0
            if self.enable_preroll:
                pre = (np.concatenate(list(self._ring)) if self._ring
                       else np.zeros(0, dtype=np.float32))
                if pre.size > self._preroll_samples:
                    pre = pre[-self._preroll_samples:]
                self._frames = [pre] if pre.size else []
                self._ring.clear()
                self._ring_samples = 0
                self._recording = True
        if not self.enable_preroll:
            # No always-on mic: open the stream just for this take.
            self._frames = []
            self._recording = True
            self._open_stream()

    def stop(self) -> np.ndarray:
        """End the take and return it (preroll + recorded) as 1-D float32."""
        with self._lock:
            self._recording = False
            chunks = list(self._frames)
            self._frames = []
        if not self.enable_preroll and self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32, copy=False)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # --- diagnostics ------------------------------------------------------
    @property
    def level(self) -> float:
        """RMS level of the latest block (~0 silence .. ~0.3 loud speech)."""
        return self._level

    @property
    def peak(self) -> float:
        """Max |sample| seen during the current/last take (>=0.99 ≈ clipping)."""
        return self._peak

    @staticmethod
    def list_devices() -> str:
        return str(sd.query_devices())
