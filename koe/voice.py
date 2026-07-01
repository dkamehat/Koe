"""Local text-to-speech for Koe Talk — the ④ of the conversation loop.

Mirrors the ③ refiner's backend pattern exactly (D16/D22): "auto" probes a
LOCAL server and degrades gracefully, one visible notice per fallback, and no
backend error may ever crash the conversation — it just gets quieter:

    VOICEVOX (127.0.0.1:50021)  →  Windows SAPI5 (pyttsx3)  →  text-only

VOICEVOX is the Ollama of TTS: a free local server the user may already run,
excellent Japanese, HTTP API (`/audio_query` + `/synthesis`). SAPI5 ships with
Windows (mediocre but universal). Both return WAV bytes here so playback is a
single interruptible path in talk.py (sounddevice, chunked, epoch-checked).

Synthesis methods must be called from ONE thread (talk.py's TTS worker):
pyttsx3/SAPI is COM-backed and thread-confined — creating the engine lazily on
the calling thread is what makes that safe.
"""

from __future__ import annotations

import io
import sys
import wave

import numpy as np
import requests


def wav_bytes_to_float32(data: bytes) -> tuple[np.ndarray, int]:
    """WAV bytes -> (float32 mono in [-1,1], sample_rate). Pure (stdlib wave),
    so the parsing/downmix logic is CI-tested. Returns (empty, 0) on anything
    unparseable — callers treat that as "nothing to play"."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            ch, width, rate, n = (w.getnchannels(), w.getsampwidth(),
                                  w.getframerate(), w.getnframes())
            raw = w.readframes(n)
    except Exception:
        return np.zeros(0, dtype=np.float32), 0
    if width == 2:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        a = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:  # 8-bit WAV is unsigned
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        return np.zeros(0, dtype=np.float32), 0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a.astype(np.float32, copy=False), rate


class Voice:
    """Base/null backend: no audio — the caller prints the text instead.
    The conversation still works end-to-end (and `--text` mode uses this).

    `last_error` records the most recent synthesis failure so the caller can
    warn visibly instead of degrading in silence (D16)."""

    name = "text"

    def __init__(self):
        self.last_error = ""

    def synth(self, text: str) -> tuple[np.ndarray, int]:  # noqa: ARG002
        return np.zeros(0, dtype=np.float32), 0


class VoicevoxVoice(Voice):
    """Local VOICEVOX server. `speaker` is a style id (GET /speakers lists
    installed voices; 3 = ずんだもん ノーマル in the default install)."""

    name = "voicevox"

    def __init__(self, url: str, speaker: int):
        super().__init__()
        self.url = url.rstrip("/")
        self.speaker = speaker
        # Own session, thread-confined to the TTS worker (same rule as the
        # translator/responder sessions — D05).
        self._session = requests.Session()
        self._session.trust_env = False

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        try:
            q = self._session.post(f"{self.url}/audio_query",
                                   params={"text": text, "speaker": self.speaker},
                                   timeout=10)
            q.raise_for_status()
            r = self._session.post(f"{self.url}/synthesis",
                                   params={"speaker": self.speaker},
                                   json=q.json(), timeout=30)
            r.raise_for_status()
            return wav_bytes_to_float32(r.content)
        except Exception as exc:
            self.last_error = str(exc)
            return np.zeros(0, dtype=np.float32), 0


class SapiVoice(Voice):
    """Windows built-in SAPI5 via pyttsx3 (optional dependency). Synthesizes to
    a temp WAV instead of speaking directly so playback stays on the one
    interruptible sounddevice path. Engine is created lazily on first synth —
    i.e. on the TTS worker thread — because SAPI/COM objects must be used only
    on the thread that created them."""

    name = "sapi"

    def __init__(self):
        super().__init__()
        self._engine = None

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        import os
        import tempfile
        try:
            if self._engine is None:
                import pyttsx3
                self._engine = pyttsx3.init()
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                self._engine.save_to_file(text, path)
                self._engine.runAndWait()
                with open(path, "rb") as fh:
                    return wav_bytes_to_float32(fh.read())
            finally:
                try:
                    os.remove(path)
                except Exception:
                    pass
        except Exception as exc:
            self.last_error = str(exc)
            return np.zeros(0, dtype=np.float32), 0


def _voicevox_available(url: str) -> bool:
    try:
        s = requests.Session()
        s.trust_env = False
        return s.get(f"{url.rstrip('/')}/version", timeout=1.5).ok
    except Exception:
        return False


def _sapi_available() -> bool:
    try:
        import pyttsx3  # noqa: F401
        return sys.platform == "win32"
    except Exception:
        return False


def pick_voice_backend(requested: str, voicevox_ok: bool, sapi_ok: bool) -> str:
    """The fallback-chain decision, pure so it's CI-tested: an explicit request
    is honored if available (else text-only, loudly — a silent stand-in would
    fake the experience, same rationale as bench's refiner warning); "auto"
    walks voicevox -> sapi -> text."""
    r = (requested or "auto").lower()
    if r == "voicevox":
        return "voicevox" if voicevox_ok else "text"
    if r == "sapi":
        return "sapi" if sapi_ok else "text"
    if r in ("text", "none"):
        return "text"
    if voicevox_ok:
        return "voicevox"
    if sapi_ok:
        return "sapi"
    return "text"


def is_unwanted_fallback(requested: str, got: str) -> bool:
    """True when an EXPLICITLY requested backend couldn't be honored (warn) —
    but not for "auto" (any rung is fine) or "none"/"text" (text is the ask).
    Pure so the warning condition is CI-tested."""
    want = (requested or "auto").lower()
    if want == "auto":
        return False
    if want in ("none", "text"):
        want = "text"
    return got != want


def build_voice(cfg, requested: str | None = None) -> Voice:
    want = (requested or getattr(cfg, "voice_backend", "auto") or "auto").lower()
    got = pick_voice_backend(want, _voicevox_available(cfg.voicevox_url),
                             _sapi_available())
    if is_unwanted_fallback(want, got):
        print(f"! TTS backend {want!r} unavailable — replies will be text-only. "
              f"(VOICEVOX: start the app at {cfg.voicevox_url}; SAPI: pip install pyttsx3)",
              file=sys.stderr, flush=True)
    if got == "voicevox":
        return VoicevoxVoice(cfg.voicevox_url, cfg.voicevox_speaker)
    if got == "sapi":
        return SapiVoice()
    return Voice()
