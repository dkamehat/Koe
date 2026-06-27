"""Terminology dictionary — the cheapest, most effective STT-quality lever.

Two on-device mechanisms, no LLM and no network involved:

1. **Bias the recognizer** — proper nouns / jargon are passed to Whisper as an
   `initial_prompt`, which nudges decoding toward spelling them correctly.
2. **Deterministic correction pass** — explicit "wrong => right" rules fix
   recurring mis-transcriptions after decoding.

Mechanism (2) is also where the *improvement cycle* lands: when the user corrects
a result, we append a `wrong => right` rule here so the same error self-heals next
time. Everything stays in a local text file the user owns.

File format (`dictionary.txt`, UTF-8):

    # comments start with '#'
    Kubernetes                # a term to recognize verbatim
    PostgreSQL
    クバネティス => Kubernetes  # auto-correct: left (heard) -> right (canonical)
    ポスグレ => PostgreSQL
"""

from __future__ import annotations

from pathlib import Path

from .paths import data_dir

PROJECT_ROOT = data_dir()
DEFAULT_DICT_PATH = PROJECT_ROOT / "dictionary.txt"

_TEMPLATE = """\
# Koe — terminology dictionary (fully local, never leaves this machine)
#
# 1 line = 1 entry. Write proper nouns / jargon to make them transcribe correctly:
#     Kubernetes
#     PostgreSQL
#
# Use 'heard => canonical' to auto-correct a recurring mis-transcription:
#     クバネティス => Kubernetes
#     ポスグレ => PostgreSQL
#
# Corrections you make while dictating are appended here automatically.
# ---------------------------------------------------------------------------
"""


class Dictionary:
    def __init__(self, path: Path = DEFAULT_DICT_PATH):
        self.path = path
        self.terms: list[str] = []
        self.corrections: list[tuple[str, str]] = []
        self.load()

    # --- parsing ----------------------------------------------------------
    def load(self) -> None:
        self.terms = []
        self.corrections = []
        if not self.path.exists():
            self.path.write_text(_TEMPLATE, encoding="utf-8")
            return
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if "=>" in line:
                wrong, right = (p.strip() for p in line.split("=>", 1))
                if wrong and right:
                    self.corrections.append((wrong, right))
                    if right not in self.terms:
                        self.terms.append(right)
            else:
                if line not in self.terms:
                    self.terms.append(line)
        # Apply longer rules first so they win over substrings.
        self.corrections.sort(key=lambda wr: len(wr[0]), reverse=True)

    # --- mechanism 1: bias the recognizer ---------------------------------
    def initial_prompt(self) -> str | None:
        """A short vocabulary hint for Whisper's `initial_prompt`.

        Kept compact (Whisper only attends to the last ~224 tokens of the prompt);
        most-recently-listed terms are weighted, so newest entries go last.
        """
        if not self.terms:
            return None
        # A natural-language frame transfers better than a bare comma list.
        return "用語: " + "、".join(self.terms[-60:]) + "。"

    # --- mechanism 2: deterministic correction ----------------------------
    def apply(self, text: str) -> str:
        for wrong, right in self.corrections:
            if wrong and wrong in text:
                text = text.replace(wrong, right)
        return text

    # --- improvement cycle: learn from a user correction ------------------
    def learn(self, heard: str, canonical: str) -> None:
        heard, canonical = heard.strip(), canonical.strip()
        if not heard or not canonical or heard == canonical:
            return
        if (heard, canonical) in self.corrections:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"{heard} => {canonical}\n")
        self.corrections.append((heard, canonical))
        self.corrections.sort(key=lambda wr: len(wr[0]), reverse=True)
        if canonical not in self.terms:
            self.terms.append(canonical)
