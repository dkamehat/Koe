"""Local translation for Koe Interpreter (S2 of the interpreter roadmap).

Reuses the same on-device Ollama server as the ③ refiner — no cloud. This is the
deliberate opposite of the refiner: the refiner's hard rule is *never translate*,
so translation lives in its own module with its own prompt and NO language guard.
On any error it returns the source text, so captions never break.
"""

from __future__ import annotations

import requests

from .refiner import _has_cjk

# Friendly names for the prompt; unknown codes pass through verbatim, so new
# targets work without code changes (toward the multilingual North Star).
LANGS = {
    "ja": "Japanese", "en": "English", "zh": "Chinese", "ko": "Korean",
    "es": "Spanish", "fr": "French", "de": "German",
}


def language_name(code: str) -> str:
    return LANGS.get(code.lower(), code)


# High-frequency *simplified-Chinese* characters whose Japanese counterparts are a
# different glyph, so they never appear in correct Japanese — a precise signal that
# the model leaked Chinese into a non-Chinese target (e.g. 贡 vs JP 貢, 样 vs 様,
# 决 vs 決). Deliberately excludes shinjitai shared with Japanese (国学会体区医点数)
# to avoid false positives. Best-effort net; the real fix for leaks is a better model.
_SIMPLIFIED = set(
    "这你个们时说别贡专适对么进过还现实关标题问让边应单习书东语觉师证样决间长门风"
    "饭错银铁钟规护办击华协历县价见观务动变图难类认识试营销团队员报际网"
)


def leaked_nontarget_chinese(target: str, text: str) -> bool:
    """True if Chinese leaked into a target that is NOT Chinese (zh*). Lets us keep
    Chinese OUT of e.g. a Japanese caption now, while still allowing --to zh later."""
    if target.lower().startswith("zh"):
        return False
    return any(c in _SIMPLIFIED for c in text)


def _has_kana(s: str) -> bool:
    """Hiragana/katakana — present in genuine Japanese, absent in Chinese. Lets us
    tell a Japanese source from a Chinese one (both are CJK) for --to ja."""
    return any(
        "ぁ" <= c <= "ゟ"     # hiragana
        or "゠" <= c <= "ヿ"  # katakana
        or "ｦ" <= c <= "ﾟ"  # half-width katakana
        for c in s
    )


def already_in_target(target: str, text: str) -> bool:
    """Skip translation when the text is already in the target language — a cheap
    script check that avoids a needless LLM call and 'translating' same-language."""
    t = target.lower()
    if t == "ja":
        return _has_kana(text)   # kana => genuinely Japanese (Chinese has none)
    if t in ("en", "es", "fr", "de", "it", "pt"):
        return (not _has_cjk(text)) and any(c.isascii() and c.isalpha() for c in text)
    return False


def _system_prompt(lang: str) -> str:
    # Target-specific guard: qwen sometimes emits Chinese for a Japanese target,
    # especially on short fragments. Pin the script explicitly.
    extra = ""
    if lang == "Japanese":
        extra = (" Write in natural Japanese using hiragana, katakana and kanji. "
                 "NEVER output Chinese.")
    return (
        f"You are a professional real-time interpreter. Translate the user's text into "
        f"natural, fluent {lang}. Output ONLY the translation in {lang} — no preamble, "
        f"no quotes, no notes, no romanization, no source text.{extra} Translate EVERY "
        f"word into {lang}; do NOT leave words in the source language. Keep only genuine "
        f"proper nouns (brand/product/person names such as ChatGPT, Google) as-is. "
        f"Preserve meaning and tone. Translate even a short fragment."
    )


class OllamaTranslator:
    """Translate caption text via the local Ollama server (free, on-device)."""

    def __init__(self, model: str, url: str, target: str):
        self.model = model
        self.url = url.rstrip("/")
        self.target = target.lower()
        self.lang = language_name(self.target)
        # Own session (not the refiner's shared one) so the transcribe thread and
        # the suggestion worker never touch the same Session concurrently.
        self._session = requests.Session()
        self._session.trust_env = False

    def _chat(self, system: str, text: str) -> str:
        try:
            resp = self._session.post(
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
                        {"role": "system", "content": system},
                        {"role": "user", "content": text},
                    ],
                },
                timeout=60,
            )
            resp.raise_for_status()
            return (resp.json()["message"]["content"] or "").strip()
        except Exception:
            return ""

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text or already_in_target(self.target, text):
            return text
        out = self._chat(_system_prompt(self.lang), text)
        # If Chinese leaked into a non-Chinese target, retry once, harder.
        if out and leaked_nontarget_chinese(self.target, out):
            hard = _system_prompt(self.lang) + (
                f" CRITICAL: output ZERO Chinese characters. Write strictly in {self.lang}."
            )
            retry = self._chat(hard, text)
            if retry:
                out = retry
        return out or text  # never break captions — show the source on failure
