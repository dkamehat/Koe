#!/usr/bin/env python
"""Smoke test: verifies the engine loads (GPU if available) and the full
audio -> text -> format pipeline runs without touching hardware hotkeys.

Generates a synthetic audio buffer so it works headless / in CI.
"""

import time

import numpy as np

from koe.config import Config
from koe.engine import TranscriptionEngine, _cuda_available
from koe.formatter import format_text


def main() -> None:
    print("CUDA available:", _cuda_available())

    cfg = Config.load()
    # Use a small model for a fast smoke test unless the user overrides it.
    model = "small"
    print(f"Loading engine (model='{model}')…")
    t0 = time.time()
    eng = TranscriptionEngine(model=model, device=cfg.device, compute_type=cfg.compute_type)
    print(f"  -> device={eng.device} compute={eng.compute_type} ({time.time()-t0:.1f}s)")

    # 1 second of low white noise — exercises the decode path end to end.
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal(16000) * 0.01).astype(np.float32)
    print("Transcribing synthetic audio…")
    t0 = time.time()
    raw = eng.transcribe(audio)
    print(f"  raw={raw!r} ({time.time()-t0:.1f}s)")

    # Formatter checks (deterministic, no model needed).
    samples = [
        "hello world new line this is a test",
        "これはテストです 改行 二行目",
        "let's go full stop done",
    ]
    print("\nFormatter checks:")
    for s in samples:
        print(f"  {s!r}\n   -> {format_text(s)!r}")

    print("\nOK: pipeline is functional.")


if __name__ == "__main__":
    main()
