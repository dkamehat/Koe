#!/usr/bin/env python
"""Koe personal quality bench — compare transcription quality on YOUR voice.

"Satisfying quality" can't be reduced to one number, but you CAN make changes
*comparable*: record a few of your own samples once, write the text you'd accept,
then re-run after any change (model, refiner, settings) and read the diffs + CER.

Usage:
    python bench.py record "正解として納得できるテキスト"   # record one sample (Enter to stop)
    python bench.py list                                    # list saved samples
    python bench.py run                                     # score all samples with current config
    python bench.py run --diff                              # also show char-level diffs (ref vs final)
    python bench.py run --model large-v3 --refiner rules    # quick A/B without editing config.json
    python bench.py sweep --models small,large-v3-turbo,large-v3 --refiners rules,ollama
                                                            # compare a matrix in one shot
    python bench.py history                                 # past results, newest first

Samples live in ./bench/ (NN.wav + NN.txt) and are gitignored — your voice never
leaves the machine. Every run/sweep row is appended to bench/results.jsonl so you
can see whether a change actually moved the needle over time.
"""

from __future__ import annotations

import json
import sys
import time
import unicodedata
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from koe.config import Config

# Emit UTF-8 so Japanese + arrows render on Windows Terminal; never crash on a
# legacy cp932 console (errors="replace" degrades instead of raising).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

BENCH_DIR = Path(__file__).resolve().parent / "bench"
RESULTS_LOG = BENCH_DIR / "results.jsonl"


# --- character error rate (no deps) ----------------------------------------

def _dp(a: str, b: str) -> list[list[int]]:
    """Full Levenshtein DP table (rows=len(a)+1, cols=len(b)+1)."""
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        dp[i][0] = i
    for j in range(1, n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        ca = a[i - 1]
        for j in range(1, n + 1):
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + (ca != b[j - 1]),
            )
    return dp


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


def normalize(s: str) -> str:
    """Canonical form scored by the benchmark (see BENCHMARK.md for the spec).

    NFKC (folds full/half-width, ％->%, ３->3) -> casefold (ignore Latin case) ->
    drop punctuation / separators / control chars, EXCEPT '%' which carries meaning
    (so a missing % is still an error). Word-internal marks like 'ー' (Lm) are kept.
    This makes CER measure *recognition*, not the reference's punctuation style.
    """
    s = unicodedata.normalize("NFKC", s).casefold()
    out = []
    for c in s:
        if c == "%":
            out.append(c)
        elif unicodedata.category(c)[0] in ("P", "Z", "C"):
            continue  # punctuation, separators (incl. spaces), control
        else:
            out.append(c)
    return "".join(out)


def cer(ref: str, hyp: str) -> float:
    """Character error rate (0 = perfect) over the normalized forms."""
    r = normalize(ref)
    h = normalize(hyp)
    return 0.0 if not r else _levenshtein(r, h) / len(r)


def char_diff(ref: str, hyp: str) -> str:
    """Compact, readable char-level diff of ref vs hyp (whitespace-insensitive).

    Matching runs are printed as-is; mismatches use git-style ASCII markers:
    [a->b] a substitution, [-x-] a deletion (ref had it, output dropped it),
    [+y+] an insertion (output added it). This makes it obvious whether a miss is
    a swap the dictionary can fix (e.g. [クバネティス->Kubernetes]) or a deeper
    STT error. ASCII markers so it never crashes on a cp932 console.

    Diffed on the normalized (scored) forms, so it explains the CER number and
    isn't drowned in punctuation/casing noise.
    """
    r = normalize(ref)
    h = normalize(hyp)
    dp = _dp(r, h)
    i, j = len(r), len(h)
    # Backtrace into ordered ops, then reverse.
    del_buf: list[str] = []   # ref chars not in hyp (or substituted-from)
    ins_buf: list[str] = []   # hyp chars not in ref (or substituted-to)
    out: list[str] = []

    def flush() -> None:
        if not (del_buf or ins_buf):
            return
        left = "".join(reversed(del_buf))
        right = "".join(reversed(ins_buf))
        if left and right:
            out.append(f"[{left}->{right}]")
        elif left:
            out.append(f"[-{left}-]")
        else:
            out.append(f"[+{right}+]")
        del_buf.clear()
        ins_buf.clear()

    while i > 0 or j > 0:
        if i > 0 and j > 0 and r[i - 1] == h[j - 1] and dp[i][j] == dp[i - 1][j - 1]:
            flush()
            out.append(r[i - 1])
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            del_buf.append(r[i - 1])   # substitution
            ins_buf.append(h[j - 1])
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            del_buf.append(r[i - 1])   # deletion (ref had a char hyp dropped)
            i -= 1
        else:
            ins_buf.append(h[j - 1])   # insertion (hyp added a char)
            j -= 1
    flush()
    return "".join(reversed(out))


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


def _samples() -> list[Path]:
    return sorted(BENCH_DIR.glob("*.wav")) if BENCH_DIR.exists() else []


def _ref_text(wav: Path) -> str:
    # utf-8-sig strips a BOM if an editor (Notepad/PowerShell) added one.
    return (BENCH_DIR / f"{wav.stem}.txt").read_text(encoding="utf-8-sig").strip()


def _log_result(row: dict) -> None:
    """Append one summary row to results.jsonl (history / cross-run comparison)."""
    BENCH_DIR.mkdir(exist_ok=True)
    row = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M"), **row}
    with RESULTS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


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
    samples = _samples()
    if not samples:
        print("no samples yet — record one with: python bench.py record \"...\"")
        return
    for wav in samples:
        ref = (BENCH_DIR / f"{wav.stem}.txt")
        ref_text = ref.read_text(encoding="utf-8-sig") if ref.exists() else "(no reference)"
        print(f"{wav.stem}: {ref_text}")


def _flag(argv: list[str], name: str) -> str | None:
    """Return the value following --name, or None."""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


def cmd_run(argv: list[str]) -> None:
    from koe.dictionary import Dictionary
    from koe.engine import TranscriptionEngine
    from koe.refiner import build_refiner

    cfg = Config.load()
    if (m := _flag(argv, "--model")):
        cfg.model = m
    if (r := _flag(argv, "--refiner")):
        cfg.refiner_backend = r
    show_diff = "--diff" in argv

    samples = _samples()
    if not samples:
        print("no samples — record some first: python bench.py record \"...\"")
        return

    d = Dictionary()
    t_load = time.time()
    eng = TranscriptionEngine(model=cfg.model, device=cfg.device,
                              compute_type=cfg.compute_type, language=cfg.language)
    r = build_refiner(cfg)
    _maybe_warn_refiner(cfg, r)
    print(f"model={cfg.model}  refiner={r.name}  "
          f"({len(samples)} samples, load {time.time()-t_load:.1f}s)\n")

    raw_tot = fin_tot = stt_tot = ref_tot = 0.0
    for wav in samples:
        ref = _ref_text(wav)
        audio = _read_wav(wav)
        t0 = time.time()
        raw = d.apply(eng.transcribe(audio, initial_prompt=d.initial_prompt()))
        t_stt = time.time() - t0
        t1 = time.time()
        final = d.apply(r.refine(raw, list(d.terms)))
        t_ref = time.time() - t1
        c_raw, c_fin = cer(ref, raw), cer(ref, final)
        raw_tot += c_raw
        fin_tot += c_fin
        stt_tot += t_stt
        ref_tot += t_ref
        print(f"[{wav.stem}] CER raw={c_raw:.1%}  final={c_fin:.1%}  "
              f"[STT {t_stt:.1f}s / ③ {t_ref:.1f}s]")
        print(f"    ref  : {ref}")
        print(f"    raw  : {raw}")
        print(f"    final: {final}")
        if show_diff and c_fin > 0:
            print(f"    diff : {char_diff(ref, final)}")
        print()

    n = len(samples)
    print(f"=== mean CER:  raw(STT)={raw_tot/n:.1%}   final(+③)={fin_tot/n:.1%}   "
          f"| mean latency: STT {stt_tot/n:.1f}s / ③ {ref_tot/n:.1f}s ===")
    print("(lower is better; compare across model/refiner changes)")
    _log_result({"cmd": "run", "model": cfg.model, "refiner": r.name, "n": n,
                 "cer_raw": round(raw_tot / n, 4), "cer_final": round(fin_tot / n, 4),
                 "stt_s": round(stt_tot / n, 2), "ref_s": round(ref_tot / n, 2)})


def cmd_sweep(argv: list[str]) -> None:
    """Compare a matrix of models × refiners in one shot.

    Each model is loaded ONCE and transcribes every sample (the expensive step);
    its raw output is cached and reused across all refiners, so the matrix costs
    roughly (load+transcribe)·models + refine·models·refiners — not the product.
    """
    from koe.dictionary import Dictionary
    from koe.engine import TranscriptionEngine
    from koe.refiner import build_refiner

    cfg = Config.load()
    models = (_flag(argv, "--models") or cfg.model).split(",")
    refiners = (_flag(argv, "--refiners") or cfg.refiner_backend).split(",")
    models = [m.strip() for m in models if m.strip()]
    refiners = [x.strip() for x in refiners if x.strip()]

    samples = _samples()
    if not samples:
        print("no samples — record some first: python bench.py record \"...\"")
        return

    d = Dictionary()
    refs = {w.stem: _ref_text(w) for w in samples}
    audios = {w.stem: _read_wav(w) for w in samples}
    print(f"sweep: models={models} x refiners={refiners}  ({len(samples)} samples)\n")

    rows: list[dict] = []
    for model in models:
        t_load = time.time()
        try:
            eng = TranscriptionEngine(model=model, device=cfg.device,
                                      compute_type=cfg.compute_type, language=cfg.language)
        except Exception as exc:
            print(f"  ! model {model} failed to load: {exc}\n")
            continue
        # Transcribe once per sample; cache raw + STT latency.
        raw_cache: dict[str, str] = {}
        stt_sum = 0.0
        for w in samples:
            t0 = time.time()
            raw_cache[w.stem] = d.apply(eng.transcribe(audios[w.stem],
                                                        initial_prompt=d.initial_prompt()))
            stt_sum += time.time() - t0
        raw_cer = sum(cer(refs[s], raw_cache[s]) for s in raw_cache) / len(samples)
        print(f"  {model}: raw CER={raw_cer:.1%}  "
              f"(load {time.time()-t_load:.1f}s, STT {stt_sum/len(samples):.1f}s/sample)")

        for backend in refiners:
            cfg.refiner_backend = backend
            r = build_refiner(cfg)
            _maybe_warn_refiner(cfg, r, indent="    ")
            fin_sum = ref_sum = 0.0
            for s, raw in raw_cache.items():
                t1 = time.time()
                final = d.apply(r.refine(raw, list(d.terms)))
                ref_sum += time.time() - t1
                fin_sum += cer(refs[s], final)
            rows.append({
                "model": model, "refiner": r.name,
                "cer_raw": raw_cer, "cer_final": fin_sum / len(samples),
                "stt_s": stt_sum / len(samples), "ref_s": ref_sum / len(samples),
            })
        del eng  # release VRAM before loading the next model
        print()

    if not rows:
        return
    rows.sort(key=lambda x: x["cer_final"])
    print(f"{'model':<18} {'refiner':<8} {'raw':>7} {'final':>7} {'STT':>7} {'③':>7}")
    print("-" * 58)
    for x in rows:
        print(f"{x['model']:<18} {x['refiner']:<8} "
              f"{x['cer_raw']:>6.1%} {x['cer_final']:>6.1%} "
              f"{x['stt_s']:>6.1f}s {x['ref_s']:>6.1f}s")
    best = rows[0]
    print(f"\n→ best final CER: {best['model']} + {best['refiner']} "
          f"({best['cer_final']:.1%})")
    for x in rows:
        _log_result({"cmd": "sweep", "model": x["model"], "refiner": x["refiner"],
                     "n": len(samples), "cer_raw": round(x["cer_raw"], 4),
                     "cer_final": round(x["cer_final"], 4),
                     "stt_s": round(x["stt_s"], 2), "ref_s": round(x["ref_s"], 2)})


def cmd_history(argv: list[str]) -> None:
    if not RESULTS_LOG.exists():
        print("no results yet — run `python bench.py run` first.")
        return
    limit = int(_flag(argv, "--limit") or 20)
    lines = RESULTS_LOG.read_text(encoding="utf-8").splitlines()
    print(f"{'when':<17} {'model':<18} {'refiner':<8} {'raw':>7} {'final':>7} {'STT':>7} {'③':>7}")
    print("-" * 76)
    for line in lines[-limit:]:
        try:
            x = json.loads(line)
        except Exception:
            continue
        print(f"{x.get('ts',''):<17} {x.get('model',''):<18} {x.get('refiner',''):<8} "
              f"{x.get('cer_raw',0):>6.1%} {x.get('cer_final',0):>6.1%} "
              f"{x.get('stt_s',0):>6.1f}s {x.get('ref_s',0):>6.1f}s")


def _maybe_warn_refiner(cfg, refiner, indent: str = "") -> None:
    """Warn if the chosen refiner silently degraded (e.g. ollama not running),
    so a sweep row isn't mistaken for a real comparison."""
    backend = (cfg.refiner_backend or "").lower()
    if backend == "ollama" and refiner.name == "rules":
        print(f"{indent}! ollama selected but unavailable — using rules (start the server for a real test).")
    if backend in ("claude", "openai") and refiner.is_cloud:
        import os
        key = "ANTHROPIC_API_KEY" if backend == "claude" else "OPENAI_API_KEY"
        if not os.environ.get(key):
            print(f"{indent}! {backend} selected but {key} is unset — output falls back to rules.")


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "record" and len(args) >= 2:
        cmd_record(args[1])
    elif cmd == "list":
        cmd_list()
    elif cmd == "run":
        cmd_run(args[1:])
    elif cmd == "sweep":
        cmd_sweep(args[1:])
    elif cmd == "history":
        cmd_history(args[1:])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
