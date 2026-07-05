# Spec: frozen-multiservice — one Koe.exe that runs every service  (status: ready)

## Goal

Today the packaged release (`koe.spec` → `dist/Koe/Koe.exe`) has a single entry,
`run.py`, so the shipped .exe is **dictation only**. The Interpreter, Talk, and
the new Control Center do not exist in the release a non-developer downloads —
exactly the "誰でも使える" audience the .exe is for. Make one `Koe.exe` able to
*be* any service, dispatched by argument:

- `Koe.exe`  (double-click, no args)      → the **Control Center** window
- `Koe.exe --run dictation [flags]`        → dictation (koe.app.main)
- `Koe.exe --run interpreter [flags]`      → interpreter.main
- `Koe.exe --run talk [flags]`             → talk.main

The Control Center, when frozen, launches each service by re-invoking **itself**
(`sys.executable` = `Koe.exe`) as `Koe.exe --run <service> …` instead of
`python interpreter.py …`. One-folder build, so re-launching Koe.exe is cheap
(shared DLLs on disk). This closes the gap flagged in control-center.md's
non-goals and D29.

## Non-goals

- **No source-path change.** `run.py` (dictation) and `launcher.py` (Control
  Center) stay the source entry points, exactly as today. This spec adds a
  *frozen* dispatcher; from source nothing about how the owner runs Koe changes.
- **No one-file build, no code signing, no CUDA/bundle-size rework.** koe.spec
  stays a one-folder COLLECT; the only build changes are the entry script and the
  dependency-collection list. Signing/SmartScreen stays as documented in
  release-pipeline.md.
- **No new per-service behavior.** Each service's own logic/flags are untouched;
  we only add a routing layer in front of their existing `main()`s.
- **This spec cannot be validated on CI.** The PyInstaller build needs Windows +
  CUDA + the audio stack. CI/this author verify only the *pure* routing logic and
  that the new modules compile/import headless. The actual .exe is proven by the
  owner building it and running each `--run` target (see Manual checks). Treat
  the koe.spec dependency list as a *first, reasoned draft* that the owner will
  complete by running the exe and adding whatever `hiddenimports` a missing-module
  traceback names — that iteration is expected, not a failure.

## Context to read (exhaustive)

- CLAUDE.md (invariants 1,2,3; lazy Windows imports)
- koe.spec (current build: single `run.py` entry, `collect_all` list, CUDA dedup)
- koe/launcher.py + koe/launchergui.py (Control Center: `build_command`'s frozen
  branch currently *raises*; `_on_setup` spawns the source `run.py --setup-only`)
- koe/app.py `main(argv=None)` (takes an argv list; imports `keyboard` at module
  scope — so it must be imported LAZILY by the dispatcher, never at its top)
- interpreter.py `main()` + talk.py `main()` (both read `sys.argv[1:]` themselves
  and call `sys.stdout.reconfigure(...)` — they do NOT take an argv param)
- koe/paths.py (`data_dir()`, `sys.frozen` handling), docs/DECISIONS.md D29
- docs/specs/control-center.md (the piece this completes), release-pipeline.md
- tests/test_launcher.py (extend it — same pure-test style)

## Design

### 1. `koe/launcher.py` — pure additions (routing + setup command + frozen build)

```python
RUN_TARGETS = ("dictation", "interpreter", "talk")  # control is the default

def parse_run_target(argv) -> tuple[str, list[str]]:
    """Map process args to (target, rest). If argv starts with "--run <t>" and t
    is a known service → (t, argv-after-those-two-tokens). Anything else, incl.
    an empty argv or an unknown/missing target → ("control", argv). Pure; the
    dispatcher (koe/cli.py) turns the target into an actual main() call.
    WHY 'unknown → control': a mistyped --run opens the front door, never a
    silent crash (invariant 3)."""

def setup_command(*, python_exe, repo_root, frozen: bool) -> list[str]:
    """Argv for the Control Center's 'redo setup' one-shot (wizard, then exit —
    koe/app.py:main --setup-only). frozen → [python_exe, "--run", "dictation",
    "--setup-only"]; source → [python_exe, "<repo_root>/run.py", "--setup-only"].
    Symmetric with build_command so both modes stay in one tested place."""
```

`build_command` — replace the `NotImplementedError` frozen branch with a real one:

```python
def build_command(service, cfg, *, python_exe, repo_root, frozen=False):
    args = service_args(service, cfg)
    if frozen:
        # python_exe is sys.executable == Koe.exe; re-invoke self as the service.
        return [python_exe, "--run", service.key, *args]
    return [python_exe, f"{repo_root}/{service.script}", *args]
```

(Its docstring loses the "not supported when frozen" line; D29's note that the
frozen path is deferred is now resolved — update D29 with a one-line addendum.)

### 2. `koe/cli.py` — the dispatch edge (all service imports LAZY)

```python
def main(argv=None) -> None:
    """Frozen entry brain. Route by parse_run_target and hand off to the chosen
    service's existing main(). Heavy/service imports happen HERE, lazily, only for
    the chosen target — importing koe.app pulls `keyboard`, interpreter/talk pull
    the audio stack, so a bare Control Center launch must import none of them."""
```

Routing (import the target and call it — nothing else runs):

- `control`  → `from .launchergui import main as m; _hide_console(); m()`
- `dictation`→ `from .app import main as m; m(rest)`   (app.main takes argv)
- `interpreter` / `talk` → these read `sys.argv` themselves and don't take argv,
  so set `sys.argv = [sys.argv[0], *rest]` FIRST, then
  `import interpreter; interpreter.main()` (resp. `talk`).

`_hide_console()` — best-effort, Windows-only, guarded:

```python
def _hide_console() -> None:
    """The one-folder exe is built console=True (services need a console for
    logs/captions). For the Control Center that console is just noise behind the
    window, and looks alarming to a non-developer — hide it. Best-effort: any
    failure (non-Windows, no console, API quirk) silently no-ops (invariant 3)."""
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass
```

### 3. `koe/launchergui.py` — make the two spawn sites frozen-aware

- `_on_toggle`: pass `frozen=getattr(sys, "frozen", False)` into `build_command`.
- `_on_setup`: replace the hard-coded source argv with
  `setup_command(python_exe=sys.executable, repo_root=data_dir(),
  frozen=getattr(sys, "frozen", False))`, then `_spawn(...)` it.

Both are one-line edits; the pure functions carry the branch. No other UI change.

### 4. `koe_main.py` (repo root) — the frozen entry script

Two lines: `from koe.cli import main` / `main()`. A module docstring: this is the
PyInstaller entry (`koe.spec`), NOT a source entry — source users still use
`run.py` / `launcher.py`. Named `koe_main.py`, never `koe.py` (package shadow).

### 5. `koe.spec` — entry swap + collect the pillars' deps

```python
from PyInstaller.utils.hooks import collect_all, collect_submodules
```

- Entry: `Analysis(["run.py"])` → `Analysis(["koe_main.py"])`.
- Grow the `collect_all` loop with the Interpreter/Talk deps that today's
  dictation-only build never needed:
  `"pyaudiowpatch"` (WASAPI loopback — interpreter), `"pyttsx3"` (SAPI — talk),
  `"requests"` (Ollama/VOICEVOX/AivisSpeech HTTP + certifi cacert), `"numpy"`
  (VAD math). Keep every existing entry.
- Force the modules PyInstaller's static analysis can't see because every pillar
  imports lazily (this is THE frozen pitfall):
  `hiddenimports += ["interpreter", "talk", "tkinter", "tkinter.ttk"]`
  `hiddenimports += collect_submodules("koe")`  # every koe/*.py, lazy or not
- Everything else (CUDA DLL bundling + dedup, dictionary example, `console=True`,
  COLLECT name "Koe") unchanged.

WHY `collect_submodules("koe")`: interpreter/talk reach koe.translator,
koe.responder, koe.overlay, koe.voice, koe.turntaking, koe.context_grabber… all
via function-local imports; without this they'd be missing from the bundle and
each service would crash on first use with ModuleNotFoundError.

## File-by-file changes

| File | Change |
|------|--------|
| koe/launcher.py | +`parse_run_target`, +`setup_command`; `build_command` frozen branch returns `--run` argv (was `raise`) |
| koe/cli.py | new — lazy dispatch edge + `_hide_console` |
| koe_main.py | new — 2-line PyInstaller entry → koe.cli.main |
| koe/launchergui.py | `_on_toggle`/`_on_setup` pass `frozen=`; `_on_setup` uses `setup_command` |
| koe.spec | entry → koe_main.py; +4 collect_all pkgs; +hiddenimports (interpreter/talk/tkinter + collect_submodules("koe")) |
| tests/test_launcher.py | +tests for parse_run_target, build_command frozen, setup_command |
| docs/DECISIONS.md | D29 addendum: frozen path now implemented via self-re-invoke |
| README.md / README.ja.md | reviewer: Option A (.exe) now = all services via the Control Center |

## Tests to write (extend tests/test_launcher.py — pure, CI-safe)

- `test_parse_run_target_services` — `["--run","interpreter","--to","ja"]` →
  `("interpreter", ["--to","ja"])`; same for dictation/talk.
- `test_parse_run_target_defaults_to_control` — `[]` → `("control", [])`;
  `["--foo"]` → `("control", ["--foo"])`; `["--run","bogus"]` →
  `("control", ["--run","bogus"])` (unknown target ⇒ front door, args preserved).
- `test_build_command_frozen_reinvokes_self` — frozen=True, interpreter, full
  combo → `["/py","--run","interpreter","--to","ja","--overlay","--suggest"]`;
  dictation frozen → `["/py","--run","dictation"]`. (Replaces the old
  `test_build_command_frozen_raises`.)
- `test_build_command_source_unchanged` — frozen=False still →
  `["/py","/repo/interpreter.py",...]` (no regression).
- `test_setup_command_both_modes` — frozen → `["/py","--run","dictation",
  "--setup-only"]`; source → `["/py","/repo/run.py","--setup-only"]`.
- `test_cli_module_imports_headless` — `import koe.cli` with the audio/keyboard/
  tkinter stack blocked via the import shim; proves koe.cli imports no service
  at module scope (only the chosen one is imported, inside main()).

koe.spec and koe_main.py get a `compileall` check only — koe.spec is not
importable (PyInstaller execs it) and the build itself is owner-Windows-only.

## Acceptance criteria

**CI-verifiable (must hold before commit):**
1. Full suite green (`pip install pytest requests numpy`); `python -m compileall
   koe koe_main.py` clean.
2. `koe/launcher.py` still imports nothing beyond stdlib-pure at module scope;
   `koe/cli.py` imports no service (koe.app/interpreter/talk/launchergui) at
   module scope — both proven by headless-import tests.
3. `parse_run_target`, `build_command` (both modes), `setup_command` covered by
   the tests above.
4. Source entry points unchanged: `python launcher.py` and `python run.py`
   behave exactly as before (build_command source branch untouched in behavior).

**Owner-Windows-only (documented, not gating the commit):**
5. `pyinstaller koe.spec --noconfirm` completes; `dist/Koe/Koe.exe` exists.
6. Each `--run` target launches its service; bare `Koe.exe` opens the Control
   Center with no visible console behind it.
7. From the frozen Control Center, every start/stop button works (children are
   `Koe.exe --run …`), and "redo setup" opens the wizard then exits.

## Manual checks for the owner (Windows — the only place these CAN run)

Build: `.\.venv\Scripts\pyinstaller.exe koe.spec --noconfirm`

1. `dist\Koe\Koe.exe` (double-click) → Control Center opens, **no console window**
   behind it, all lamps grey.
2. From it: start **Dictation** → tray + hotkey work. Start **通訳＋字幕** → its
   console + overlay appear over a video; F9 suggests. Start **会話** → console,
   speak, get a reply. Each stop closes its window.
3. Command line sanity: `dist\Koe\Koe.exe --run interpreter --to ja --overlay`
   runs the interpreter directly; `--run talk` runs Talk; `--run dictation
   --setup-only` opens the wizard and exits.
4. If any service dies instantly with `ModuleNotFoundError: X` → add `X` to
   `koe.spec` hiddenimports (or `collect_all("X")` if it ships DLLs/data),
   rebuild. Expected libraries already covered: pyaudiowpatch, pyttsx3, requests,
   numpy, tkinter, all koe.* — report anything beyond these so the spec's list
   can be corrected.
5. "すべて終了して閉じる" / [X] → every child Koe.exe closes; none linger in Task
   Manager.

## Known pitfalls

- **Lazy imports defeat PyInstaller.** Every pillar obeys invariant 2 (heavy
  imports inside functions), so static analysis sees almost none of them — hence
  `collect_submodules("koe")` + explicit third-party `collect_all`. This is the
  single most likely source of "runs from source, crashes as .exe."
- **koe.app imports `keyboard` at module scope.** koe/cli.py must import koe.app
  *inside* the dictation branch only — importing it at module top would drag
  `keyboard` into a bare Control Center launch (and every headless import test).
- **interpreter/talk read `sys.argv`, not an argv param.** The dispatcher must
  rewrite `sys.argv` to `[argv0, *rest]` before calling their `main()`; dictation
  is the odd one out (its `main(argv)` takes the list directly).
- **`sys.executable` differs by mode.** Frozen: Koe.exe (re-invoke via `--run`).
  Source: python.exe (run the .py). `build_command`/`setup_command` branch on the
  `frozen` flag the GUI reads from `sys.frozen`; the pure functions never touch
  `sys` themselves (stay testable).
- **console=True + a GUI default.** The single exe can't be per-invocation
  windowed, so the Control Center hides its own console at runtime; keep it
  best-effort (a hidden-console failure must not stop the window opening).
- **One-file would make re-invoke slow.** Keep the one-folder COLLECT — each
  `Koe.exe --run …` re-launch reuses the extracted folder; a one-file build would
  re-unpack ~1.5 GB per service launch.
