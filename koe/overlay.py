"""Tkinter I/O edge for the Interpreter's on-screen caption overlay (--overlay).

Translucent, click-through, always-on-top caption strip so the Interpreter can
be read over a call window instead of a terminal. Display-only by contract
(D17): it only renders text the pipeline already produced, never triggers GPU
work. All tkinter/ctypes imports are lazy (invariant 2) since this module is
Windows-only and only loaded when --overlay is passed.
"""

from __future__ import annotations

import queue
import sys
import time


def run_overlay(overlay_q, stop, cfg) -> None:
    """Owns the Tk mainloop. MUST run on the process's main thread (tkinter
    is main-thread-only). Drains overlay_q via root.after polling — no other
    thread ever touches a Tk object.
    overlay_q items: (kind, text) tuples. stop: threading.Event — when set by
    the caller, the window closes; when the window dies, run_overlay returns.
    """
    try:
        import ctypes
        import tkinter as tk

        from koe.overlaytext import (CaptionBuffer, KIND_SOURCE,
                                     KIND_SUGGESTION, KIND_TRANSLATION)

        try:
            # Per-monitor v2 DPI awareness so text isn't blurry on scaled
            # displays; best-effort, older Windows builds may not have shcore.
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

        root = tk.Tk()
        root.overrideredirect(True)              # no title bar/border — this is a display strip, not a window
        root.attributes("-topmost", True)         # always visible over the call window
        root.attributes("-alpha", cfg.overlay_opacity)

        # Background acts as the chroma key for click-through transparency;
        # labels use a different (near-black) background so they stay visible
        # (same color for both would make the text chips disappear too).
        TRANSPARENT = "#010203"
        root.config(bg=TRANSPARENT)
        try:
            root.wm_attributes("-transparentcolor", TRANSPARENT)
        except Exception:
            pass

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        width = int(sw * 0.7)                     # 70% of screen width, anchored bottom-center
        # Fixed height sized for the worst case so the bottom-anchored
        # position never has to be recomputed as captions come and go:
        # (max_items-1) items x 2 lines + one 3-line suggestion = 7 lines,
        # plus one line of breathing room = 8. Simpler than a resizing
        # window for a v1 display strip.
        line_h = cfg.overlay_font_size + 12
        height = line_h * 8
        x = (sw - width) // 2
        y = int(sh * 0.9) - height                # bottom edge at 90% of screen height
        root.geometry(f"{width}x{height}+{x}+{y}")

        colors = {
            KIND_SOURCE: "white",
            KIND_TRANSLATION: "#7fdbff",
            KIND_SUGGESTION: "#7cfc00",
        }

        buf = CaptionBuffer()
        labels: list = []
        # Last-rendered CaptionBuffer.revision: pump() skips the redraw when
        # nothing changed, so idle poll ticks don't rebuild labels (flicker).
        rendered_rev = [-1]

        def _render(items) -> None:
            for lbl in labels:
                lbl.destroy()
            labels.clear()
            for item in items:
                color = colors.get(item.kind, "white")
                for line in item.lines:
                    # No extra ">> " prefix here: the suggester's first line
                    # already carries it (_SuggestHelper.lines) — adding one
                    # here doubled it to ">> >>".
                    lbl = tk.Label(root, text=line,
                                  font=("Yu Gothic UI", cfg.overlay_font_size),
                                  fg=color, bg="#101010")   # near-black chip: readable over any app
                    lbl.pack(anchor="center")
                    labels.append(lbl)

        def _make_click_through() -> None:
            # Best-effort: guarded so the overlay still displays (just isn't
            # click-through) if this ever fails (D16 — a failed subsystem
            # degrades, it never crashes the pipeline).
            try:
                GWL_EXSTYLE = -20
                WS_EX_LAYERED = 0x00080000
                WS_EX_TRANSPARENT = 0x00000020
                WS_EX_NOACTIVATE = 0x08000000
                hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
                style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
                style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
                ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
            except Exception:
                pass

        def pump() -> None:
            while True:
                try:
                    kind, text = overlay_q.get_nowait()
                except queue.Empty:
                    break
                buf.add(kind, text, time.time())
            # visible() also applies expiry (bumping revision when an item
            # drops), so comparing revisions catches both new and expired text.
            items = buf.visible(time.time())
            if buf.revision != rendered_rev[0]:
                _render(items)
                rendered_rev[0] = buf.revision
            if stop.is_set():
                root.destroy()
                return
            root.after(100, pump)

        # 200ms so the window is mapped before we touch its hwnd — GetAncestor
        # on an unmapped window can return a transient parent (still guarded).
        root.after(200, _make_click_through)
        root.after(100, pump)
        root.mainloop()
    except Exception as exc:
        print(f"! overlay unavailable ({exc}) — terminal captions only",
              file=sys.stderr, flush=True)
        return
