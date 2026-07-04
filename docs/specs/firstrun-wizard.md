# Spec: firstrun-wizard — first-launch setup wizard  (status: ready)

## Goal

A non-developer who starts Koe for the first time gets a small window that
(1) says what Koe is and that everything stays on this machine, (2) detects
their GPU and recommends the right Whisper model, (3) lets them pick and TEST
their microphone with a live level meter, (4) explains the hotkey — then saves
`config.json` and hands off to the normal tray startup. Re-runnable any time
with `python run.py --setup`.

## Non-goals

- No model downloading inside the wizard. The existing startup flow already
  downloads on first use and shows status in the tray; the wizard only *warns*
  that the first start downloads ~0.5–1.6 GB. (This sidesteps progress-bar
  integration with huggingface_hub entirely — deliberate scope cut.)
- No hotkey *rebinding* UI (config.json + `--diagnose-keys` cover it; a picker
  is a later spec). Mode choice (toggle/PTT) IS included — it's a radio button.
- No new config keys. The wizard writes existing fields only
  (`model`, `input_device`, `hotkey_mode`). D18 untouched.

## Context to read (exhaustive)

- CLAUDE.md
- koe/app.py (main() and the console flow), koe/config.py, koe/recorder.py
  (level metering pattern), koe/engine.py (`_cuda_available`), koe/overlay.py
  (the repo's tkinter conventions: lazy imports, guarded degradation),
  tests/test_overlay.py (test style)

## Design

### 1. `koe/firstrun.py` — one module, pure logic at top, tkinter below

Pure functions (module level, no tkinter/ctypes/sounddevice imports at module
scope — CI-testable):

```python
def recommend_model(cuda_available: bool) -> tuple[str, str]:
    """(model_name, ja_rationale). CUDA -> ("large-v3-turbo", "GPUを検出しました。
    最高精度の推奨モデルです（初回 約1.6GB をダウンロード）").
    No CUDA -> ("small", "GPUが見つからないため、CPUでも快適な軽量モデルを
    推奨します（初回 約500MB）。精度優先なら後から変更できます").
    WHY in comment: turbo beat large-v3 on the owner's bench (BENCHMARK v0);
    small is the documented CPU fallback in README troubleshooting."""

def input_device_choices(devices: list[dict], default_index: int | None) -> list[tuple[int | None, str]]:
    """Parse sounddevice.query_devices()-shaped dicts into dropdown choices:
    first entry always (None, "既定のマイク（Windows設定に従う）"); then every
    device with max_input_channels > 0 as (index, f"{index}: {name}"), marking
    the default with " ← 既定". Pure: takes plain dicts, so tests feed fakes."""

def level_fraction(rms: float) -> float:
    """Mic RMS -> 0.0..1.0 meter fill. Normal speech RMS is ~0.03–0.1
    (koe/recorder.py's meter docs), so full scale at 0.15 keeps the bar lively
    without pegging: min(1.0, rms / 0.15)."""
```

Wizard entry point (all heavy imports inside):

```python
def run_wizard(cfg) -> bool:
    """Show the 4-step wizard, mutate+save cfg on finish. Returns True if the
    user completed it, False if cancelled/unavailable — the caller proceeds
    with defaults either way (the wizard must never block startup — D16).
    tkinter import failure, any Exception during construction: print one
    stderr line ('! setup wizard unavailable (...) — starting with defaults')
    and return False."""
```

### 2. Wizard structure (single Tk window, 4 steps, Back/Next buttons)

One `tk.Tk` root, title 「Koe — 初回セットアップ」, fixed ~520x420, normal
window (NOT overrideredirect — this one is a real window with a close box).
A dict of step-frames packed/unpacked by index; 「戻る」「次へ」 buttons, last
step's Next reads 「Koeを開始」. Closing the window = cancel (return False,
nothing saved).

- **Step 1 ようこそ**: 2 sentences (what Koe is; 「音声はこのPCの中だけで処理され、
  クラウドには一切送信されません」). Below: GPU status line, initially
  「GPUを確認中…」— `_cuda_available()` runs on a daemon thread (its
  ctranslate2 import can take seconds) posting into a `queue.Queue` that an
  `after(200)` poll reads; result swaps the label to 「NVIDIA GPU: 検出 (CUDA)」
  or 「NVIDIA GPU: なし（CPUで動作します）」.
- **Step 2 モデル**: radio group — recommended option (from
  `recommend_model`, preselected, suffix 「（推奨)」), plus the other two of
  {small, large-v3-turbo, large-v3} with one-line JA descriptions; the
  rationale string shown under the group. Warning line: 「最初の起動時に一度
  だけモデルをダウンロードします（以後オフライン動作)」.
- **Step 3 マイク**: dropdown from `input_device_choices(sd.query_devices(),
  default_input_index)` (sounddevice imported lazily HERE; on failure the step
  shows 「マイク一覧を取得できませんでした — 既定のマイクを使用します」 and
  stays skippable). Below: `ttk.Progressbar` level meter driven by an
  `sd.InputStream` (16 kHz mono, the Recorder callback pattern: store latest
  RMS in a one-slot box; `after(100)` sets the bar to `level_fraction(rms)`),
  label 「マイクに向かって話すとバーが動きます」. The stream opens when the
  step is shown, closes when leaving the step or the window (try/except —
  a busy mic must not kill the wizard). Changing the dropdown reopens the
  stream on the selected device.
- **Step 4 ホットキーと完了**: explains 「{cfg.hotkey} を1回押して話し、もう
  一度押すと文字になります」; radio toggle(推奨)/ptt with one-line JA
  descriptions; summary block (model / mic / mode); button 「Koeを開始」→
  write choices into cfg, `cfg.save()`, destroy window, return True.

### 3. Hook into startup — `koe/app.py:main()`

Immediately before the existing `cfg = Config.load()` line:

```python
from .config import CONFIG_PATH
first_run = not CONFIG_PATH.exists()   # BEFORE load() — load() creates the file
```

after `cfg = Config.load()` and the CLI overrides:

```python
if (first_run or "--setup" in argv) and "--console" not in argv:
    try:
        from .firstrun import run_wizard
        run_wizard(cfg)
    except Exception as exc:   # belt over run_wizard's own braces
        print(f"[setup wizard unavailable: {exc}] starting with defaults.")
```

`--setup` added to the module docstring and to README troubleshooting
(「設定をやり直す」) — reviewer writes README text.

## File-by-file changes

| File | Change |
|------|--------|
| koe/firstrun.py | new (pure helpers + wizard) |
| koe/app.py | first_run detection + wizard hook + docstring line |
| tests/test_firstrun.py | new, pure tests only |

## Tests to write (tests/test_firstrun.py)

- `test_recommend_model_gpu_and_cpu` — cuda→large-v3-turbo, else small; both rationales non-empty JA
- `test_device_choices_filters_outputs` — fake dicts: output-only device excluded, input device formatted with index
- `test_device_choices_marks_default` — default index gets the ← marker; entry 0 is always (None, 既定…)
- `test_device_choices_empty_list` — returns just the default entry
- `test_level_fraction_clamps` — 0→0.0, 0.15→1.0, 1.0→1.0, monotonic in between
- `test_firstrun_module_imports_headless` — `import koe.firstrun` with tkinter/sounddevice blocked via a `builtins.__import__` shim (the overlay test's technique)
- `test_app_still_imports` — `import koe.app`

## Acceptance criteria

1. Full suite green under CI constraints; `python -m compileall koe` clean.
2. `koe/firstrun.py` module scope imports nothing beyond stdlib-pure
   (no tkinter/ctypes/sounddevice at import time).
3. With an existing config.json and no `--setup`, `koe/app.py:main()` behavior
   is unchanged (verified by reading the diff: the hook is gated on
   `first_run or "--setup"`).
4. Cancelling the wizard (window close) saves nothing and startup proceeds.
5. Every constant (0.15 scale, sizes in rationale strings) carries a WHY comment.
6. Self-check table submitted, one row per criterion.

## Manual checks for the owner (Windows)

- Delete/rename config.json → `run.bat`: wizard appears; complete it; confirm
  config.json contains the chosen model/input_device/hotkey_mode and the tray
  starts normally afterward.
- `python run.py --setup` re-opens it with current values as defaults.
- Mic meter moves when speaking; switching the dropdown re-targets the meter.
- On a CPU-only machine (or `--device cpu` sanity check): step 1 says CPU,
  step 2 preselects small.
- Close the wizard mid-way → Koe starts with defaults, no crash, no config loss.

## Known pitfalls

- `Config.load()` CREATES config.json — the first_run check must run before it.
- tkinter mainloop is main-thread-only; `_cuda_available` must not run on it
  (multi-second import stall) — hence the thread + after() poll.
- An `sd.InputStream` left open when the window is destroyed keeps the mic
  busy — close it in the step-leave handler AND in a `WM_DELETE_WINDOW`
  handler (set `protocol("WM_DELETE_WINDOW", ...)`).
- The wizard runs before the model exists — do not import faster_whisper or
  construct TranscriptionEngine anywhere in it.
