"""Local visual grounding — read what the user is looking at, on-device, in ms.

At dictation time the target app still has focus (our app is a background tray),
so we can cheaply read the foreground window title and, best-effort, the text of
the focused control. Feeding that to the ③ refiner as `Context:` lets the LLM
re-rank ambiguous words toward what's actually on screen (file names, code
identifiers, proper nouns) — the biggest accuracy lever after the dictionary.

Everything stays local. Only the focused field is read (not the whole screen),
and the grabber degrades to "title only", then to nothing, rather than ever
breaking dictation.
"""

from __future__ import annotations

import ctypes
import re

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


def _foreground_title() -> str:
    if _user32 is None:
        return ""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value.strip()
    except Exception:
        return ""


def _focused_field_text(max_chars: int) -> str:
    """Best-effort text of the focused control via UI Automation. Guarded and
    capped; returns "" on any problem (some apps don't expose it)."""
    try:
        import uiautomation as auto
    except Exception:
        return ""
    try:
        ctrl = auto.GetFocusedControl()
        if ctrl is None:
            return ""
        text = ""
        # Editable controls expose their content through the Value pattern.
        try:
            vp = ctrl.GetValuePattern()
            text = (vp.Value or "").strip()
        except Exception:
            text = ""
        if not text:
            try:
                text = (ctrl.Name or "").strip()
            except Exception:
                text = ""
        if len(text) > max_chars:
            # Keep the tail — that's nearest the cursor in most editors.
            text = text[-max_chars:]
        return text
    except Exception:
        return ""


# Pull out tokens worth biasing toward: identifiers, CamelCase, file names, terms.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\w]+\.[A-Za-z0-9]{1,5}")


def extract_terms(context: str, limit: int = 20) -> list[str]:
    seen: list[str] = []
    for m in _TOKEN_RE.findall(context or ""):
        if m not in seen:
            seen.append(m)
        if len(seen) >= limit:
            break
    return seen


def get_context(max_chars: int = 400, include_field: bool = True) -> str | None:
    """A compact, on-screen context string for the refiner, or None."""
    parts = []
    title = _foreground_title()
    if title:
        parts.append(f"Window: {title}")
    if include_field:
        field = _focused_field_text(max_chars)
        if field:
            parts.append(f"Focused text: {field}")
    return "\n".join(parts) if parts else None
