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
- **Addendum (owner testing, 2026-07):** the "14b removes it" finding was
  validated for *translation* only. Koe Talk's free-form conversation leaked
  Chinese live on **both** 7b and 14b (e.g. "筋力アップ为了什么要练腿？",
  "目标明确很好啊。想从哪里开始练习呢？") — upgrading the model is not a full
  fix for this consumer. `koe/turntaking.py` now carries its own copy of the
  `_SIMPLIFIED` set (`_SIMPLIFIED_CHINESE`, kept dependency-free like
  `_has_cjk_local`) and `sanitize_for_speech` drops the WHOLE sentence on any
  hit — unlike the translator's "retry once, harder," a full LLM retry isn't
  practical against a per-sentence streaming reply, and a per-character strip
  would still leave the rest of a Chinese sentence readable (the set is
  precision-tuned, not exhaustive). Since Koe Talk never legitimately replies
  in Chinese (only JA/EN), the check applies unconditionally, no target-language
  plumbing needed.
- Enforced (Talk): `koe/turntaking.py:sanitize_for_speech`,
  `tests/test_talk.py::test_sanitize_drops_sentence_*`.

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
- **Addendum (owner testing, 2026-07):** embedding the literal
  `INTERRUPTED_MARK` string inside `TALK_SYSTEM_PROMPT` (to explain what it
  means) risks a confused/weak local model echoing that instructional text
  back as if it were its own reply — observed live on qwen2.5:7b under a
  degenerate, un-punctuated merged turn (see D24 addendum below for how that
  turn arose). Same prompt-leakage class as D04's rejected `initial_prompt`
  demo sentence: never trust a prompt rule without a deterministic guard
  behind it (D02's rule, generalized). Fix: `sanitize_for_speech` now strips
  any literal `INTERRUPTED_MARK` occurrence unconditionally, so it can never
  reach the screen or TTS regardless of why the model produced it.

## D24 — addendum (owner testing, 2026-07): `--text` mode's merge window is gated by real TTS playback, not by typing speed

`on_reply_started` (THINKING → SPEAKING) fires only on the `play_start` mailbox
event, which — with a real voice backend (SAPI/VOICEVOX/AivisSpeech) — doesn't
happen until synthesis finishes and playback actually begins (SAPI's first
synthesis in particular can take seconds). A fast typist in `--text` mode will
almost always send their next line while still in THINKING, so `on_barge_key`
treats it as a merge (by design, D24/D28) rather than a fresh turn — every line
appends to one ever-growing, punctuation-free turn instead of becoming a new
one, and the assistant's spoken reply never reaches `ConversationHistory`
(nothing was ever marked "spoken"). This is the turn-taking design working
exactly as specified, not a defect in `TurnEngine` — but it is a real UX gap
specific to keyboard-speed input against real-time TTS. **Not fixed in code**
(no spec authorized changing `--text` mode's defaults); the documented
workaround is `--text --voice-backend text` (the null backend marks a sentence
"spoken" the instant it's synthesized, so turns complete near-instantly) or
simply waiting for the reply to finish playing before typing the next line.
Candidate future spec: default `--text` mode to `voice_backend="text"` unless
`--voice-backend` is explicit, or print a "still speaking…" cue before the
next `you:` prompt.

## D24 — addendum #2 (owner testing, 2026-07): THINKING-state voice detection had no debounce — ambient room noise alone could prevent the AI from ever finishing a reply

`on_block` cancelled+merged on a SINGLE voiced block (100 ms) while
THINKING — no minimum-duration gate at all, unlike SPEAKING's barge-in
(`barge_blocks`, "long enough to reject coughs and echo transients"). In a
live mic test in a normal (non-silent) room, ambient noise (rms ~0.01–0.04,
well below real speech) kept re-triggering the merge-cancel every time, so
the AI never got a single reply through the LLM+TTS pipeline before being
cancelled — every turn accreted into one growing, garbled blob (compounding
with the addendum above and with D28's merge semantics). **Fixed**:
`TurnEngine` now requires `barge_blocks` (default 3 = 300 ms) *consecutive*
voiced blocks during THINKING too, via a new `_think_voice_run` counter reset
alongside `_barge_run` in `_reset_turn`. 300 ms is negligible for a genuine
「あ、それと…」 continuation (a real phrase easily sustains that long) but
rejects single-block noise blips — the same tradeoff already validated for
SPEAKING, now applied symmetrically to THINKING.

- Enforced: `TurnEngine.on_block` (THINKING branch), `_think_voice_run`,
  `tests/test_talk.py::test_thinking_ignores_single_noise_blip`,
  `test_speech_during_thinking_cancels_and_merges` (updated for the debounce).
- **addendum #3 (same session, deeper root cause):** the debounce above was
  necessary but NOT sufficient in the DEFAULT `mute` echo mode. `mute`'s mic
  gate in `talk.py` only skipped blocks during `SPEAKING`, so during the
  (SAPI-inflated, multi-second) `THINKING` window the mic stayed hot and
  sustained room noise still cancel-merged the reply before TTS could start —
  live result: `turns=0`, no reply ever completed the whole session. Fix:
  `mute` mode now skips the mic for the WHOLE floor-held window
  (`THINKING` + `SPEAKING` + `mic_hold_until`), consistent with mute's
  premise that the AI holds the floor from turn commit, not from audio start.
  Merge-on-resume stays available in `headphones` mode (opt-in hot mic);
  in `mute`, F8 is the deliberate interrupt. The `_think_voice_run` debounce
  still governs `headphones` mode.
- Enforced: `talk.py` audio-branch mute gate (`turns.state in (THINKING,
  SPEAKING)`). Note: this is an I/O-edge gate, not pure-testable — it composes
  the pure `TurnEngine` (tested) with the mic-skip policy.

## Backlog (not fixed): per-fragment language auto-detection is fragile

Same live mic test: a short, quiet fragment was transcribed as Korean
(`아니 왜요`) while the owner was speaking Japanese. Koe Talk's fragments are
cut much shorter (0.4 s hang, D27) than dictation's one-shot utterance or the
Interpreter's caption segments, giving Whisper's language auto-detect (D01/
`Config.language = None`, deliberately kept auto to support JP/EN code-
switching, D04) far less acoustic evidence per call — short+quiet fragments
are inherently more likely to be mis-identified as an unrelated language,
producing fluent-sounding garbage that then feeds the merge logic above.
**Deliberately not fixed here**: unlike the debounce bug, this has no
single obviously-correct fix (candidates: bias `language=` from the
conversation's already-established language after turn 1; raise `MIN_SPEECH`
for fragments; add a confidence/no-speech-probability gate) and, per D14,
a quality-affecting STT change needs a bench number before its default
changes — not just live anecdotes. Tracked in the private roadmap backlog;
revisit with `bench.py`-style measurement before changing fragment STT behavior.

## D29 — The Control Center launches services as separate processes, not in-process threads

Koe grew to three end-user services (Dictation tray, Interpreter+captions, Talk),
each a mature standalone script with its own main loop, audio capture, `keyboard`
global hook, and — for the Interpreter overlay — a tkinter root that owns its
thread. A single window to start/stop them on demand had two possible shapes:

- **Rejected — one process, each service a thread.** Would mean rewriting three
  proven pillars to be re-entrant and stoppable mid-flight, and fighting real
  contention: pystray and tkinter both want *the* main thread; two tkinter roots
  (overlay + a control window) in one process is unsupported; a crash in one
  service's thread could take the whole UI down. High risk for zero user benefit.
- **Chosen — the control center only *spawns and tracks child processes*.** Each
  service keeps its exact current entry point and runs isolated. This is the same
  "local server with graceful fallback" instinct as the Ollama/VOICEVOX pattern,
  applied to our own subsystems: one service crashing leaves the others and the
  control window alive, and its status lamp flips to grey on its own (the window
  polls `Popen.poll()` every 700 ms). It also keeps the valuable logic pure and
  CI-testable — `koe/launcher.py` (service data, command building, conflict
  detection, a `ServiceManager` whose spawn call is *injected*) imports nothing
  heavier than `dataclasses`; all subprocess/tkinter I/O lives in
  `koe/launchergui.py` at the edge (invariant 2).

Corollaries locked in during review:
- **Closing the window stops every child it started** (`WM_DELETE_WINDOW` and the
  「すべて終了して閉じる」 button share one handler → `stop_all` → `destroy`). A
  service with no visible controller is worse than having to reopen the window;
  no orphaned python.exe. (Rejected: minimise-to-tray / keep-running — a later
  enhancement, not v1.)
- **"Redo setup" needed a wizard-only mode.** The first draft spawned
  `run.py --setup`, assuming it "exits on its own" — but `--setup` runs the wizard
  then falls through to start the dictation tray, orphaning an untracked instance.
  Added `--setup-only` to `koe/app.py:main` (wizard, then return; no tray). Same
  lesson as D04/D28: a plausible-sounding assumption about a subsystem's behaviour
  is a bug until the code path is actually read.
- **Window form factor, not a tray icon, for v1** — discoverability for the
  「誰でも使える」 audience. Because the UI is a thin edge over `ServiceManager`, a
  tray front-end later is a small addition, not a rewrite.

Enforced/covered by `tests/test_launcher.py` (pure): command shapes, no-double-
spawn, self-exit detection, `stop_all`, mic-conflict detection, headless import.
Spec: docs/specs/control-center.md.

**Addendum (frozen-multiservice spec):** `build_command`'s frozen branch no
longer raises. It now re-invokes `sys.executable` (== `Koe.exe` once packaged)
as `Koe.exe --run <service> …`, dispatched by `koe/cli.py` — a lazy routing
edge that imports only the chosen service's `main()`, never all of them. The
one-folder COLLECT build (not one-file) makes each re-invoke cheap: shared
DLLs already sit on disk, so `Koe.exe --run …` doesn't re-unpack anything.
Spec: docs/specs/frozen-multiservice.md.

---

*When you make a new non-trivial decision (or reject an approach with evidence),
append it here in the same format. This file is the project's memory.*
