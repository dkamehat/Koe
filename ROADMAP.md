# Koe Roadmap

Koe is a fully **local, offline** voice toolkit for Windows. Everything runs
on-device — the guiding constraint, not a feature. The north star it is moving
toward — AIとのやり取りを本当に逐次的な会話で実現する — lives in
[docs/VISION.md](docs/VISION.md); non-obvious decisions (and rejected paths)
live in [docs/DECISIONS.md](docs/DECISIONS.md).

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
- **Koe Talk v1** (`talk.py`) — sequential voice conversation with a local LLM:
  semantic end-of-turn detection (「…けど」 waits, 「…ですか？」 answers fast),
  epoch-cancelled interruption (hotkey; voice barge-in in `--echo-mode
  headphones`), merge-on-resume, local TTS ladder (VOICEVOX → SAPI → text-only),
  「貼って」/「終了」 spoken commands, `--text` mode (mic-less validation on any
  OS), `--debug` per-turn latency timelines. Pure core + executable-spec tests
  in `koe/turntaking.py` / `tests/test_talk.py` (D22–D28).

## Next

In rough priority order — smallest / lowest-risk first:

- [ ] **Owner validation of Koe Talk on hardware** — the v1 loop is spec-tested
  but was written off-Windows. Run: `python talk.py --text` (no mic), then
  `python talk.py --debug` with mic+Ollama(+VOICEVOX), then `--echo-mode
  headphones` barge-in mid-reply. Tune `talk_patience` from the printed gap
  timelines; record p50/p95 in BENCHMARK.md (a "Talk latency" section).
- [ ] **Reply-language autodetect beyond en/ja** — distinguish ja/zh/ko for the reply
  direction (today it's CJK-script-based en/ja). Logic-only and unit-testable.
- [ ] **On-demand VAD recalibration** — a flag/hotkey to re-measure the noise floor
  mid-session when the audio source changes (extends the startup calibration; reuses
  `calibrate_threshold`).
- [ ] **Talk v2 — the rhythm** (see docs/VISION.md): aizuchi backchannels + a
  thinking sound from a zero-latency local WAV cache; startup echo-calibration
  handshake + `is_self_echo` pause-then-verify for open speakers; `talkbench`
  (conversation-latency regression bench, results.jsonl); `--debug` event-trace
  save + offline replay through the pure TurnEngine.
- [ ] **Extract the interpreter's VAD loop into a shared Segmenter class** —
  talk.py deliberately did not touch interpreter.py (zero regression risk);
  migrate both onto one class only behind an equivalence test.
- [ ] **On-screen overlay captions** — a translucent, always-on-top window over the call
  showing the live caption + translation. Display-only — never re-transcribe on the GPU
  path (the dictation overlay was removed for exactly that regression).

## Non-goals

- Cloud-required features, or sending audio off the device by default. Cloud
  refiner backends stay strictly opt-in (bring-your-own-key). Local-first is the thesis.
