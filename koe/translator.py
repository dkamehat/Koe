"""Local translation for Koe Interpreter (S2 of the interpreter roadmap).

Reuses the same on-device Ollama server as the ③ refiner — no cloud. This is the
deliberate opposite of the refiner: the refiner's hard rule is *never translate*,
so translation lives in its own module with its own prompt and NO language guard.
On any error it returns the source text, so captions never break.
"""

from __future__ import annotations

from .refiner import _has_cjk, _ollama_session

# Friendly names for the prompt; unknown codes pass through verbatim, so new
# targets work without code changes (toward the multilingual North Star).
LANGS = {
    "ja": "Japanese", "en": "English", "zh": "Chinese", "ko": "Korean",
    "es": "Spanish", "fr": "French", "de": "German",
}


def language_name(code: str) -> str:
    return LANGS.get(code.lower(), code)


def already_in_target(target: str, text: str) -> bool:
    """Skip translation when the text is already in the target language — a cheap
    script check that avoids a needless LLM call and 'translating' same-language."""
    t = target.lower()
    if t == "ja":
        return _has_cjk(text)
    if t in ("en", "es", "fr", "de"):
        return (not _has_cjk(text)) and any(c.isascii() and c.isalpha() for c in text)
    return False


def _system_prompt(lang: str) -> str:
    return (
        f"You are a professional real-time interpreter. Translate the user's text into "
        f"natural, fluent {lang}. Output ONLY the translation — no preamble, no quotes, "
        f"no notes, no romanization, no source text. Preserve meaning, tone and proper "
        f"nouns. Translate even a short phrase."
    )


class OllamaTranslator:
    """Translate caption text via the local Ollama server (free, on-device)."""

    def __init__(self, model: str, url: str, target: str):
        self.model = model
        self.url = url.rstrip("/")
        self.target = target.lower()
        self.lang = language_name(self.target)

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text or already_in_target(self.target, text):
            return text
        try:
            resp = _ollama_session.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "options": {
                        "temperature": 0.2,
                        "num_predict": max(64, min(640, len(text) * 2 + 64)),
                    },
                    "keep_alive": "10m",
                    "messages": [
                        {"role": "system", "content": _system_prompt(self.lang)},
                        {"role": "user", "content": text},
                    ],
                },
                timeout=60,
            )
            resp.raise_for_status()
            out = (resp.json()["message"]["content"] or "").strip()
            return out or text
        except Exception:
            return text  # never break captions — show the source
