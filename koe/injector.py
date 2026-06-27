"""Inject transcribed text into whatever app currently has focus.

Default path is clipboard + Ctrl+V, which is the only reliable way to emit
arbitrary Unicode (e.g. Japanese) instantly across Windows apps. A keystroke
fallback exists for apps that block programmatic paste.
"""

from __future__ import annotations

import time

import keyboard
import pyperclip


def _paste(text: str) -> None:
    previous = None
    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None

    pyperclip.copy(text)
    time.sleep(0.02)
    keyboard.send("ctrl+v")
    time.sleep(0.05)

    # Restore the user's prior clipboard contents so we don't clobber it.
    if previous is not None:
        time.sleep(0.05)
        try:
            pyperclip.copy(previous)
        except Exception:
            pass


def inject(text: str, mode: str = "paste") -> None:
    if not text:
        return
    if mode == "clipboard":
        pyperclip.copy(text)
        return
    if mode == "type":
        keyboard.write(text, delay=0.005)
        return
    _paste(text)  # default


# --- streaming helpers: save the clipboard ONCE, paste chunks without the
# per-chunk save/restore dance, restore ONCE at the end (less flicker). ---

def get_clipboard() -> str | None:
    try:
        return pyperclip.paste()
    except Exception:
        return None


def restore_clipboard(previous: str | None) -> None:
    if previous is not None:
        try:
            pyperclip.copy(previous)
        except Exception:
            pass


def inject_chunk(text: str, mode: str = "paste") -> None:
    """Emit one streamed chunk. For paste mode this does NOT restore the
    clipboard — the caller restores once after the stream completes."""
    if not text:
        return
    if mode == "type":
        keyboard.write(text, delay=0.005)
        return
    pyperclip.copy(text)
    time.sleep(0.02)
    keyboard.send("ctrl+v")
    time.sleep(0.03)
