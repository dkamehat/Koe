# Koe — Decision Log (設計判断ログ)

Every non-obvious decision in this codebase, with its *why*, where it is enforced,
and what was tried and **rejected** (rejections are the most expensive knowledge to
re-earn — do not re-litigate them without new data).

Format: one entry per decision. `Enforced:` names the code/tests that keep the
decision true, so a successor can verify it still holds before touching it.

---

## D01 — Local-first is the thesis, not a feature

Everything runs on-device by default; cloud is strictly opt-in, bring-your-own-key.
API keys are read from **environment variables only**, never stored in `config.json`
(a shared config can never leak a key or silently phone home). On-screen context
grabbing is **automatically disabled when a cloud refiner is selected** so screen
text never leaves the machine.

- Enforced: `koe/refiner.py` (key handling), `koe/app.py:_transcribe_job`
  (`not self.refiner.is_cloud` gate), ROADMAP.md non-goals.
- Consequence for new features: an optional **local server** with graceful fallback
  (the Ollama pattern: probe `127.0.0.1`, degrade silently) is the accepted way to
  add capability. A required cloud call is disqualifying.

## D02 — The ③ refiner must never translate

LLM cleanup preserves the speaker's language and wording. The prompt alone is not
trusted: a deterministic guard (`_language_preserved`, kana/CJK-based) rejects any
output that switched language and falls back to rule formatting. The streaming path
checks *before the first emit* so a translation can never partially reach the user.

- Enforced: `koe/refiner.py:_guard`, `refine_stream` (checked-before-emit),
  `tests/test_pure.py::test_language_guard_*`.
- Corollary: translation is a *separate* module (`koe/translator.py`) with the
  opposite contract and no language guard. Keep them separate.

## D03 — Refiner default is `auto`; on the owner's voice, `rules` beats `ollama`

Personal bench v1 (9 JP samples): `ollama` was **net-negative** (5.4% → 7.1% CER) —
it paraphrased against instruction (verb endings, particles) and hallucinated
Chinese on code-switching. `rules` never alters content (`final ≡ raw`). The shipped
default stays `auto` (rests on one speaker's samples; don't generalize), but for the
owner's own use `rules` is the measured winner.

- Evidence: BENCHMARK.md v1. Re-run `bench.py sweep` before changing defaults.

## D04 — The terminology dictionary is the main accuracy lever

Two mechanisms, both local, no engine changes: (1) decode-time bias via Whisper
`initial_prompt` — the *only* lever that can fix homographs (`意思`→`issue`),
because it acts before wrong characters exist; (2) safe post-hoc `wrong => right`
rules for unambiguous katakana. Together: worst code-switch sample 45.6% → 8.8%,
mean 5.4% → 1.3% (BENCHMARK v2). The user-correction loop (tray →
`Dictionary.learn`) appends rules so errors self-heal.

- Enforced: `koe/dictionary.py`, `tests/test_pure.py::test_dictionary_*`.
- **Rejected with data:** prefixing a natural-language *demo sentence* to
  `initial_prompt` ("…such terms are written in English…"). Whisper treats the
  prompt as prior transcript, not instructions: it failed to fix the target AND
  regressed a clean sample (0% → 33.3%). The prompt stays a plain listing:
  `用語: A、B、C。` (BENCHMARK v2, dictionary.py comment).
- Known hard limit: true homographs (`クローン` = clone vs close) need sentence
  context; a blind rule would corrupt genuine uses. Left unresolved deliberately.

## D05 — `127.0.0.1`, never `localhost`

On Windows, resolving `localhost` incurs a ~2 s IPv6→IPv4 fallback delay per
request. All local-server URLs use the literal IP. Also: one
`requests.Session(trust_env=False)` per *thread* (refiner's shared session is used
by the transcribe path; translator and responder own separate sessions) — sessions
are not thread-safe and proxy lookups add latency.

- Enforced: `koe/config.py` (comment on `ollama_url`), `koe/refiner.py`,
  `koe/translator.py`, `koe/responder.py` (per-instance sessions).

## D06 — Streaming output is sentence-chunked via `_find_boundary`

Ollama streams tokens; we emit only at sentence boundaries so injected text never
flickers mid-word. ASCII `.!?` end a sentence **only before whitespace** (protects
`3.5`, `v1.2`); a trailing `.` waits for the next chunk. Full-width `。！？` and
newline always end one. This tiny function is the backbone of every streaming
feature — reuse it, don't reinvent it.

- Enforced: `koe/refiner.py:_find_boundary`, `tests/test_pure.py::test_boundary_*`.

## D07 — Preroll ring buffer (always-on mic)

Drivers take ~100–300 ms to start a stream; without preroll the first word is
clipped ("…ello"). A continuously-running input stream keeps a ~0.3 s ring; a take
is seeded from it. If the mic can't stay open, degrade to per-take open (no
preroll) rather than fail. The mic starts warming *during* model load.

- Enforced: `koe/recorder.py`, `koe/app.py:load_model`.

## D08 — Hotkeys: raw hook, side-specific names, self-event suppression

`keyboard`'s high-level on_press_key is unreliable for modifiers. We hook raw
events and match on canonical *side-specific* names ("right ctrl"), falling back to
scan codes only for nameless keys (scan codes overlap: right ctrl shares 29 with
left ctrl). Never fold "right ctrl" into "ctrl" — the app itself synthesizes
Left-Ctrl+V to paste, and `self._suppress` marks those synthetic events so the
paste can't retrigger the hotkey.

- Enforced: `koe/app.py:_install_hotkey`, `_key_aliases`, `_suppress` flag.

## D09 — Injection default is clipboard-paste; save/restore once per stream

Clipboard + Ctrl+V is the only reliable instant Unicode (Japanese) path across
Windows apps. `type` mode exists for paste-blocking apps; `clipboard` mode for
never-auto-paste. When streaming, the user's clipboard is saved once before and
restored once after the whole stream — not per sentence (flicker, races).

- Enforced: `koe/injector.py`, `koe/app.py:_refine_streaming`.

## D10 — Whisper hallucination defenses are layered and deterministic

(1) decode-time: temperature ladder [0, .2, .4, .6] + compression-ratio /
logprob / no-speech thresholds + repetition_penalty 1.1; (2) post-decode:
`collapse_runaway_repeats` (a short unit repeated 6+ times collapses to one — real
speech never does this); (3) interpreter-only: a blacklist of Whisper's
stock-phrase hallucinations ("Thank you." / "ご視聴ありがとうございました") dropped
only when the clip is < 1.6 s (too short to plausibly contain them).

- Enforced: `koe/engine.py:transcribe`, `koe/formatter.py`,
  `interpreter.py:_is_hallucination`, tests.

## D11 — Interpreter pipeline: 3 decoupled stages, 1 ordered consumer

capture thread → raw-block queue → segmenter (main thread, energy VAD) →
utterance queue → single transcriber thread. Capture never blocks on the GPU; a
single consumer keeps captions in spoken order. faster-whisper is not streaming,
so utterances are cut at silence gaps (0.6 s hang) or a hard cap (`--max-seg`).
LLM work (translation inline after captioning; suggestions on a separate worker
with coalescing) must never stall the caption path.

- Enforced: `interpreter.py` (`_Capture`, `_Transcriber`, `_SuggestWorker`;
  suggest queue only enqueued when idle).

## D12 — VAD threshold is auto-calibrated, as a pure function

Startup measures ~1 s of loopback RMS; threshold = low percentile (p35, robust to
speech slipping into the window) × margin (2.5), clamped to [0.005, 0.03] — digital
silence can't drive it to 0, loud calibration audio can't gate out speech (speech
RMS ≈ 0.06). `--threshold` pins it; `--no-calibrate` uses the static default.
`calibrate_threshold` is pure and unit-tested; `_measure_noise` does the I/O.

- Enforced: `interpreter.py:calibrate_threshold` + 5 tests.

## D13 — Language identity checks are script-based, not model-based

Cheap deterministic charset checks are trusted over LLM judgment everywhere:
`_has_cjk` (JP/EN direction), `_has_kana` (Japanese vs Chinese — kana exists only
in Japanese), `_SIMPLIFIED` set (simplified-only hanzi whose JP glyph differs →
precise "Chinese leaked" signal; deliberately excludes shinjitai shared with JP).
`already_in_target` skips needless translation calls with the same trick.

- Enforced: `koe/refiner.py`, `koe/translator.py`, tests.
- Model note: qwen2.5:**7b** occasionally leaks Chinese into JA output; **14b**
  removes it (validated). Interpreter accepts `--ollama-model` so translation can
  run a stronger model while dictation stays fast on 7b.

## D14 — Bench culture: no change to quality-affecting code without a number

`bench.py` scores the owner's private samples (gitignored — voice never leaves the
machine) with normalized CER (NFKC → casefold → strip P/Z/C except `%`). Every
run/sweep appends to `bench/results.jsonl`. BENCHMARK.md records versioned
snapshots tied to commits. STT-quality changes made "by feel" are not accepted;
see BENCHMARK.md for the metric spec.

- **Rejected:** a public multi-dataset harness. A public run measures Whisper, not
  Koe (the dictionary — Koe's real lever — is inert on generic audio), and
  cross-tool CERs aren't comparable. We *cite* the base model's published JP CER
  instead (BENCHMARK Tier 2 decision).

## D15 — Pure core / I/O edges (the testing contract)

CI (ubuntu, no GPU/mic/Windows) runs `compileall` + `pytest` with only
`pytest requests numpy` installed. Therefore: pure logic lives in importable
module-level functions/classes; Windows-only or heavy imports (`keyboard`,
`sounddevice`, `pyaudiowpatch`, `uiautomation`, `pystray`, `faster_whisper`,
`tkinter`) happen **lazily inside functions**. `selftest.py` is the on-Windows
hardware smoke test. If a new feature's core can't be tested on CI, restructure it
until it can.

- Enforced: `.github/workflows/ci.yml`, import structure of every module,
  `tests/test_pure.py`.

## D16 — Graceful degradation over errors, everywhere

The pipeline must never crash mid-dictation or mid-call: refiner errors → rules
fallback; translator errors → show source text; suggester errors → skip; missing
ollama model → loud stderr warning + fallback model; mic can't open always-on →
per-take mode; CUDA load fails → CPU int8; tray unavailable → console mode. New
code follows the same rule: catch broadly at the boundary, degrade to something
useful, keep a visible warning when silence would mislead (see
`bench.py:_maybe_warn_refiner` — a silent fallback would fake a comparison).

## D17 — No overlay window on the dictation GPU path

A dictation overlay was removed for causing a re-transcription regression. Future
overlay/caption UI must be **display-only** — it may render text the pipeline
already produced, never trigger extra GPU work (ROADMAP note).

## D18 — Config forward/backward compatibility

`Config.load` filters unknown keys (old configs survive upgrades; downgrades
survive new keys). Frozen (PyInstaller) builds keep `config.json`/`dictionary.txt`
next to the .exe (`koe/paths.py`) so the app is portable and user-ownable.

## D19 — Console robustness on Windows

`sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at every CLI entry —
never crash on a cp932 console. Reference files are read with `utf-8-sig` (Notepad
BOMs). Diff markers in bench output are ASCII for the same reason.

## D20 — LLM output length is bounded by input length

`_num_predict`: a post-processor mostly copies its input, so output tokens are
capped ≈ 1.5× input + slack (bounds latency AND stops rambling/invention). Any new
LLM call should set an equivalent explicit bound.

## D21 — Prompt engineering lessons (local 7B models)

What works, learned the hard way: few-shot pairs beat instructions (the refiner
ships 5 curated pairs, including two mirroring real observed failures);
"ABSOLUTE RULE" + language pinned in the *user* prompt too; deterministic guards
behind every prompt rule you actually care about (D02, D13); temperature 0.2 for
mechanical tasks, 0.4 for the reply suggester; `keep_alive: "10m"` keeps the model
resident in VRAM between calls (cold load is seconds).

## D22 — TTS is a local-server ladder: VOICEVOX → SAPI → text-only

Koe Talk's voice follows the Ollama pattern exactly: probe a local VOICEVOX
server (`127.0.0.1:50021`, excellent Japanese, free), fall back to Windows SAPI5
(pyttsx3, optional dep), fall back to text-only — the conversation never dies,
it gets quieter. Both engines return WAV bytes (SAPI via `save_to_file`) so
playback is ONE interruptible sounddevice path, chunked ~50 ms with an epoch
check between chunks. An *explicitly requested* backend that is unavailable
degrades loudly to text (a silent stand-in would fake the experience — the
bench-warning rule).

- Enforced: `koe/voice.py`, `tests/test_talk.py::test_voice_fallback_chain`.
- Pitfall: pyttsx3/SAPI is COM-backed and **thread-confined** — the engine is
  created lazily on the TTS worker thread and only used there.

## D23 — Echo strategy: a ladder of modes, not AEC

`talk_echo_mode: "mute"` (default) ignores the mic while reply audio plays —
structurally echo-proof on open speakers; interruption is the hotkey.
`"headphones"` keeps the mic live and ~0.3 s of sustained voice (3 consecutive
voiced blocks — long enough to reject coughs, short enough to feel instant)
interrupts mid-word. **Rejected for v1:** acoustic echo cancellation — we know
exactly *what text* we spoke and *when*, so a future text-level self-echo check
(v2, `is_self_echo`) beats generic AEC at a fraction of the complexity. Also
rejected: wake words (always-on inference, false triggers, unneeded — VISION).

- Enforced: `talk.py` (mute gate in the audio branch), `TurnEngine.barge_by_voice`,
  `tests/test_talk.py::test_voice_barge_needs_sustained_voice`.

## D24 — End-of-turn is semantic, and biased toward holding

A fixed silence timeout is what makes voice chatbots feel like walkie-talkies.
Instead the wait depends on the trailing cue of what was said: question 450 ms /
complete 650 ms / neutral 1000 ms / incomplete (「…けど」, "and") 2000 ms — one
knob (`talk_patience`) scales all. The **asymmetry principle** governs the cue
lists: a false INCOMPLETE costs ~1 s of patience; a false COMPLETE interrupts
the user mid-thought and costs trust — when unsure, hold. Refinement learned
from tests: particles are two-tier — strong conjunctions (が/けど/ので…)
override Whisper's aggressive 。, weak cues (て/は/を…) only count when
unpunctuated, because 「説明して。」 is a finished request but 「〜があって」
is a breath pause.

- Enforced: `koe/turntaking.py:classify_completeness` + the endpointing tests.
- Tune with data, not feel: `talk.py --debug` prints per-turn gap timelines
  (D14 applies to conversation latency too).

## D25 — Interruption is epoch bumping, never thread-killing

Every committed turn gets a fresh epoch; every downstream artifact (LLM stream,
TTS synthesis, playback chunk, mailbox event) carries its epoch and every stage
drops stale ones. Cancelling = increment the epoch. No thread is killed, no
flag soup, and the classic voice-assistant race conditions become pure-logic
tests (`test_stale_epoch_events_are_ignored`). The *input* side has the same
mechanism: fragments carry a **generation** (`TurnEngine.gen`, bumped on every
turn reset), so STT that was in flight across a reply's end or a barge can
never seed the next turn as a phantom (a backchannel 「うん」 said over the
AI's reply must not get answered).

- Enforced: `TurnEngine.epoch` / `TurnEngine.gen`, epoch checks in `talk.py`'s
  workers and handlers, `test_inflight_fragment_after_reset_is_dropped`.

## D26 — One event mailbox, one decision maker

All threads (mic, STT, LLM, TTS, player, hotkey) post typed events into a
single queue consumed by one loop that drives the pure `TurnEngine`. The engine
is single-threaded *by contract*; time reaches it as counted blocks, so every
live turn-taking bug is reproducible by replaying its event sequence through
the engine on CI. (v2: persist `--debug` event traces for literal replay.)

- Enforced: `talk.py` main loop structure; `TurnEngine` docstring/tests.

## D27 — Fragment STT runs *under* the end-of-turn wait

Speech is cut into short fragments (0.4 s hang — deliberately shorter than the
interpreter's 0.6 s) and transcribed eagerly while the user is still pausing,
so the turn's text — and therefore its completeness cue — is already known when
the silence gate fires. This hides most of faster-whisper's non-streaming
latency inside time we must wait anyway. A turn never commits while a
fragment's STT is pending (no truncated turns).

- Enforced: `talk.py` FRAG_* constants; `TurnEngine._pending_frags`;
  `test_no_commit_while_fragment_stt_is_pending`.

## D28 — The model remembers only what it actually said

The LLM may generate five sentences; if the user barged in after two, history
records two + 「（途中で遮られた）」, and the system prompt tells the model what
that marker means — so it continues like a person who was cut off instead of
repeating itself. Corollaries: user speech during THINKING (nothing spoken yet)
cancels the reply and **merges** the committed text back into the building turn
(「あ、それと…」 extends the same turn), and the provisional user message is
dropped from history until the merged turn commits.

- Enforced: `ConversationHistory` (spoken-only, `interrupted`,
  `drop_pending_user`), `TurnEngine._cancel(merge=...)` + tests.

---

*When you make a new non-trivial decision (or reject an approach with evidence),
append it here in the same format. This file is the project's memory.*
