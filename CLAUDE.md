# CLAUDE.md — working on Koe (声)

This file is the onboarding contract for any AI assistant (or human) continuing
this project. It exists so the project can be advanced *at the same level*
regardless of which model or person picks it up. Read it fully before changing code.

## What Koe is

A fully **local, offline** voice toolkit for Windows. Four shipped pillars:

1. **Dictation** (`run.py` → `koe/app.py` + `koe/tray.py`) — hotkey → mic →
   faster-whisper → dictionary → ③ refiner → Unicode-safe injection into the
   focused app. Streams sentence-by-sentence.
2. **Koe Interpreter** (`interpreter.py`) — live captions of *system* audio
   (WASAPI loopback) → local translation (`--to`) → grounded reply suggestions
   (`--suggest` / `--auto-suggest`, `--role`, `--context`).
3. **Koe Talk** (`talk.py`) — sequential voice *conversation* with a local LLM:
   semantic end-of-turn detection, epoch-cancelled interruption, local TTS
   ladder (VOICEVOX → SAPI → text). The pure core is `koe/turntaking.py`, whose
   tests are the turn-taking spec. This pillar is the direct pursuit of the
   north star below.
4. **Personal benchmark** (`bench.py` + BENCHMARK.md) — reproducible CER on the
   owner's own voice; the project's decision-making instrument.

**The thesis (non-negotiable):** 誰でも使えて、誰でも安全 — free, offline, no
account, no telemetry. Nothing leaves the machine unless the user explicitly opts
into a cloud backend with their own key.

**The owner's north star (the 理想):** 「AIとのやり取りを本当に逐次的な会話で実現する」
— make interacting with AI a genuinely *sequential, turn-by-turn conversation*,
not batch request/response. See `docs/VISION.md` for the full vision and staged
roadmap toward it. When prioritizing, prefer work that moves toward that ideal.

## Read these before designing anything

| File | What it gives you |
|------|-------------------|
| `docs/DECISIONS.md` | Every non-obvious decision + **rejected approaches with data**. The project's memory. |
| `docs/VISION.md` | The sequential-conversation ideal, interaction designs, staged roadmap. |
| `ROADMAP.md` | Shipped / Next / Non-goals. Keep it current. |
| `BENCHMARK.md` | The metric spec and versioned quality results. |
| `koe/refiner.py`, `interpreter.py` | The two idiomatic reference implementations — new code should look like these. |

## Invariants (violating any of these is a bug, not a choice)

1. **Local by default.** New capability = optional local server with graceful
   fallback (the Ollama/VOICEVOX pattern), never a required cloud call. Cloud is
   opt-in, keys from env vars only, never in `config.json`.
2. **Pure core, I/O edges.** All valuable logic must be unit-testable on Ubuntu CI
   with only `pytest requests numpy`. Windows-only / heavy imports (`keyboard`,
   `sounddevice`, `pyaudiowpatch`, `uiautomation`, `pystray`, `faster_whisper`,
   `tkinter`, TTS engines) are imported **lazily inside functions**.
3. **Graceful degradation.** A failed subsystem degrades to something useful and
   never crashes the pipeline. Warn visibly when a silent fallback would mislead.
4. **The ③ refiner never translates** (deterministic guard, not just prompt).
   Translation lives only in `koe/translator.py`.
5. **Never stall the hot path.** GPU work off the capture/hotkey threads; LLM work
   off the caption/transcribe path (queues + dedicated workers, coalesce bursts).
6. **Quality changes need numbers.** Anything touching STT/refine quality: ask the
   owner to run `bench.py run` / `sweep` (samples are private, only they can) and
   record the result in BENCHMARK.md before changing defaults.
7. **`127.0.0.1`, never `localhost`**; one `requests.Session(trust_env=False)` per
   thread.
8. **Display-only UI on the GPU path** — never re-transcribe for a UI feature.

## Development workflow

```bash
python -m pytest -q          # pure tests — must pass on any OS, no hardware
python -m compileall koe     # syntax check (CI runs both)
```

- On Windows only: `python selftest.py` (engine smoke), `run.bat` /
  `run-admin.bat`, `python interpreter.py --debug`, `bench.py`.
- CI is `.github/workflows/ci.yml`: ubuntu, `pip install pytest requests numpy`,
  compileall + pytest. If you add a module CI can't import, you broke the contract
  in invariant 2.
- You (an AI in a Linux sandbox) **cannot** validate GPU/mic/Windows behavior.
  Say so explicitly in your handoff, and list the exact manual checks the owner
  should run (which flags, what to listen for).

### Process for a change (this is how the level is maintained)

1. Re-read `docs/DECISIONS.md` — is this decided or rejected already?
2. Write the pure-logic core first, with tests that encode the *spec*, including
   the failure modes (see `test_calibrate_threshold_*` as the model).
3. Wrap it in I/O following an existing module's shape (`interpreter.py` for
   pipelines, `refiner.py` for LLM backends, `config.py` for settings).
4. Every tunable constant gets a comment stating **why that value**.
5. Update: ROADMAP.md (move/add items), README.md + README.ja.md (keep in sync —
   both exist and both are maintained), DECISIONS.md (if you decided or rejected
   something non-trivial), BENCHMARK.md (if you measured something).
6. Commit messages follow the existing style: `Area: what changed` — look at
   `git log --oneline` and match it.

## Style

- Module docstrings explain **why the module exists and its contract**, not just
  what it does — they are the primary documentation. Match the density of
  `refiner.py` / `recorder.py`.
- Comments record *decisions and constraints*, not narration. English code/comments;
  Japanese is used in user-facing strings (tray menu, prompts to the user) and
  domain examples.
- Threads: `daemon=True`, communicate via `queue.Queue`, `None` as the stop
  sentinel, `join(timeout=...)` on shutdown.
- Config: add fields to the `Config` dataclass with a comment block; unknown keys
  are filtered on load (forward compatible) — never rename an existing key.
- CLI entry points parse argv by hand (no argparse — keep it consistent), start
  with `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`.

## Architecture map (one line each)

```
run.py               entry → koe.app.main (tray by default, --console fallback)
koe/app.py           dictation loop: hotkey → record → transcribe → refine → inject
koe/tray.py          pystray shell: status icon, settings menu, correction dialog
koe/recorder.py      always-on mic stream + preroll ring buffer
koe/engine.py        faster-whisper wrapper (CUDA→CPU fallback, anti-hallucination)
koe/dictionary.py    terminology bias (initial_prompt) + wrong=>right corrections
koe/refiner.py       ③ cleanup backends (rules/ollama/claude/openai) + streaming
koe/formatter.py     deterministic formatting, spoken commands, repeat collapse
koe/translator.py    caption translation via local Ollama (opposite contract to ③)
koe/responder.py     reply suggestion for live calls (--role/--context grounding)
koe/context_grabber.py  focused window/field text via UIA (local grounding)
koe/injector.py      clipboard-paste / keystroke injection
koe/config.py        config.json dataclass (created on first run)
koe/paths.py         data dir resolution (repo root vs. next to frozen .exe)
koe/turntaking.py    Koe Talk's pure core: semantic endpointing, TurnEngine
                     (epochs/merge/barge), history, commands, TTS hygiene
koe/voice.py         local TTS ladder (VOICEVOX → SAPI → text), WAV decode
koe/latency.py       per-turn conversation latency timelines + session stats
interpreter.py       system-audio live captions pipeline (capture/VAD/transcribe)
talk.py              voice conversation loop (event mailbox around TurnEngine)
bench.py             personal CER benchmark (record/run/sweep/history)
selftest.py          on-Windows hardware smoke test
```

## Known pitfalls (each cost real debugging time)

- `keyboard` high-level APIs are unreliable for modifier keys → raw hook +
  side-specific names (`koe/app.py:_install_hotkey`). The `_suppress` flag exists
  because Koe's own Ctrl+V would otherwise retrigger the hotkey.
- Whisper `initial_prompt` is *prior transcript*, not instructions — writing
  directives in it fails and regresses (BENCHMARK v2).
- Whisper hallucinates stock phrases on near-silence and loops on noise — three
  deterministic defense layers exist (DECISIONS D10); keep them.
- qwen2.5:7b leaks Chinese into Japanese output; scripts checks (kana /
  simplified-hanzi sets) are the reliable detectors, not LLM self-judgment.
- cp932 consoles crash on unicode prints → `errors="replace"` at every entry point.
- A deque at maxlen mutated by another thread raises during iteration → snapshot
  with `list(...)` (interpreter.py `_suggest_now`).
- pyttsx3/SAPI is COM-backed and **thread-confined**: create the engine on the
  one thread that uses it (koe/voice.py synthesizes on the TTS worker only).
- VOICEVOX's first synthesis after startup is slow (model load) → talk.py
  pre-warms it via a PREWARM_EPOCH item on the TTS worker's own queue (thread
  confinement!), and Ollama on a plain background HTTP thread.
- `TurnEngine` is single-threaded by contract — feed it only from the one
  mailbox loop; workers may READ `turns.epoch` (atomic int) but never call
  methods (D26).
