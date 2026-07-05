"""Koe control center — pure core: service definitions, command building,
process lifecycle.

Koe ships three end-user services (Dictation, Interpreter+captions, Talk),
each a standalone script with its own CLI flags. The control center
(root ``launcher.py`` + ``koe/launchergui.py``) is one small window that
starts/stops each as its own **child process** — not an in-process thread,
because Interpreter and Talk are mature console programs with their own main
loops, mic/loopback capture, `keyboard` global hooks, and (Interpreter) a
tkinter overlay that owns its main thread; folding them into one process would
mean a risky rewrite and audio/main-thread contention. Separate processes also
satisfy graceful degradation (invariant 3): one service crashing leaves the
others and the control center alive, and its lamp flips to grey on its own.

This module is the pure core: service data + three pure functions +
``ServiceManager``, whose only I/O (spawning) is an **injected callable** — so
every branch here is testable on CI with no subprocess ever spawned and no
tkinter ever imported. Keep it that way: no tkinter/subprocess import at
module scope (see ``koe/launchergui.py`` for the real I/O edge).

v1 ships as a window, not a tray icon (discoverability for the "誰でも使える"
audience). Because the UI is a thin edge over this pure manager, a tray
front-end later is a small addition, not a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    key: str        # "dictation" | "interpreter" | "talk"  (stable id)
    label: str      # JA display name, e.g. "ディクテーション（音声入力）"
    script: str     # "run.py" | "interpreter.py" | "talk.py" (repo-root relative)
    uses_mic: bool  # True if it captures the microphone (Dictation, Talk).
                    # Interpreter captures system-audio loopback, not the mic → False.


def services() -> list[Service]:
    """The three end-user services in display order. Bench is dev-only, excluded."""
    return [
        Service("dictation", "ディクテーション（音声入力）", "run.py", uses_mic=True),
        Service("interpreter", "通訳＋字幕（Interpreter）", "interpreter.py", uses_mic=False),
        Service("talk", "会話（Koe Talk）", "talk.py", uses_mic=True),
    ]


def interpreter_args(cfg) -> list[str]:
    """Flags after interpreter.py, from config:
    ["--to", cfg.interpreter_to] only when interpreter_to is truthy (""=captions
    only, no --to); "--overlay" when cfg.interpreter_overlay; "--suggest" when
    cfg.interpreter_suggest. Order fixed: --to first, then --overlay, --suggest.
    WHY config-driven: the owner's proven-good combo is --to ja --overlay
    --suggest, but captions-only / no-suggest must be reachable without code."""
    args: list[str] = []
    if cfg.interpreter_to:
        args += ["--to", cfg.interpreter_to]
    if cfg.interpreter_overlay:
        args.append("--overlay")
    if cfg.interpreter_suggest:
        args.append("--suggest")
    return args


def service_args(service: Service, cfg) -> list[str]:
    """Per-service flags. dictation → []; talk → [] (both read config directly);
    interpreter → interpreter_args(cfg)."""
    if service.key == "interpreter":
        return interpreter_args(cfg)
    return []


def build_command(service: Service, cfg, *, python_exe, repo_root, frozen: bool = False) -> list[str]:
    """Full argv to spawn: [python_exe, <repo_root>/<script>, *service_args].
    frozen=True raises NotImplementedError with a one-line message (see non-goal).
    Pure: no os/subprocess — takes python_exe and repo_root as plain args so tests
    pass fakes ('/py', '/repo')."""
    if frozen:
        raise NotImplementedError(
            "control center: launching services is not supported in the "
            "packaged build yet"
        )
    script_path = f"{repo_root}/{service.script}"
    return [python_exe, script_path, *service_args(service, cfg)]


def audio_conflicts(running_keys, all_services) -> list[str]:
    """Keys among running_keys whose services set uses_mic — returned only when
    ≥2 of them would fight over the one microphone (Dictation + Talk). One or zero
    mic users → []. Interpreter never conflicts. Lets the UI warn, not block."""
    # Order by all_services' display order (not running_keys' iteration order,
    # which may be an unordered set) so the result is deterministic.
    mic_running = [s.key for s in all_services if s.key in running_keys and s.uses_mic]
    if len(mic_running) >= 2:
        return mic_running
    return []


class ServiceManager:
    """Process lifecycle for the three services. Spawning is injected so every
    branch here is testable without ever touching subprocess/tkinter."""

    def __init__(self, spawn):
        """spawn: (argv: list[str]) -> handle. handle must expose .poll()
        (None while alive) and .terminate(). Real impl injects a subprocess.Popen
        wrapper (I/O edge, in koe/launchergui.py); tests inject a fake."""
        self._spawn = spawn
        self._handles: dict[str, object] = {}

    def is_running(self, key) -> bool:
        # a handle exists AND handle.poll() is None. A crashed/exited child
        # (poll() is not None) reads as stopped — the lamp self-heals.
        handle = self._handles.get(key)
        if handle is None:
            return False
        return self._safe_poll(handle) is None

    def start(self, key, argv) -> bool:
        # no-op returning False if already running; else spawn, store handle, True.
        # (argv passed in — built by the caller via build_command — so the manager
        #  stays free of config/path knowledge and is trivially testable.)
        if self.is_running(key):
            return False
        self._handles[key] = self._spawn(argv)
        return True

    def stop(self, key) -> bool:
        # terminate the handle if running, drop it, True; else False.
        if not self.is_running(key):
            return False
        self._safe_terminate(self._handles[key])
        del self._handles[key]
        return True

    def toggle(self, key, argv) -> bool:
        # start if stopped else stop
        if self.is_running(key):
            return self.stop(key)
        return self.start(key, argv)

    def running_keys(self) -> list[str]:
        # keys whose handle.poll() is None
        return [key for key in self._handles if self.is_running(key)]

    def stop_all(self) -> None:
        # terminate every live child (for shutdown)
        for key in list(self._handles):
            if self.is_running(key):
                self._safe_terminate(self._handles[key])
            del self._handles[key]

    # Every terminate()/poll() call is wrapped so a dead handle can't raise
    # into the UI (Popen on an already-exited process is fine, but a foreign
    # handle might not be — be defensive).
    @staticmethod
    def _safe_poll(handle):
        try:
            return handle.poll()
        except Exception:
            return 0  # unreadable handle: treat as exited, not alive

    @staticmethod
    def _safe_terminate(handle) -> None:
        try:
            handle.terminate()
        except Exception:
            pass
