"""tkinter window for the Koe control center — start/stop each end-user
service (Dictation, Interpreter+captions, Talk) as its own child process.

Thin UI edge over koe/launcher.py's pure ServiceManager: this module owns
widgets, polling, and the real subprocess spawner; all lifecycle logic and
CI-testable branches live in launcher.py. v1 is a plain window, not a tray
icon (discoverability for the "誰でも使える" audience) — see
docs/specs/control-center.md. A tray front-end later would only need to swap
this file for a new thin edge; the manager underneath would not change.

All tkinter/subprocess imports are lazy inside functions (invariant 2), so
this module — and the control center it powers — imports and starts
instantly even though none of the three services it launches need be
installed yet.
"""

from __future__ import annotations

import sys

# Poll cadence for self-healing lamps/buttons: responsive enough that a
# crashed/closed service is noticed well within any human "did it stop?"
# patience, but cheap — each tick is only as many poll() calls as services (3).
POLL_MS = 700

# Status lamp colors (Canvas dot fill). Green/grey read at a glance regardless
# of the row's JA label, and match common "on/off" status conventions.
LAMP_RUNNING = "#2f9e44"
LAMP_STOPPED = "#adb5bd"

# Fixed window size: a header + 3 status rows + footer never needs to resize,
# so a fixed, non-resizable window is one less layout concern for a v1 tool.
WINDOW_SIZE = "440x300"

WARNING_TEXT = (
    "マイクを使う機能が複数動いています（ディクテーションと会話）。"
    "片方だけの利用をおすすめします。"
)


def _spawn(argv):
    """Real I/O-edge spawner injected into ServiceManager. CREATE_NEW_CONSOLE
    gives each service its own visible console (captions/logs/conversation are
    the point during early release; Interpreter's overlay shows on top
    regardless). getattr(...) keeps this callable off-Windows (0 = no special
    flags), even though the control center never spawns anything on CI."""
    import subprocess

    from .paths import data_dir

    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen(argv, cwd=str(data_dir()), creationflags=flags)


class _ControlWindow:
    """Owns the Tk root and all row widgets. MUST be constructed and driven
    from the main thread only — tkinter is main-thread-only, same constraint
    as koe/overlay.py's run_overlay and koe/firstrun.py's _Wizard."""

    def __init__(self, tk_module, cfg, mgr):
        from .launcher import services

        self._tk = tk_module
        self.cfg = cfg
        self.mgr = mgr
        self._services = services()
        self._rows: dict[str, tuple] = {}  # key -> (canvas, lamp_item, button)

        self.root = tk_module.Tk()
        self.root.title("Koe コントロールセンター")
        self.root.geometry(WINDOW_SIZE)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        tk_module.Label(
            self.root,
            text="使いたい機能を開始／停止できます。音声はこのPCの中だけで処理されます。",
            justify="left", anchor="w", wraplength=400,
        ).pack(fill="x", padx=16, pady=(16, 8))

        rows_frame = tk_module.Frame(self.root)
        rows_frame.pack(fill="x", padx=16)
        for service in self._services:
            self._build_row(rows_frame, service)

        self._warning_label = tk_module.Label(
            self.root, text="", fg="#c92a2a", justify="left", anchor="w",
            wraplength=400,
        )
        self._warning_label.pack(fill="x", padx=16, pady=(8, 0))

        footer = tk_module.Frame(self.root)
        footer.pack(fill="x", padx=16, pady=16, side="bottom")
        tk_module.Button(
            footer, text="初回セットアップをやり直す", command=self._on_setup,
        ).pack(side="left")
        tk_module.Button(
            footer, text="すべて終了して閉じる", command=self._on_close,
        ).pack(side="right")

        self.root.after(POLL_MS, self._poll)

    def _build_row(self, parent, service) -> None:
        tk = self._tk
        row = tk.Frame(parent)
        row.pack(fill="x", pady=6)

        lamp = tk.Canvas(row, width=16, height=16, highlightthickness=0)
        lamp_item = lamp.create_oval(2, 2, 14, 14, fill=LAMP_STOPPED, outline="")
        lamp.pack(side="left", padx=(0, 8))

        tk.Label(row, text=service.label, anchor="w").pack(
            side="left", fill="x", expand=True
        )

        button = tk.Button(
            row, text="開始", width=8,
            command=lambda s=service: self._on_toggle(s),
        )
        button.pack(side="right")

        self._rows[service.key] = (lamp, lamp_item, button)

    def _on_toggle(self, service) -> None:
        from .launcher import build_command
        from .paths import data_dir

        argv = build_command(
            service, self.cfg,
            python_exe=sys.executable, repo_root=data_dir(),
            frozen=getattr(sys, "frozen", False),
        )
        self.mgr.toggle(service.key, argv)
        self._refresh_row(service.key)
        self._refresh_warning()

    def _refresh_warning(self) -> None:
        # Warn, never block (invariant 3). Recomputed here (not only on click) so
        # that if a mic service exits/crashes on its own the warning clears too —
        # it must self-heal like the lamps, not linger on stale state.
        from .launcher import audio_conflicts

        conflicts = audio_conflicts(self.mgr.running_keys(), self._services)
        self._warning_label.config(text=WARNING_TEXT if conflicts else "")

    def _refresh_row(self, key: str) -> None:
        lamp, lamp_item, button = self._rows[key]
        running = self.mgr.is_running(key)
        lamp.itemconfig(lamp_item, fill=LAMP_RUNNING if running else LAMP_STOPPED)
        button.config(text="停止" if running else "開始")

    def _poll(self) -> None:
        # Self-heal: a service that exited/crashed on its own (not via our
        # button) flips its lamp to grey — and clears any stale mic warning —
        # without any click.
        for service in self._services:
            self._refresh_row(service.key)
        self._refresh_warning()
        self.root.after(POLL_MS, self._poll)

    def _on_setup(self) -> None:
        # One-shot child that genuinely exits on its own: --setup-only runs the
        # wizard and returns WITHOUT starting the dictation tray (plain --setup
        # would fall through to the tray, leaving an untracked instance the
        # window can't stop). See koe/app.py:main. setup_command mirrors
        # build_command's frozen branch so the packaged build re-invokes itself
        # (--run dictation --setup-only) instead of spawning a nonexistent run.py.
        from .launcher import setup_command
        from .paths import data_dir

        argv = setup_command(
            python_exe=sys.executable, repo_root=data_dir(),
            frozen=getattr(sys, "frozen", False),
        )
        _spawn(argv)

    def _on_close(self) -> None:
        # No orphan children ever outlive the window — same action whether
        # triggered by the footer button or the [X].
        self.mgr.stop_all()
        self.root.destroy()


def main() -> None:
    """Entry for launcher.py. Build config, ServiceManager with the real
    spawner, show the window, run the tk mainloop. On tkinter import failure
    print one stderr line and exit 1 (there is no headless fallback for a GUI
    launcher)."""
    try:
        import tkinter as tk
    except Exception as exc:
        print(f"! control center unavailable ({exc}) — tkinter is required",
              file=sys.stderr)
        sys.exit(1)

    from .config import Config
    from .launcher import ServiceManager

    cfg = Config.load()
    mgr = ServiceManager(_spawn)
    window = _ControlWindow(tk, cfg, mgr)
    window.root.mainloop()


if __name__ == "__main__":
    main()
