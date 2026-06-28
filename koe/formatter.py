"""Lightweight, fully-local text formatting.

No LLM required: handles spoken editing commands (English + Japanese) and tidies
spacing/capitalization. Whisper already produces punctuation, so this stays light
and deterministic — predictable is better than clever for dictation.
"""

from __future__ import annotations

import re

# Spoken commands -> literal output. Order matters: longer phrases first.
_COMMANDS: list[tuple[str, str]] = [
    (r"\bnew paragraph\b", "\n\n"),
    (r"\bnew line\b", "\n"),
    (r"\bnext line\b", "\n"),
    (r"改行して", "\n"),
    (r"改行", "\n"),
    (r"\bfull stop\b", ". "),
    (r"\bperiod\b", ". "),
    (r"\bcomma\b", ", "),
    (r"\bquestion mark\b", "? "),
    (r"\bexclamation mark\b", "! "),
    (r"。", "。"),
]


# A short unit (1-12 chars) repeated 6+ times back-to-back. Whisper falls into
# this kind of loop on noise/silence ("シャッシャッシャッ…", "the the the…"); real
# speech almost never repeats a short unit that many times in a row.
_RUNAWAY_REPEAT = re.compile(r"(.{1,12}?)\1{5,}", re.DOTALL)


def collapse_runaway_repeats(text: str) -> str:
    """Collapse a degenerate repetition loop to a single occurrence of the unit.

    A deterministic safety net for Whisper's repetition-hallucination failure
    mode, so a stuck decode never reaches the user (or makes the refiner chew on
    a thousand repeated tokens).
    """
    if not text:
        return text
    return _RUNAWAY_REPEAT.sub(r"\1", text)


def _apply_commands(text: str) -> str:
    for pattern, repl in _COMMANDS:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return text


def _tidy_spacing(text: str) -> str:
    # Collapse spaces created around inserted punctuation, but preserve newlines.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r" +([.,!?;:])", r"\1", text)
    text = re.sub(r"([.,!?;:])(?=[^\s.,!?;:])", r"\1 ", text)
    # ...but never split a decimal or grouped number: undo a space wedged between
    # a digit+separator and a digit ("3. 5" -> "3.5", "1, 000" -> "1,000").
    text = re.sub(r"(?<=\d[.,]) (?=\d)", "", text)
    return text.strip()


def _capitalize_sentences(text: str) -> str:
    # Capitalize the first alphabetic char after sentence boundaries (ASCII only,
    # so Japanese is untouched).
    def cap(m: re.Match) -> str:
        return m.group(1) + m.group(2).upper()

    text = re.sub(r"(^|[.!?]\s+)([a-z])", cap, text)
    return text


def format_text(text: str, *, enable: bool = True, auto_capitalize: bool = True) -> str:
    if not text:
        return ""
    if not enable:
        return text
    text = _apply_commands(text)
    text = _tidy_spacing(text)
    if auto_capitalize:
        text = _capitalize_sentences(text)
    return text
