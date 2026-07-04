# Spec: aivisspeech — AivisSpeech rung in the TTS ladder  (status: implemented)

## Goal

Koe Talk's voice ladder gains **AivisSpeech** (a local TTS server that speaks
the VOICEVOX-compatible API on `127.0.0.1:10101`, with notably more natural
Japanese than VOICEVOX). New ladder, best voice first:

    AivisSpeech → VOICEVOX → SAPI → text-only

Users who already run AivisSpeech get the better voice with zero config; the
existing `VoicevoxVoice` class is reused as-is (same API), so this is mostly a
probing/selection change.

## Non-goals

- No speaker/style *picker* UI (config int + a startup hint line is enough for
  v1; GET /speakers lists styles on either server).
- No Style-Bert-VITS2 direct integration (AivisSpeech IS its packaged serving
  layer — that's the point of this rung).
- No new dependencies. Do not modify VOICEVOX behavior for existing users:
  if only VOICEVOX is running, everything works exactly as today.

## Context to read (exhaustive)

- CLAUDE.md
- koe/voice.py (whole file), koe/config.py (Koe Talk block), talk.py (only:
  where `build_voice` is called and the startup banner), tests/test_talk.py
  (the voice tests)

## Design

### 1. Config (koe/config.py — additive, D18)

```python
# AivisSpeech: a VOICEVOX-compatible local TTS server (default port 10101)
# with more natural Japanese voices. When voice_backend="auto" it is probed
# BEFORE VOICEVOX — better voice wins when both are running.
aivisspeech_url: str = "http://127.0.0.1:10101"   # 127.0.0.1, same reason as ollama_url
# Style id on the AivisSpeech server. Speaker ids are NOT shared between
# servers (a VOICEVOX id is meaningless on AivisSpeech — that's why each rung
# carries its own). 888753760 = Anneli ノーマル, the default install's voice;
# GET /speakers on the server lists everything installed.
aivisspeech_speaker: int = 888753760
```

`voicevox_url` / `voicevox_speaker` are untouched.

### 2. koe/voice.py

- `_voicevox_available(url)` stays as the generic compatible-server probe
  (GET /version) — reuse it for both URLs; do not duplicate it.
- `pick_voice_backend` (pure) gains one value and one argument, keeping its
  signature style:

```python
def pick_voice_backend(requested: str, aivis_ok: bool, voicevox_ok: bool,
                       sapi_ok: bool) -> str:
    """"auto" walks aivisspeech -> voicevox -> sapi -> text (best voice first).
    Explicit "aivisspeech" / "voicevox" honored if reachable, else "text"
    LOUDLY (the existing is_unwanted_fallback contract, unchanged)."""
```

  Update all existing callers/tests for the new parameter (test updates are
  mechanical: insert the new flag).
- `is_unwanted_fallback` needs no logic change ("aivisspeech" is just another
  explicit value that either got honored or degraded to "text").
- `build_voice`:

```python
aivis_ok = _voicevox_available(cfg.aivisspeech_url)
vv_ok = _voicevox_available(cfg.voicevox_url)
got = pick_voice_backend(want, aivis_ok, vv_ok, _sapi_available())
if got == "aivisspeech":
    return VoicevoxVoice(cfg.aivisspeech_url, cfg.aivisspeech_speaker, name="aivisspeech")
if got == "voicevox":
    return VoicevoxVoice(cfg.voicevox_url, cfg.voicevox_speaker)
```

  `VoicevoxVoice.__init__` gains `name: str = "voicevox"` (keyword, last) and
  sets `self.name = name` — the class is otherwise untouched (same API on both
  servers). The `Voice.name` class attribute pattern stays for the others.

### 3. talk.py

- Accept `--voice-backend aivisspeech` (no parsing change needed — the value
  is passed through; just add it to the docstring's `--voice-backend` line).
- No banner change needed: it already prints `voice → {voice.name}`, which now
  says `aivisspeech` when that rung wins.

## File-by-file changes

| File | Change |
|------|--------|
| koe/config.py | 2 keys with comments |
| koe/voice.py | probe both URLs; pick chain + named VoicevoxVoice instance |
| talk.py | docstring line only |
| tests/test_talk.py | update pick tests for new arg; add cases below |

## Tests to write / update (tests/test_talk.py)

- Update `test_voice_fallback_chain` for the 4-arg signature; add:
  - auto with aivis+voicevox both up → "aivisspeech"
  - auto with only voicevox → "voicevox"
  - explicit "voicevox" while only aivis is up → "text" (explicit ask not honored → loud degrade)
  - explicit "aivisspeech" while up → "aivisspeech"
- `test_fallback_warning_only_when_request_not_honored`: add
  `is_unwanted_fallback("aivisspeech", "text") is True` and
  `("aivisspeech", "aivisspeech") is False`.
- `test_voicevox_voice_name_override` — `VoicevoxVoice("http://x", 1)` has
  name "voicevox"; with `name="aivisspeech"` reports "aivisspeech" (construct
  only; no HTTP).

## Acceptance criteria

1. Full suite green under CI constraints; compileall clean.
2. With neither server running and no request, behavior is identical to today
   (sapi → text), and with only VOICEVOX running it still picks voicevox with
   the same speaker id as before (no regression for existing users).
3. `pick_voice_backend` remains pure (no I/O) and fully covered by the tests above.
4. Both new config keys carry the WHY comments verbatim from this spec.
5. Self-check table submitted, one row per criterion.

## Manual checks for the owner (Windows)

- Install/run AivisSpeech; `python talk.py` banner shows `voice → aivisspeech`;
  reply audio uses the Anneli voice; stop AivisSpeech mid-session → next
  sentence degrades (one warning, text continues — the D16 path).
- With both servers running, confirm AivisSpeech wins; `--voice-backend
  voicevox` forces VOICEVOX back.
- Record the 「はい」 synth latency for both servers in BENCHMARK.md (D14 —
  the ladder reorder is a quality claim; give it a number).

## Known pitfalls

- Speaker ids are server-specific — never pass `voicevox_speaker` to the
  AivisSpeech rung (a wrong id turns every synth into a silent text fallback).
- AivisSpeech's first synthesis after boot loads the model (seconds) — the
  existing PREWARM_EPOCH warm-up in talk.py already covers this; don't add
  another.
