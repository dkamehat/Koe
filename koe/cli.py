"""Koe.exe's frozen dispatch edge — the one thing every packaged invocation of
Koe.exe runs first.

Source installs have four separate entry points: run.py (dictation),
interpreter.py, talk.py, and launcher.py (the Control Center). The frozen
build collapses all four into one Koe.exe (koe.spec's Analysis script is
koe_main.py, which just calls main() below), so this module is the brain that
decides, from argv, which of the four to actually become: bare invocation
opens the Control Center, `--run <service> [flags]` runs that service directly
(see koe/launcher.py:parse_run_target for the pure argv → target mapping).

Every branch imports its target LAZILY, inside main(), for the same reason the
four source entry points keep their own heavy imports lazy (invariant 2):
koe.app imports `keyboard` at module scope, interpreter.py/talk.py pull in
sounddevice/pyaudiowpatch/numpy, and koe.launchergui's window needs tkinter —
a bare Control Center launch (by far the most common invocation: a user just
double-clicked Koe.exe) must not import any of that, and this module itself
must stay importable headless with none of that stack installed.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    """Frozen entry brain. Route by parse_run_target and hand off to the chosen
    service's existing main(). Heavy/service imports happen HERE, lazily, only
    for the chosen target — importing koe.app pulls `keyboard`, interpreter/talk
    pull the audio stack, so a bare Control Center launch must import none of
    them."""
    from .launcher import parse_run_target

    if argv is None:
        argv = sys.argv[1:]
    target, rest = parse_run_target(argv)

    if target == "control":
        from .launchergui import main as m
        _hide_console()
        m()
    elif target == "dictation":
        from .app import main as m
        m(rest)
    elif target == "interpreter":
        sys.argv = [sys.argv[0], *rest]
        import interpreter
        interpreter.main()
    elif target == "talk":
        sys.argv = [sys.argv[0], *rest]
        import talk
        talk.main()


def _hide_console() -> None:
    """The one-folder exe is built console=True (services need a console for
    logs/captions). For the Control Center that console is just noise behind the
    window, and looks alarming to a non-developer — hide it. Best-effort: any
    failure (non-Windows, no console, API quirk) silently no-ops (invariant 3).

    Only hide a console THIS process owns — one freshly allocated because the exe
    was double-clicked. If more than one PID is attached, Koe.exe was launched
    from an existing shell (e.g. the owner testing `Koe.exe` from PowerShell);
    hiding that console would hide the user's own terminal. GetConsoleProcessList
    returns the count of attached processes — 1 means we're alone and own it."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        pids = (ctypes.c_uint * 2)()
        if kernel32.GetConsoleProcessList(pids, 2) != 1:
            return  # inherited an existing shell's console — never hide it
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass
