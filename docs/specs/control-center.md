# Spec: control-center — one launcher to start/stop every Koe service  (status: implemented)

## Goal

Koe now has three end-user services (Dictation, Interpreter+captions, Talk),
each its own script with its own flags. A non-developer should not have to
remember `interpreter.py --to ja --overlay --suggest`. Ship one small **control
window** — the first thing you double-click — with one row per service: a status
lamp and a start/stop button. Start any service, stop it, start another, any
time. Closing the window stops everything it started (no orphan processes the
user can't see).

Each service runs as its **own child process** (not an in-process thread).
Interpreter and Talk are mature console programs with their own main loops, mic
capture, `keyboard` global hooks, and (Interpreter) a tkinter overlay that owns
its main thread — folding them into one process would mean a risky rewrite and
audio/main-thread contention. Separate processes also satisfy graceful
degradation (D16): one service crashing leaves the others and the control center
alive, and the lamp flips to grey on its own.

## Non-goals

- **No frozen-.exe packaging in this spec.** `build_command` targets the
  source/venv install (what the owner runs today: `.venv\Scripts\python.exe`).
  It branches on `frozen` only far enough to raise a clear, single-line
  "not supported in the packaged build yet" — unifying the PyInstaller entry so
  one .exe can dispatch to each pillar is the release-pipeline spec's job, noted
  there as follow-up. Do not touch run.py's/exe bootstrap here.
- **No tray version.** The window is the v1 form factor (discoverability for the
  "誰でも使える" audience). The UI is a thin edge over a pure `ServiceManager`, so
  a tray front-end later is a small addition, not a rewrite — say so in the
  module docstring, build nothing for it.
- **No per-service settings UI.** The control center launches services with
  config-derived defaults. Tuning still lives in config.json and each service's
  own menu/flags (Dictation's tray menu is untouched). The only new config keys
  are the three interpreter launch defaults below.
- **No "minimize to tray / keep running in background".** Closing the window
  stops all children. A persist-in-tray behavior is a documented later
  enhancement, not v1.
- **No auto-start of anything.** The window opens with everything stopped; the
  user picks.

## Context to read (exhaustive)

- CLAUDE.md (invariants 1,2,3,5; the architecture map)
- koe/config.py (dataclass + unknown-key filtering — how to add fields)
- koe/paths.py (`data_dir()` = repo root from source, exe dir when frozen)
- koe/app.py `main()` (dictation entry; `--setup` re-opens the wizard),
  interpreter.py `main()` (the `--to/--overlay/--suggest` flags),
  talk.py `main()` (reads config defaults when flags absent)
- koe/overlay.py + koe/firstrun.py (the repo's tkinter conventions: lazy imports
  inside functions, guarded degradation, `after()` polling, WM_DELETE_WINDOW)
- tests/test_firstrun.py, tests/test_overlay.py (pure-test + headless-import style)

## Design

### 1. `koe/launcher.py` — pure core (no tkinter/subprocess at module scope)

Service definitions as data + three pure functions + one manager whose only I/O
(spawning) is an **injected callable**, so every branch is CI-testable.

```python
@dataclass(frozen=True)
class Service:
    key: str        # "dictation" | "interpreter" | "talk"  (stable id)
    label: str      # JA display name, e.g. "ディクテーション（音声入力）"
    script: str     # "run.py" | "interpreter.py" | "talk.py" (repo-root relative)
    uses_mic: bool  # True if it captures the microphone (Dictation, Talk).
                    # Interpreter captures system-audio loopback, not the mic → False.

def services() -> list[Service]:
    """The three end-user services in display order. Bench is dev-only, excluded."""

def interpreter_args(cfg) -> list[str]:
    """Flags after interpreter.py, from config:
    ["--to", cfg.interpreter_to] only when interpreter_to is truthy (""=captions
    only, no --to); "--overlay" when cfg.interpreter_overlay; "--suggest" when
    cfg.interpreter_suggest. Order fixed: --to first, then --overlay, --suggest.
    WHY config-driven: the owner's proven-good combo is --to ja --overlay
    --suggest, but captions-only / no-suggest must be reachable without code."""

def service_args(service, cfg) -> list[str]:
    """Per-service flags. dictation → []; talk → [] (both read config directly);
    interpreter → interpreter_args(cfg)."""

def build_command(service, cfg, *, python_exe, repo_root, frozen=False) -> list[str]:
    """Full argv to spawn: [python_exe, <repo_root>/<script>, *service_args].
    frozen=True raises NotImplementedError with a one-line message (see non-goal).
    Pure: no os/subprocess — takes python_exe and repo_root as plain args so tests
    pass fakes ('/py', '/repo')."""

def audio_conflicts(running_keys, all_services) -> list[str]:
    """Keys among running_keys whose services set uses_mic — returned only when
    ≥2 of them would fight over the one microphone (Dictation + Talk). One or zero
    mic users → []. Interpreter never conflicts. Lets the UI warn, not block."""
```

`ServiceManager` — process lifecycle, spawning injected:

```python
class ServiceManager:
    def __init__(self, spawn):
        """spawn: (argv: list[str]) -> handle. handle must expose .poll()
        (None while alive) and .terminate(). Real impl injects a subprocess.Popen
        wrapper (I/O edge, below); tests inject a fake."""
    def is_running(self, key) -> bool
        # a handle exists AND handle.poll() is None. A crashed/exited child
        # (poll() is not None) reads as stopped — the lamp self-heals.
    def start(self, key, argv) -> bool
        # no-op returning False if already running; else spawn, store handle, True.
        # (argv passed in — built by the caller via build_command — so the manager
        #  stays free of config/path knowledge and is trivially testable.)
    def stop(self, key) -> bool
        # terminate the handle if running, drop it, True; else False.
    def toggle(self, key, argv) -> bool   # start if stopped else stop
    def running_keys(self) -> list[str]   # keys whose handle.poll() is None
    def stop_all(self) -> None            # terminate every live child (for shutdown)
```

Every `terminate()`/`poll()` call is wrapped so a dead handle can't raise into
the UI (Popen on an already-exited process is fine, but a foreign handle might
not be — be defensive).

### 2. `koe/launchergui.py` — the window (all heavy imports inside functions)

```python
def main() -> None:
    """Entry for launcher.py. Build config, ServiceManager with the real spawner,
    show the window, run the tk mainloop. On tkinter import failure print one
    stderr line and exit 1 (there is no headless fallback for a GUI launcher)."""
```

- One `tk.Tk`, title 「Koe コントロールセンター」, fixed ~440x300, normal window
  with a close box, `resizable(False, False)`.
- Header label: 「使いたい機能を開始／停止できます。音声はこのPCの中だけで処理されます。」
  (restate the local-only promise — this is the front door many users see first).
- One row per `services()` entry: a status lamp (a small Canvas dot, green
  `#2f9e44` when running / grey `#adb5bd` when stopped) + the JA label + a
  start/stop button whose text is 「開始」 when stopped, 「停止」 when running.
- Clicking a row's button: build argv via `build_command(service, cfg,
  python_exe=sys.executable, repo_root=data_dir())`, then `mgr.toggle(key, argv)`;
  refresh the row. If starting would create a mic conflict
  (`audio_conflicts` non-empty after the toggle), show a non-blocking warning
  label under the rows: 「マイクを使う機能が複数動いています（ディクテーションと会話）。
  片方だけの利用をおすすめします。」 — warn, never block (invariant 3).
- Footer: 「初回セットアップをやり直す」 button → spawn `run.py --setup-only` as a
  one-shot child (see the app.py change below); and 「すべて終了して閉じる」
  button → `mgr.stop_all()` then `root.destroy()`.
  (SPEC FIX after review: the draft said spawn `run.py --setup`, claiming "it
  exits on its own once the wizard closes." That is FALSE — plain `--setup` runs
  the wizard then falls through to start the dictation tray, which would leave an
  untracked dictation instance the window can't stop and that orphans on close.
  The correct behavior needs a wizard-only mode: `--setup-only`, added to
  koe/app.py:main, runs the wizard and returns without starting the tray.)
- `WM_DELETE_WINDOW` (the [X]) → same as すべて終了して閉じる (stop_all + destroy).
  No orphan children ever outlive the window.
- `after(700)` poll refreshes every lamp/button from `mgr.is_running(key)` so a
  child that exits or crashes on its own flips its lamp to grey without a click.
  (SPEC FIX after review: the poll must ALSO recompute the mic-conflict warning,
  not just the lamps — otherwise, if a mic service self-exits, the warning
  lingers on stale state. Factor the warning recompute into one helper called by
  both the toggle handler and the poll.)
  (700 ms: responsive enough, far below any human "did it stop?" patience,
  cheap — three poll() calls.)

The real spawner (I/O edge, defined in launchergui.py, injected into
ServiceManager):

```python
def _spawn(argv):
    import subprocess, sys
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # 0 on non-Windows
    return subprocess.Popen(argv, cwd=str(data_dir()), creationflags=flags)
```

CREATE_NEW_CONSOLE gives each service its own visible console (captions/logs/
conversation are the point during early release; Interpreter's overlay shows on
top regardless). `getattr(..., 0)` keeps it importable off-Windows.

### 3. `launcher.py` (repo root) + `Koe.bat`

- `launcher.py`: two lines — `from koe.launchergui import main` / `main()`.
  MUST be named `launcher.py`, NOT `koe.py` (a root `koe.py` would shadow the
  `koe` package on import). Module docstring: what the control center is + the
  "window is v1, tray is a later thin swap" note.
- `Koe.bat`: mirror run.bat exactly (`cd /d "%~dp0"`, `.venv\Scripts\python.exe
  launcher.py %*`, `echo.`, `pause`) so import errors stay visible — the honest
  early-release default. run.bat/run-admin.bat are untouched (Dictation-direct
  stays available for power users).

### 4. `koe/config.py` — three new fields (Control Center block)

```python
# --- Control Center (launcher.py): default flags when it starts a service ---
# Interpreter's proven-good combo is --to ja --overlay --suggest. These let a
# user pick captions-only or drop suggestions without editing code. Dictation
# and Talk take no launch flags — they read the rest of this config directly.
interpreter_to: str = "ja"        # target translation language; "" = captions only
interpreter_overlay: bool = True  # translucent on-screen caption strip
interpreter_suggest: bool = True  # F9 reply suggestions
```

Unknown-key filtering already makes these forward/backward compatible (D18).

## File-by-file changes

| File | Change |
|------|--------|
| koe/launcher.py | new — pure Service data, build_command, audio_conflicts, ServiceManager |
| koe/launchergui.py | new — tkinter window + real `_spawn` (I/O edge) |
| launcher.py | new — 2-line root entry → koe.launchergui.main |
| Koe.bat | new — double-click entry (mirrors run.bat) |
| koe/config.py | +3 fields (interpreter_to/overlay/suggest) with WHY comment |
| koe/app.py | +`--setup-only` (wizard then return, no tray) — review fix, see §2 |
| tests/test_launcher.py | new — pure tests only |
| README.md / README.ja.md | reviewer adds a "起動（コントロールセンター）" section |
| ROADMAP.md | reviewer moves the launcher into Shipped |

## Tests to write (tests/test_launcher.py — pure, CI-safe)

- `test_services_are_the_three_end_user_ones` — keys == {dictation, interpreter,
  talk} in display order; bench absent; each has a non-empty JA label + script.
- `test_interpreter_args_full_combo` — cfg(to="ja",overlay=True,suggest=True)
  → ["--to","ja","--overlay","--suggest"] in that order.
- `test_interpreter_args_captions_only` — to="" → no "--to"; overlay off → no
  "--overlay"; suggest off → no "--suggest" (drive each independently).
- `test_build_command_shapes` — dictation → ["/py","/repo/run.py"];
  interpreter full combo → ["/py","/repo/interpreter.py","--to","ja",...];
  talk → ["/py","/repo/talk.py"]. Uses python_exe="/py", repo_root="/repo".
- `test_build_command_frozen_raises` — frozen=True → NotImplementedError.
- `test_manager_start_stop_toggle` — FakeSpawn returns a FakeProc (poll()→None
  until terminate() sets it): start→running; second start→False, no 2nd spawn;
  stop→not running + terminate called; toggle flips both ways.
- `test_manager_detects_self_exit` — a FakeProc whose poll() returns 0 reads as
  not running without any stop() call (lamp self-heal).
- `test_manager_stop_all` — start all three, stop_all terminates each, running
  set empties.
- `test_audio_conflicts` — {dictation,talk}→both keys; {interpreter}→[];
  {dictation}→[]; {dictation,interpreter}→[] (only the mic pair conflicts).
- `test_launcher_module_imports_headless` — `import koe.launcher` with tkinter
  AND subprocess blocked via a `builtins.__import__` shim (overlay test's
  technique) — proves nothing heavy is imported at module scope.

## Acceptance criteria

1. Full suite green under CI constraints (`pip install pytest requests numpy`);
   `python -m compileall koe` clean.
2. `koe/launcher.py` imports nothing beyond stdlib-pure at module scope (no
   tkinter/subprocess/sounddevice) — proven by the headless-import test.
3. `ServiceManager` never spawns a second child for an already-running key and
   reads a self-exited child as stopped (verified by tests, not just by reading).
4. Closing the window (button or [X]) calls `stop_all` before destroy — no child
   outlives the window. (Verified by reading the WM_DELETE_WINDOW wiring.)
5. Existing entry points unchanged: run.py/run.bat/run-admin.bat, interpreter.py,
   talk.py behave exactly as before (this spec only ADDS files + 3 config keys).
6. Every constant (700 ms poll, lamp colors, window size) carries a WHY comment.
7. Self-check table submitted, one row per criterion.

## Manual checks for the owner (Windows — the implementer can't run these)

- Double-click `Koe.bat` → the control window appears, all three lamps grey.
- 「ディクテーション」開始 → its tray icon appears, hotkey works; 停止 → gone.
- 「通訳＋字幕」開始 → interpreter console + overlay appear over a video; F9 gives
  a suggestion; 停止 → both close.
- 「会話」開始 → Talk console; speak, get a reply; 停止 → closes.
- Start Dictation AND 会話 together → the mic-conflict warning line shows.
- Close a service's console window directly (not via the button) → within ~1 s
  its lamp goes grey on its own.
- 「すべて終了して閉じる」 or the window [X] → every started service closes, no
  leftover python.exe in Task Manager.
- 「初回セットアップをやり直す」 → the wizard opens (same as run.py --setup).

## Known pitfalls

- A root `koe.py` would shadow the `koe` package — the entry MUST be
  `launcher.py`.
- `CREATE_NEW_CONSOLE` does not exist on non-Windows — use
  `getattr(subprocess, "CREATE_NEW_CONSOLE", 0)` so launchergui stays importable
  on CI (even though CI never spawns).
- `subprocess.Popen(...).poll()` is None while alive, an int once exited — the
  running check is `poll() is None`, not truthiness (exit code 0 is falsy!).
- pystray (Dictation's tray) and this tkinter window are in **separate
  processes** — no main-thread conflict, because Dictation is a child, not
  in-process.
- Do not import faster_whisper / sounddevice / keyboard anywhere in launcher.py
  or launchergui.py — the control center only *spawns* the services that use
  them; it must start instantly and stay CI-importable.
- `terminate()` on Windows is TerminateProcess (no graceful SIGINT); that's fine
  for these services (no unsaved state — Dictation persists on each take, Talk/
  Interpreter hold nothing durable). Wrap it so a foreign/dead handle can't raise.
