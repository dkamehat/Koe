# Koe Benchmark

A small, **reproducible** quality benchmark for Koe's speech-to-text pipeline.
The point is to make changes *comparable*: fix a metric, fix a normalization, and
re-run after any change (model, refiner, prompt, dictionary) to see whether the
number actually moved — not whether the wording happened to come out differently.

> **Privacy.** The benchmark audio is **your own voice** and never leaves the
> machine: `bench/` (the `.wav` samples and their reference `.txt`) is gitignored.
> What is versioned and publishable is only this document: the **metric
> definition**, the **harness** (`bench.py`), and **aggregate numbers**. No audio,
> no transcripts.

---

## Metric

**CER — Character Error Rate** (Japanese has no word boundaries, so character-level
is the standard unit; English would use WER, but Koe here is Japanese-primary).

```
CER = levenshtein(normalize(reference), normalize(hypothesis)) / len(normalize(reference))
```

Lower is better; `0.0` is a perfect match. We report:

- **raw** — CER of the ② transcription (faster-whisper) alone.
- **final** — CER after the ③ refiner (rules / ollama / cloud).
- **STT / ③** — mean latency per sample of each stage (hardware-dependent; does
  not affect CER).

### Normalization (the part that makes CER honest)

Both strings are reduced to a canonical form **before** scoring, so CER measures
*recognition*, not the reference author's punctuation taste:

1. **NFKC** — folds full/half-width (`３`→`3`, `％`→`%`, half-width kana → full).
2. **casefold** — Latin case is ignored (`Sakana AI` == `sakana ai`).
3. **strip** punctuation, separators and control chars (Unicode categories
   `P*`, `Z*`, `C*`) — including spaces, `。、！？「」・…` and ASCII `.,!?` etc.

Kept on purpose:

- **`%`** — carries meaning, so a missing `%` is still counted as an error.
  (Note: `3.5%` → `35%`, because `.` is stripped like other punctuation. A wrong
  *decimal point* is therefore not detected — a known, standard-practice
  limitation; consistent with Whisper/ESPnet JP evaluation.)
- **word-internal marks** such as `ー` (long-vowel, category `Lm`) — part of words
  like `データ`, so never stripped.

Consequence: `%`↔`パーセント` count as **different** (they are different
transcriptions). Reference texts therefore follow Koe's **intended output
convention** — fillers removed (Whisper already drops `えーと`/`ね`), Arabic
numerals + symbols (`3.5%`, not `3.5パーセント`). This convention is applied
uniformly, not tuned per sample.

---

## Reproduce

```powershell
$py = ".\.venv\Scripts\python.exe"

# 1. Record your own samples (voice + the text you'd accept). Stays local.
& $py bench.py record "今日の会議で、来週の締め切りについて話しました。"

# 2. Score current config, with a char-level diff of any real errors.
& $py bench.py run --diff

# 3. Compare a matrix of models x refiners on the *same* waveforms.
& $py bench.py sweep --models large-v3-turbo,large-v3 --refiners rules,ollama

# 4. See how a change moved the number over time.
& $py bench.py history
```

Every `run`/`sweep` row is appended to `bench/results.jsonl` (gitignored) with a
timestamp, so cross-run comparison is automatic.

---

## Results

Each table is one measured snapshot. Tie every number to *what produced it*
(koe commit, dataset = your private sample set + its size, hardware) so the
history stays meaningful.

### v0 — 2026-06-28

- **koe commit:** `7fde3b2` (+ uncommitted: formatter decimal fix, bench normalization)
- **dataset:** personal, 4 samples (JP, mixed domain terms / numbers / fillers)
- **hardware:** RTX 3080 Ti Laptop, CUDA / float16

| model            | refiner | raw CER | final CER | STT (s) | ③ (s) |
|------------------|---------|--------:|----------:|--------:|------:|
| large-v3-turbo   | rules   |    0.0% |      0.0% |     0.8 |   0.0 |
| large-v3-turbo   | ollama  |    0.0% |      0.0% |     0.8 |   2.1 |
| large-v3         | rules   |    3.8% |      3.8% |     0.8 |   0.6 |
| large-v3         | ollama  |    3.8% |      3.8% |     0.8 |   0.6 |

**Reading:** on clean Japanese dictation, **`large-v3-turbo` is already perfect
(0.0%)** and beats `large-v3` (3.8%) while being no slower. The ③ refiner changes
CER by **nothing** here — it adds latency without improving recognition — so its
value lies in punctuation/formatting on messier, longer speech, which this small
clean set does not exercise. Decision: **keep `large-v3-turbo`**; the rules-vs-ollama
default is still open and needs longer/spontaneous samples to judge fairly.

---

## Roadmap

### Tier 2 — public-dataset, reproducible-by-anyone benchmark (TODO)

The current bench uses *your* voice (private, not reproducible by others). To get a
**citable, comparable** number — and a basis for comparing Koe against other STT
tools without re-running them — add an optional public-dataset mode:

- Add a `bench dataset` command that downloads a small **public Japanese corpus**
  on demand and scores it with the same metric/normalization above. Candidates:
  **Common Voice ja** (CC0), **JSUT**, or a **TEDxJP-10K** subset.
- Publish the resulting table here (audio stays on the dataset host; only numbers
  are committed) — fully shareable since it isn't personal voice.
- For cross-tool comparison, **cite other systems' published numbers on the same
  public dataset** rather than installing and running competitors (a full
  multi-tool leaderboard is deliberately out of scope: high maintenance, cloud
  cost, and data-egress that conflicts with Koe's local-first thesis).

Keep both benchmarks: the **personal** one tunes Koe for your voice/domain; the
**public** one provides comparability.
