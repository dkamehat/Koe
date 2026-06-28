#!/usr/bin/env python
"""Koe Interpreter — live, on-device captions for *system* audio (WASAPI loopback).

Captures whatever is playing on your speakers (a meeting, a video, a call),
transcribes it locally with the same faster-whisper engine Koe uses for the mic,
and prints rolling captions in the terminal. Nothing leaves the machine.

Sibling of `bench.py` in the Koe series: same engine, different source (the
speaker's WASAPI loopback instead of the mic) and a streaming front-end.

Usage:
    python interpreter.py                 # live captions of the default speaker
    python interpreter.py --list          # list loopback devices you can capture
    python interpreter.py --device Realtek # capture a device whose name contains this
    python interpreter.py --translate      # Whisper speech-translation -> English
    python interpreter.py --threshold 0.01 # raise if silence is misread as speech
    python interpreter.py --debug          # show a live RMS meter to tune --threshold

Stop with Ctrl+C. Requires `PyAudioWPatch` (WASAPI loopback on Windows).

Pipeline (three decoupled stages so capture never stalls on the GPU):
    capture thread ─► raw blocks ─► segmenter (energy VAD) ─► utterances
                                                              ─► transcribe thread ─► captions
faster-whisper isn't streaming, so audio is cut into utterances at short silence
gaps (or a hard length cap) — clean, non-overlapping captions on natural pauses.
The speaker runs at 48 kHz stereo; we downmix + resample to 16 kHz mono for Whisper.
"""

from __future__ import annotations

import queue
import sys
import threading
import time

import numpy as np

SR = 16000              # Whisper's sample rate (we resample loopback down to this)
BLOCK_S = 0.1           # ~0.1 s per captured block (VAD granularity)

# Whisper's stock outputs on near-silence; drop them when the clip is too short
# to plausibly contain them (avoids phantom "Thank you." captions between pauses).
_HALLUCINATION = {
    "thank you.", "thank you", "thanks for watching.", "thanks for watching",
    "you", "bye.", "ご視聴ありがとうございました", "ご視聴ありがとうございました。",
    "ありがとうございました", "ありがとうございました。", "おやすみなさい。",
}


def _pa():
    import pyaudiowpatch as pyaudio
    return pyaudio


def _default_loopback(p):
    pa = _pa()
    wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
    dout = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    for d in p.get_loopback_device_info_generator():
        if dout["name"] in d["name"]:
            return d
    for d in p.get_loopback_device_info_generator():   # fallback: any loopback
        return d
    raise SystemExit("no WASAPI loopback device found (need Windows + PyAudioWPatch).")


def _pick_loopback(p, name_substr: str | None):
    if not name_substr:
        return _default_loopback(p)
    for d in p.get_loopback_device_info_generator():
        if name_substr.lower() in d["name"].lower():
            return d
    raise SystemExit(f"no loopback device matches {name_substr!r} — see `interpreter.py --list`")


def cmd_list() -> None:
    pa = _pa()
    p = pa.PyAudio()
    try:
        default = _default_loopback(p)["name"]
        print("Loopback devices available for capture:\n")
        for d in p.get_loopback_device_info_generator():
            mark = "  <- default" if d["name"] == default else ""
            print(f"  {d['name']}{mark}")
        print("\nUse:  python interpreter.py --device \"<part of the name>\"")
    finally:
        p.terminate()


def _to_16k_mono(raw: bytes, in_rate: int, channels: int) -> np.ndarray:
    """int16 interleaved bytes -> float32 mono @ 16 kHz (downmix + linear resample)."""
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        a = a.reshape(-1, channels).mean(axis=1)
    if in_rate != SR and a.size:
        n_out = int(round(a.size * SR / in_rate))
        if n_out <= 0:
            return np.zeros(0, dtype=np.float32)
        xp = np.linspace(0.0, 1.0, num=a.size, endpoint=False)
        xq = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        a = np.interp(xq, xp, a).astype(np.float32)
    return a


class _Capture(threading.Thread):
    """Producer: stream loopback blocks (resampled to 16 kHz mono) into a queue."""

    def __init__(self, dev: dict, q: "queue.Queue[np.ndarray]"):
        super().__init__(daemon=True)
        self._dev = dev
        self._q = q
        self._running = True

    def run(self) -> None:
        pa = _pa()
        p = pa.PyAudio()
        rate = int(self._dev["defaultSampleRate"])
        ch = int(self._dev["maxInputChannels"])
        chunk = max(1, int(rate * BLOCK_S))
        st = p.open(format=pa.paInt16, channels=ch, rate=rate,
                    frames_per_buffer=chunk, input=True,
                    input_device_index=self._dev["index"])
        try:
            while self._running:
                raw = st.read(chunk, exception_on_overflow=False)
                self._q.put(_to_16k_mono(raw, rate, ch))
        finally:
            st.stop_stream()
            st.close()
            p.terminate()

    def stop(self) -> None:
        self._running = False


def _is_hallucination(text: str, audio: np.ndarray) -> bool:
    return text.strip().lower() in _HALLUCINATION and (len(audio) / SR) < 1.6


class _Transcriber(threading.Thread):
    """Consumer: pull whole utterances and caption them, off the capture path so
    the GPU never stalls audio. One thread => captions stay in spoken order."""

    def __init__(self, engine, task: str, seg_q: "queue.Queue", t0: float):
        super().__init__(daemon=True)
        self._engine = engine
        self._task = task
        self._q = seg_q
        self._t0 = t0

    def run(self) -> None:
        while True:
            audio = self._q.get()
            if audio is None:
                break
            if len(audio) / SR < 0.4:        # too short to be real speech
                continue
            text = self._engine.transcribe(audio, task=self._task).strip()
            if text and not _is_hallucination(text, audio):
                stamp = time.strftime("%M:%S", time.gmtime(time.time() - self._t0))
                print(f"[{stamp}] {text}", flush=True)


def cmd_run(device: str | None, task: str, threshold: float, debug: bool) -> None:
    try:
        import pyaudiowpatch  # noqa: F401
    except ImportError:
        raise SystemExit(
            "PyAudioWPatch is required for loopback capture.\n"
            "  install:  .\\.venv\\Scripts\\python.exe -m pip install PyAudioWPatch"
        )
    pa = _pa()

    from koe.config import Config
    from koe.engine import TranscriptionEngine

    cfg = Config.load()
    p = pa.PyAudio()
    try:
        dev = _pick_loopback(p, device)
    finally:
        p.terminate()

    print(f"loading {cfg.model} ...", flush=True)
    engine = TranscriptionEngine(model=cfg.model, device=cfg.device,
                                 compute_type=cfg.compute_type, language=cfg.language)
    mode = "translate->EN" if task == "translate" else "transcribe"
    print(f"\nKoe Interpreter — capturing: {dev['name']}\n"
          f"mode={mode}  threshold={threshold}  (Ctrl+C to stop)\n", flush=True)

    # VAD / segmentation parameters (in ~0.1 s blocks).
    SILENCE_HANG = 6     # 0.6 s of quiet ends an utterance
    MIN_SPEECH = 3       # >=0.3 s of speech before a flush is worthwhile
    MAX_SEG = 120        # 12 s hard cap so a monologue still gets captioned
    PREROLL = 3          # keep 0.3 s of pre-speech so onsets aren't clipped

    raw_q: "queue.Queue[np.ndarray]" = queue.Queue()
    seg_q: "queue.Queue" = queue.Queue()
    cap = _Capture(dev, raw_q)
    t0 = time.time()
    tr = _Transcriber(engine, task, seg_q, t0)
    cap.start()
    tr.start()

    seg: list[np.ndarray] = []
    speech = silence = 0
    in_speech = False
    last_dbg = 0.0
    try:
        while True:
            block = raw_q.get()
            rms = float(np.sqrt(np.mean(block * block))) if block.size else 0.0
            voiced = rms > threshold
            if debug and time.time() - last_dbg > 0.5:
                bar = "#" * min(40, int(rms * 400))
                print(f"  rms={rms:.4f} {'VOICED' if voiced else 'quiet '} "
                      f"|{bar:<40}| pending={seg_q.qsize()}", file=sys.stderr, flush=True)
                last_dbg = time.time()

            seg.append(block)
            if voiced:
                speech += 1
                silence = 0
                in_speech = True
            else:
                silence += 1

            ended = in_speech and silence >= SILENCE_HANG and speech >= MIN_SPEECH
            capped = len(seg) >= MAX_SEG and speech >= MIN_SPEECH
            if ended or capped:
                seg_q.put(np.concatenate(seg).astype(np.float32))
                seg, speech, silence, in_speech = [], 0, 0, False
            elif not in_speech and len(seg) > PREROLL:
                # Discard leading silence so the buffer (and latency) stays small.
                seg = seg[-PREROLL:]
    except KeyboardInterrupt:
        print("\nstopping ...", flush=True)
    finally:
        cap.stop()
        seg_q.put(None)
        tr.join(timeout=2.0)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    if "--list" in argv:
        cmd_list()
        return

    def _val(name: str, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default

    device = _val("--device")
    task = "translate" if "--translate" in argv else "transcribe"
    threshold = float(_val("--threshold", "0.005"))
    debug = "--debug" in argv
    cmd_run(device, task, threshold, debug)


if __name__ == "__main__":
    main()
