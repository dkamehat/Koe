"""Pure logic for the Interpreter's on-screen caption overlay (--overlay).

No tkinter, no ctypes, no clock of its own — everything here is unit-testable
on CI (invariant 2: pure core, I/O edges). `koe/overlay.py` is the tkinter I/O
edge that wraps this; all timestamps are passed in explicitly, same rule as
koe/latency.py.
"""

from __future__ import annotations

from dataclasses import dataclass

KIND_SOURCE, KIND_TRANSLATION, KIND_SUGGESTION = "source", "translation", "suggestion"


def display_secs(text: str) -> float:
    # Reading-time heuristic: 4s floor so short captions don't blink away,
    # +0.15s/char for long ones, 12s cap so stale text can't squat the screen.
    return max(4.0, min(12.0, 4.0 + 0.15 * len(text)))


def wrap_caption(text: str, max_chars: int = 46, max_lines: int = 2,
                 keep: str = "tail") -> list[str]:
    """Line-wrap for display. English (has ASCII spaces) wraps on word
    boundaries so no word is ever split; Japanese (no ASCII spaces) wraps by
    raw character count. When the wrapped text exceeds max_lines, `keep`
    decides which end survives, and the cut is marked with "…":
      "tail" keeps the LAST max_lines lines ("…" prefixed to the first kept
             line) — for live captions the newest words carry the meaning;
      "head" keeps the FIRST max_lines lines ("…" appended to the last kept
             line) — for a suggested reply the reply itself comes first and
             is what the user will say aloud.
    """
    if " " in text:
        words = text.split()
        lines: list[str] = []
        cur = ""
        for w in words:
            candidate = f"{cur} {w}".strip()
            if len(candidate) <= max_chars:
                cur = candidate
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if not lines:
            lines = [""]
    else:
        lines = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]

    if len(lines) > max_lines:
        if keep == "head":
            lines = lines[:max_lines]
            lines[-1] = lines[-1] + "…"
        else:
            lines = lines[-max_lines:]
            lines[0] = "…" + lines[0]
    return lines


@dataclass
class CaptionItem:
    kind: str
    lines: list[str]          # already wrapped
    expires_at: float


class CaptionBuffer:
    """Rolling set of on-screen caption items (source / translation / suggestion).
    max_items=3 keeps the strip from filling the whole screen — one line of
    source, one of translation, and the live suggestion is the common steady
    state (D17: display-only, so this never blocks on or triggers I/O)."""

    def __init__(self, max_items: int = 3):
        self._max_items = max_items
        self._items: list[CaptionItem] = []
        # Monotonic change counter: bumps on add() and whenever visible()
        # actually drops an expired item. The renderer compares it to the
        # last revision it drew and skips redraws when nothing changed —
        # rebuilding labels every poll tick would flicker for no reason.
        self.revision: int = 0

    def add(self, kind: str, text: str, now: float) -> None:
        if kind == KIND_SUGGESTION:
            # Only the latest reply proposal is ever actionable — an old one
            # left on screen would be stale advice for a live conversation.
            self._items = [it for it in self._items if it.kind != KIND_SUGGESTION]
            # Suggestions get more room (3 lines) and head-keep: the reply
            # comes first in the text and is what the user will say aloud.
            lines = wrap_caption(text, max_lines=3, keep="head")
        else:
            lines = wrap_caption(text)
        item = CaptionItem(kind=kind, lines=lines,
                            expires_at=now + display_secs(text))
        self._items.append(item)
        if len(self._items) > self._max_items:
            self._items = self._items[-self._max_items:]
        self.revision += 1

    def visible(self, now: float) -> list[CaptionItem]:
        kept = [it for it in self._items if it.expires_at > now]
        if len(kept) != len(self._items):
            self.revision += 1
        self._items = kept
        return list(self._items)
