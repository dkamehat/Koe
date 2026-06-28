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
2. **casefold** — Latin case is ignored (`Hugging Face` == `hugging face`).
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
default is left open here and resolved in v1 below with spontaneous samples.

### v1 — 2026-06-28 (③ refiner decision)

- **koe commit:** `f7afbb7` (+ this change: personal ③ default → rules)
- **dataset:** personal, 9 samples (added 5 spontaneous/messy: fillers, run-ons,
  false starts, code-switching)
- **hardware:** RTX 3080 Ti Laptop, CUDA / float16

| model            | refiner | raw CER | final CER | STT (s) | ③ (s) |
|------------------|---------|--------:|----------:|--------:|------:|
| large-v3-turbo   | rules   |    5.4% |      5.4% |     0.5 |   0.0 |
| large-v3-turbo   | ollama  |    5.4% |      7.1% |     0.5 |   0.7 |

**Decision: ③ default = `rules`.** `rules` never alters Japanese content, so
`final ≡ raw` (5.4%) at zero added latency. `ollama` is **net-negative** (7.1%):
it degraded 3/9 samples by paraphrasing *against* its instruction — altering verb
endings (`思っていて`→`思っています`), dropping content particles as if filler
(`なんですよね`→`なんですね`), and hallucinating Chinese on code-switching
(`close`→`クローン`→`克隆`). The intended ③ value (filler removal, punctuation)
rarely triggers because Whisper already handles it on this voice. `ollama` stays
selectable but is no longer the default. (Shipped default in `config.py` is left at
`auto` — this conclusion rests on one speaker's 9 JP samples and shouldn't be
generalized to all users without a public-dataset run; see Tier 2.)

**Known STT gap (#07):** embedded English tech terms spoken inside a Japanese
sentence are mis-transcribed by Whisper itself (`issue`→`意思`, `close`→`クローン`),
giving CER 45.6% on that sample. Not fixable by ③ (ollama made it worse). Addressed
in v2 below.

### v2 — 2026-06-28 (③=rules; code-switching #07 worked via the dictionary)

- **koe commit:** `7c1b216` (+ this change: bench `--no-dict` toggle; dictionary.py
  comment. The fix itself is **dictionary entries**, which live in the gitignored
  `dictionary.txt` and stay local — no model/engine code changed.)
- **dataset:** personal, 9 samples (same as v1)
- **hardware:** RTX 3080 Ti Laptop, CUDA / float16

| model            | refiner | dict | raw CER | final CER | STT (s) | ③ (s) |
|------------------|---------|------|--------:|----------:|--------:|------:|
| large-v3-turbo   | rules   | on   |    1.3% |      1.3% |     0.5 |   0.0 |
| large-v3-turbo   | rules   | off  |    9.7% |      9.7% |     0.5 |   0.0 |

(The `dict=off` row is `bench run --no-dict`, added this version to isolate the
terminology dictionary's contribution. It is the bias + correction the dictionary
provides, *not* a model change.)

**Code-switching (#07) result on the worst sample: 45.6% → 8.8%; overall mean
5.4% → 1.3%.** Two mechanisms, both in the dictionary, no engine code:

1. **Decode-time bias** (`initial_prompt`) — listing the English term (`issue`,
   `Ollama`, `Whisper`) makes Whisper emit it in English instead of kana/kanji.
   This is the *only* lever that can fix a homograph like `意思`→`issue`, because
   it acts before the wrong characters are ever produced. A post-hoc rule can't
   (it can't tell a mis-heard `意思` from a genuine one).
2. **Safe post-hoc correction** for the unambiguous katakana the bias didn't
   catch: `プルリクエスト`→`pull request`, `マージ`→`merge`.

**What did NOT work (rejected, with data):** a code-switch *demo sentence* prefixed
to `initial_prompt` ("…such terms are written in English…"). Whisper treats the
prompt as prior transcript, not an instruction, so it ignored the directive,
**failed** to fix the target (#07 stayed 45.6%) **and regressed** a previously-clean
sample (#03 `Ollama`/`Whisper` → `オラマ`/`ウィスパー`, 0%→33.3%; mean 5.4%→9.7%).
Reverted; `initial_prompt` is a plain term listing.

**Residual hard limit:** `close`→`クローン`. The katakana `クローン` is the genuine
word *clone* (`git clone`), so a blind `クローン`→`close` rule would corrupt real
usage; `close`↔`clone` is a homograph the decoder mis-resolved acoustically and
neither bias nor a safe rule can recover without sentence context. Left as-is —
this is the 8.8% residual on #07.

---

## Roadmap

### ② Code-switching: English tech terms in Japanese speech (largely resolved in v2)

Whisper mis-hears English words embedded in a Japanese sentence
(`issue`→`意思`, `close`→`クローン`, `pull request`→`プルリクエスト`) — the worst
sample in v1 at 45.6% CER. **v2 brought it to 8.8%** using the terminology
dictionary only (see v2 above): decode-time `initial_prompt` bias for the
homograph cases (`意思`→`issue`) + safe katakana corrections for the unambiguous
ones. `ollama` (③) was the wrong tool — it worsened this, even hallucinating
Chinese. A prefixed code-switch *demo sentence* was tried and rejected (regressed
other samples). **Residual:** `close`↔`clone` (`クローン`) — a true homograph that
needs sentence-level context, not yet handled. Open future direction if it
recurs: context-guarded term mapping (use the focused-window context already
captured by `enable_context`) to disambiguate `クローン`=clone vs close.

### Tier 2 — comparability via citation (decided; full harness deferred)

**Decision:** we do **not** build a multi-dataset public-run harness. Two reasons,
both structural rather than effort:

1. **A public run of Koe mostly measures Whisper, not Koe.** Koe is
   `large-v3-turbo + rules + dictionary`. The dictionary — Koe's main accuracy
   lever — is tuned to the user's voice/domain and is **inert on generic public
   audio** (the `dict=off` row above: 9.7% vs 1.3% is exactly that lever). So a
   public number would re-measure the base model, whose Japanese CER is already
   published by others.
2. **Cross-tool numbers aren't directly comparable.** Published CERs differ by
   normalization, dataset version (CommonVoice 8 vs 17), and reference
   conventions; we can't re-normalize other systems' pipelines to match ours.

Instead we state the base model honestly and **cite** its published Japanese CER.

**Underlying model — published Japanese CER (lower = better):**

| model (base of Koe)         | CommonVoice 8 (ja) | JSUT basic5000 | ReazonSpeech (held-out) |
|-----------------------------|-------------------:|---------------:|------------------------:|
| OpenAI `whisper-large-v3`   |               8.5% |           7.1% |                   14.9% |

Source: the kotoba-whisper-v2.0 model card's evaluation table (OpenAI
`whisper-large-v3` row).[^kotoba] Koe ships **`large-v3-turbo`**, which keeps the
large-v3 encoder and distills the decoder (32→4 layers): ~6× faster, accuracy
close to large-v3 (and on par with large-v2).[^turbo] Treat the table as the
single-digit-CER ballpark Koe inherits on clean public Japanese; turbo is
marginally higher.

**These are not our numbers and not directly comparable to the personal-bench CER
above** (different audio, different normalization). They establish the floor Koe
stands on. **Koe's own contribution sits on top of it** and is what the personal
bench measures: clean dictation already 0%, and code-switching driven from 45.6%
→ 8.8% via the dictionary (v2) — neither of which a generic public set exercises.

**If a Koe-specific public claim is ever needed** (e.g. to quantify the dictionary
on a shared set), the minimal add is a `bench dataset` command scoring a small
**Common Voice ja** (CC0) subset with the same metric/normalization. Kept out of
scope for now: marginal signal, data-egress vs the local-first thesis, and upkeep.

[^kotoba]: kotoba-tech/kotoba-whisper-v2.0 model card — <https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0>
[^turbo]: Whisper large-v3-turbo (4-layer distilled decoder, ~6× faster, ≈large-v2 accuracy) — <https://github.com/openai/whisper/discussions/2363>
