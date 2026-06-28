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

## Next

- [ ] Reply-language autodetect beyond en/ja (distinguish ja/zh/ko)
- [ ] On-screen overlay captions over the call window (translucent, topmost)
- [ ] VAD threshold auto-calibration (measure the noise floor at startup)

## Non-goals

- Cloud-required features, or sending audio off the device by default. Cloud
  refiner backends stay strictly opt-in (bring-your-own-key). Local-first is the thesis.
