# Koe Roadmap

Koe is a fully **local, offline** voice toolkit for Windows. Everything runs
on-device — the guiding constraint, not a feature.

## Shipped

- **Dictation** — push-to-talk local STT (faster-whisper) → pluggable ③ refiner
  (rules / local Ollama / optional cloud) → types into the active window. Terminology
  dictionary biases proper nouns and self-heals from corrections.
- **Quality benchmark** — reproducible, normalized **CER** on your own voice
  (`bench.py`, see [BENCHMARK.md](BENCHMARK.md)); versioned results; code-switching
  handled via dictionary bias + safe corrections.
- **Koe Interpreter** (`interpreter.py`) — live captions for *system* audio via
  WASAPI loopback → translate to any language (local Ollama) → **context-grounded
  reply suggestion** (F9 or auto-on-question, with `--role` / `--context`). All local.
- **VAD auto-calibration** — the interpreter measures the loopback noise floor at
  startup and derives its voicing threshold (robust low-percentile × margin, clamped),
  so `--threshold` no longer needs hand-tuning per machine/source (`--no-calibrate`
  to opt out).

## Next

In rough priority order — smallest / lowest-risk first:

- [ ] **Reply-language autodetect beyond en/ja** — distinguish ja/zh/ko for the reply
  direction (today it's kana-based en/ja). Logic-only and unit-testable; start here.
- [ ] **On-demand VAD recalibration** — a flag/hotkey to re-measure the noise floor
  mid-session when the audio source changes (extends the startup calibration; reuses
  `calibrate_threshold`).
- [ ] **On-screen overlay captions** — a translucent, always-on-top window over the call
  showing the live caption + translation. Display-only — never re-transcribe on the GPU
  path (the dictation overlay was removed for exactly that regression).

## Non-goals

- Cloud-required features, or sending audio off the device by default. Cloud
  refiner backends stay strictly opt-in (bring-your-own-key). Local-first is the thesis.
