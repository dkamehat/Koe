#!/usr/bin/env python
"""Koe personal quality bench — compare transcription quality on YOUR voice.

"Satisfying quality" can't be reduced to one number, but you CAN make changes
*comparable*: record a few of your own samples once, write the text you'd accept,
then re-run after any change (model, refiner, settings) and read the diffs + CER.

Usage:
    python bench.py record "正解として納得できるテキスト"   # record one sample (Enter to stop)
    python bench.py list                                    # list saved samples
    python bench.py run                                     # score all samples with current config
    python bench.py run --model large-v3 --refiner rules    # quick A/B without editing config.json

Samples live in ./bench/ (NN.wav + NN.txt) and are gitignored — your voice never
leaves the machine.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

from koe.config import Config

BENCH_DIR = Path(__file__).resolve().parent / "bench"


# --- character error rate (no deps) ----------------------------------------

def _levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    """Character error rate (0 = perfect). Whitespace-insensitive."""
    r = "".join(ref.split())
    h = "".join(hyp.split())
    return 0.0 if not r else _levenshtein(r, h) / len(r)


# --- wav i/o ----------------------------------------------------------------

def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        ch, n = w.getnchannels(), w.getnframes()
        audio = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    return audio


def _write_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    pcm = np.clip(audio, -1, 1)
    pcm = (pcm * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _next_index() -> int:
    BENCH_DIR.mkdir(exist_ok=True)
    used = [int(p.stem) for p in BENCH_DIR.glob("*.wav") if p.stem.isdigit()]
    return (max(used) + 1) if used else 1


# --- commands ---------------------------------------------------------------

def cmd_record(reference: str) -> None:
    import sounddevice as sd

    cfg = Config.load()
    frames: list[np.ndarray] = []
    stream = sd.InputStream(samplerate=cfg.sample_rate, channels=1, dtype="float32",
                            device=cfg.input_device,
                            callback=lambda indata, *_: frames.append(indata.copy()))
    print("● 録音中… 正解テキストを読み上げ、終わったら Enter を押してください。")
    stream.start()
    input()
    stream.stop()
    stream.close()
    audio = np.concatenate(frames).reshape(-1) if frames else np.zeros(0, dtype=np.float32)
    idx = _next_index()
    _write_wav(BENCH_DIR / f"{idx:02d}.wav", audio, cfg.sample_rate)
    (BENCH_DIR / f"{idx:02d}.txt").write_text(reference.strip(), encoding="utf-8")
    print(f"saved bench/{idx:02d}.wav ({audio.size/cfg.sample_rate:.1f}s) + reference.")


def cmd_list() -> None:
    if not BENCH_DIR.exists():
        print("no samples yet — record one with: python bench.py record \"...\"")
        return
    for wav in sorted(BENCH_DIR.glob("*.wav")):
        ref = (BENCH_DIR / f"{wav.stem}.txt")
        # utf-8-sig strips a BOM if an editor (Notepad/PowerShell) added one.
        ref_text = ref.read_text(encoding="utf-8-sig") if ref.exists() else "(no reference)"
        print(f"{wav.stem}: {ref_text}")


def cmd_run(argv: list[str]) -> None:
    from koe.dictionary import Dictionary
    from koe.engine import TranscriptionEngine
    from koe.refiner import build_refiner

    cfg = Config.load()
    for i, a in enumerate(argv):
        if a == "--model" and i + 1 < len(argv):
            cfg.model = argv[i + 1]
        elif a == "--refiner" and i + 1 < len(argv):
            cfg.refiner_backend = argv[i + 1]

    samples = sorted(BENCH_DIR.glob("*.wav")) if BENCH_DIR.exists() else []
    if not samples:
        print("no samples — record some first: python bench.py record \"...\"")
        return

    d = Dictionary()
    eng = TranscriptionEngine(model=cfg.model, device=cfg.device,
                              compute_type=cfg.compute_type, language=cfg.language)
    r = build_refiner(cfg)
    print(f"model={cfg.model}  refiner={r.name}  ({len(samples)} samples)\n")

    raw_tot = fin_tot = 0.0
    for wav in samples:
        ref = (BENCH_DIR / f"{wav.stem}.txt").read_text(encoding="utf-8-sig").strip()
        audio = _read_wav(wav)
        raw = d.apply(eng.transcribe(audio, initial_prompt=d.initial_prompt()))
        final = d.apply(r.refine(raw, list(d.terms)))
        c_raw, c_fin = cer(ref, raw), cer(ref, final)
        raw_tot += c_raw
        fin_tot += c_fin
        print(f"[{wav.stem}] CER raw={c_raw:.1%}  final={c_fin:.1%}")
        print(f"    ref  : {ref}")
        print(f"    raw  : {raw}")
        print(f"    final: {final}\n")

    n = len(samples)
    print(f"=== mean CER:  raw(STT)={raw_tot/n:.1%}   final(+③)={fin_tot/n:.1%} ===")
    print("(lower is better; compare across model/refiner changes)")


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "record" and len(args) >= 2:
        cmd_record(args[1])
    elif cmd == "list":
        cmd_list()
    elif cmd == "run":
        cmd_run(args[1:])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
