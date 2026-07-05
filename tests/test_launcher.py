"""Unit tests for the control center's pure core (koe/launcher.py).

Pure tests only — koe/launcher.py keeps tkinter/subprocess imports out
entirely (they live in koe/launchergui.py instead), so these run on any OS
with no display/mic/subprocess spawn (invariant 2). Run with:
python -m pytest
"""

import builtins
import importlib
from types import SimpleNamespace

from koe.launcher import (ServiceManager, audio_conflicts, build_command,
                           interpreter_args, parse_run_target, services,
                           setup_command)


def _cfg(to="ja", overlay=True, suggest=True):
    return SimpleNamespace(
        interpreter_to=to, interpreter_overlay=overlay, interpreter_suggest=suggest,
    )


# --- services -----------------------------------------------------------------

def test_services_are_the_three_end_user_ones():
    svcs = services()
    keys = [s.key for s in svcs]
    assert keys == ["dictation", "interpreter", "talk"]   # display order
    assert "bench" not in keys
    for s in svcs:
        assert s.label
        assert s.script


# --- parse_run_target -----------------------------------------------------------

def test_parse_run_target_services():
    assert parse_run_target(["--run", "interpreter", "--to", "ja"]) == (
        "interpreter", ["--to", "ja"],
    )
    assert parse_run_target(["--run", "dictation"]) == ("dictation", [])
    assert parse_run_target(["--run", "talk", "--debug"]) == ("talk", ["--debug"])


def test_parse_run_target_defaults_to_control():
    assert parse_run_target([]) == ("control", [])
    assert parse_run_target(["--foo"]) == ("control", ["--foo"])
    # unknown target: front door, not a crash (invariant 3) — args preserved
    assert parse_run_target(["--run", "bogus"]) == ("control", ["--run", "bogus"])


# --- interpreter_args -----------------------------------------------------------

def test_interpreter_args_full_combo():
    cfg = _cfg(to="ja", overlay=True, suggest=True)
    assert interpreter_args(cfg) == ["--to", "ja", "--overlay", "--suggest"]


def test_interpreter_args_captions_only():
    # Drive each flag independently.
    assert interpreter_args(_cfg(to="", overlay=True, suggest=True)) == [
        "--overlay", "--suggest",
    ]
    assert interpreter_args(_cfg(to="ja", overlay=False, suggest=True)) == [
        "--to", "ja", "--suggest",
    ]
    assert interpreter_args(_cfg(to="ja", overlay=True, suggest=False)) == [
        "--to", "ja", "--overlay",
    ]
    assert interpreter_args(_cfg(to="", overlay=False, suggest=False)) == []


# --- build_command --------------------------------------------------------------

def test_build_command_shapes():
    svcs = {s.key: s for s in services()}
    cfg = _cfg(to="ja", overlay=True, suggest=True)

    assert build_command(
        svcs["dictation"], cfg, python_exe="/py", repo_root="/repo",
    ) == ["/py", "/repo/run.py"]

    assert build_command(
        svcs["interpreter"], cfg, python_exe="/py", repo_root="/repo",
    ) == ["/py", "/repo/interpreter.py", "--to", "ja", "--overlay", "--suggest"]

    assert build_command(
        svcs["talk"], cfg, python_exe="/py", repo_root="/repo",
    ) == ["/py", "/repo/talk.py"]


def test_build_command_source_unchanged():
    svcs = {s.key: s for s in services()}
    cfg = _cfg(to="ja", overlay=True, suggest=True)

    assert build_command(
        svcs["interpreter"], cfg, python_exe="/py", repo_root="/repo",
    ) == ["/py", "/repo/interpreter.py", "--to", "ja", "--overlay", "--suggest"]


def test_build_command_frozen_reinvokes_self():
    svcs = {s.key: s for s in services()}
    cfg = _cfg(to="ja", overlay=True, suggest=True)

    assert build_command(
        svcs["interpreter"], cfg, python_exe="/py", repo_root="/repo", frozen=True,
    ) == ["/py", "--run", "interpreter", "--to", "ja", "--overlay", "--suggest"]

    assert build_command(
        svcs["dictation"], cfg, python_exe="/py", repo_root="/repo", frozen=True,
    ) == ["/py", "--run", "dictation"]


# --- setup_command ----------------------------------------------------------------

def test_setup_command_both_modes():
    assert setup_command(
        python_exe="/py", repo_root="/repo", frozen=True,
    ) == ["/py", "--run", "dictation", "--setup-only"]

    assert setup_command(
        python_exe="/py", repo_root="/repo", frozen=False,
    ) == ["/py", "/repo/run.py", "--setup-only"]


# --- ServiceManager --------------------------------------------------------------

class FakeProc:
    """poll() -> None while alive; an int once exited. terminate() marks it
    exited, mirroring subprocess.Popen's real contract (known pitfall: exit
    code 0 is falsy but still means 'exited')."""

    def __init__(self):
        self.terminated = False
        self.crashed = False

    def poll(self):
        return 0 if (self.terminated or self.crashed) else None

    def terminate(self):
        self.terminated = True


class FakeSpawn:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.procs: list[FakeProc] = []

    def __call__(self, argv):
        self.calls.append(argv)
        proc = FakeProc()
        self.procs.append(proc)
        return proc


def test_manager_start_stop_toggle():
    spawn = FakeSpawn()
    mgr = ServiceManager(spawn)

    assert mgr.start("dictation", ["argv"]) is True
    assert mgr.is_running("dictation") is True
    assert len(spawn.calls) == 1

    assert mgr.start("dictation", ["argv"]) is False   # already running: no-op
    assert len(spawn.calls) == 1                        # no 2nd spawn

    assert mgr.stop("dictation") is True
    assert mgr.is_running("dictation") is False
    assert spawn.procs[0].terminated is True

    assert mgr.toggle("dictation", ["argv"]) is True    # stopped -> start
    assert mgr.is_running("dictation") is True
    assert mgr.toggle("dictation", ["argv"]) is True    # running -> stop
    assert mgr.is_running("dictation") is False


def test_manager_detects_self_exit():
    spawn = FakeSpawn()
    mgr = ServiceManager(spawn)
    mgr.start("dictation", ["argv"])
    spawn.procs[0].crashed = True   # exited on its own, no stop() call
    assert mgr.is_running("dictation") is False
    assert mgr.running_keys() == []


def test_manager_stop_all():
    spawn = FakeSpawn()
    mgr = ServiceManager(spawn)
    for key in ("dictation", "interpreter", "talk"):
        mgr.start(key, ["argv"])
    assert set(mgr.running_keys()) == {"dictation", "interpreter", "talk"}

    mgr.stop_all()

    assert mgr.running_keys() == []
    assert all(p.terminated for p in spawn.procs)


# --- audio_conflicts --------------------------------------------------------------

def test_audio_conflicts():
    svcs = services()
    assert set(audio_conflicts({"dictation", "talk"}, svcs)) == {"dictation", "talk"}
    assert audio_conflicts({"interpreter"}, svcs) == []
    assert audio_conflicts({"dictation"}, svcs) == []
    assert audio_conflicts({"dictation", "interpreter"}, svcs) == []


# --- module import safety (invariant 2) -----------------------------------------

def test_launcher_module_imports_headless(monkeypatch):
    """koe/launcher.py must not import tkinter/subprocess at module scope —
    block both via an import shim (the overlay test's technique), so this
    stays meaningful even on a machine that has them installed."""
    import koe.launcher as launcher_module

    blocked = {"tkinter", "subprocess"}
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] in blocked:
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    importlib.reload(launcher_module)  # re-run the module body under the shim


def test_cli_module_imports_headless(monkeypatch):
    """koe/cli.py must import no service (koe.app/interpreter/talk/
    koe.launchergui) at module scope — only the branch main() actually chose
    may. Block the heavy stack each service needs at ITS OWN module scope
    (keyboard for koe.app, numpy for interpreter.py/talk.py) plus the rest of
    the Windows-only stack, so a hoisted import fails the reload (same shim
    technique as test_launcher_module_imports_headless)."""
    import koe.cli as cli_module

    blocked = {
        "keyboard", "tkinter", "subprocess", "sounddevice", "pyaudiowpatch",
        "faster_whisper", "uiautomation", "comtypes", "pystray", "pyttsx3",
        "numpy",
    }
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] in blocked:
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    importlib.reload(cli_module)  # re-run the module body under the shim
