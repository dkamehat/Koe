"""③ Refiner — context-aware correction layer (pluggable, local-first).

This is the context-aware cleanup: an LLM rewrites the raw transcription into the
text the user *meant* — fixing mis-hearings, punctuation, filler words and casing
— using the terminology dictionary (and later, the active-app context) as hints.

Design constraints from the project thesis:
- **Local by default, safe by default.** With backend "auto" we only ever call a
  LOCAL Ollama server; if it isn't running we silently fall back to deterministic
  rule formatting. Nothing leaves the machine unless the user *explicitly* selects
  a cloud backend.
- **Cloud is bring-your-own-key.** Claude / OpenAI keys are read from environment
  variables ONLY, never from config.json, so a shared/distributed config can never
  leak a key or silently enable the cloud.
- **Never breaks dictation.** Any backend error degrades gracefully to rules.

Backends: "auto" (local Ollama if present, else rules) · "none"/"rules" ·
"ollama" · "claude" · "openai".
"""

from __future__ import annotations

import json
import os

import requests

# Reused connection for the local ollama server. trust_env=False skips proxy
# lookups; combined with a 127.0.0.1 URL this avoids the ~2s/request overhead
# that plagues "localhost" + a fresh connection on Windows.
_ollama_session = requests.Session()
_ollama_session.trust_env = False

# Sentence terminators we commit on when streaming (JP + EN + newline).
_BOUNDARY = "。．！？\n.!?"

from .formatter import format_text

# Keep the model on a tight leash: it corrects, it does not converse or invent.
SYSTEM_PROMPT = (
    "You lightly clean up raw speech-to-text dictation. Return ONLY the cleaned text "
    "— no preamble, no quotes, no explanation.\n"
    "ABSOLUTE RULE: NEVER translate. The output MUST be in the SAME language as the "
    "input (Japanese in -> Japanese out, English in -> English out), even if the "
    "dictation talks about translating.\n"
    "PRESERVE THE ORIGINAL: keep the speaker's exact wording, word order and sentence "
    "structure. Do NOT paraphrase, summarize, shorten, merge sentences, or 'improve' "
    "the phrasing. When in doubt, leave it as-is.\n"
    "Your ONLY allowed edits are:\n"
    "1. Remove filler words / false starts (um, uh, you know / えー, あのー, えーと, まあ).\n"
    "2. Add natural punctuation, casing and spacing (Japanese uses 。、).\n"
    "3. Fix clear mis-transcriptions of words that were actually spoken — never add "
    "new information.\n"
    "4. Apply the spelling of any known term when the speech clearly refers to it "
    "(e.g. heard 'クバネティス' -> 'Kubernetes').\n"
    "Keep every content word the speaker said. If already clean, return it unchanged."
)

# Few-shot pairs teach the behavior far more reliably than instructions alone —
# especially Japanese filler removal and canonical proper-noun spelling.
_FEWSHOT: list[tuple[str, str]] = [
    (
        "Known terms: Kubernetes, PostgreSQL\n\nRaw dictation:\n"
        "えーと あのー 今日の会議でね 来週の締め切りについて話しました "
        "たぶん木曜日までには終わると思います",
        "今日の会議で、来週の締め切りについて話しました。"
        "たぶん木曜日までには終わると思います。",
    ),
    (
        "Raw dictation:\n"
        "um so i think we should uh ship the the local version first you know",
        "So I think we should ship the local version first.",
    ),
    (
        "Known terms: Kubernetes, PostgreSQL\n\nRaw dictation:\n"
        "クバネティスの上で ポスグレっていうデータベースをね 動かしてて",
        "Kubernetesの上で、PostgreSQLというデータベースを動かしています。",
    ),
    # Anti-translation: Japanese in -> Japanese out, even when the speech talks
    # about transcribing/English. (Mirrors a real failure we saw.)
    (
        "Raw dictation:\n"
        "えー これ録音されてるけど 文字に起こしてください 緑茶おいしい テレビ消えてる",
        "これは録音されていますが、文字に起こしてください。緑茶、おいしい。テレビが消えています。",
    ),
    # Preserve wording/word order: keep the question form and the speaker's verb,
    # only add punctuation. (Counters over-paraphrasing we saw.)
    (
        "Raw dictation:\n"
        "これは日本語と英語の判定はどのようにしてますか Transformerでやっています",
        "これは日本語と英語の判定はどのようにしてますか？ Transformerでやっています。",
    ),
]


def _has_cjk(s: str) -> bool:
    return any(
        "぀" <= c <= "ヿ"   # hiragana + katakana
        or "一" <= c <= "鿿"  # CJK ideographs
        or "ｦ" <= c <= "ﾝ"  # half-width katakana
        for c in s
    )


def _language_preserved(raw: str, out: str) -> bool:
    """True unless the refiner clearly switched languages (i.e. translated).

    The deterministic safety net behind the prompt: if the input has Japanese
    but the output dropped all Japanese (or vice-versa), it translated — reject it.
    """
    raw_cjk, out_cjk = _has_cjk(raw), _has_cjk(out)
    if raw_cjk and not out_cjk:
        return False  # Japanese in, English out -> translated
    # English in, Japanese out: only flag when the input was real latin text.
    if (not raw_cjk) and out_cjk and any(c.isascii() and c.isalpha() for c in raw):
        return False
    return True


def _build_user_prompt(raw: str, terms: list[str], context: str | None) -> str:
    parts = []
    lang = "Japanese" if _has_cjk(raw) else "English"
    parts.append(f"Output language MUST be {lang}. Do NOT translate.")
    if terms:
        parts.append("Known terms: " + ", ".join(terms[-60:]))
    if context:
        parts.append("Surrounding text (for context, do not repeat it):\n" + context)
    parts.append("Raw dictation:\n" + raw)
    return "\n\n".join(parts)


def _chat_messages(raw: str, terms: list[str], context: str | None,
                   include_system: bool) -> list[dict]:
    """Full message list with few-shot priming. Claude takes system separately,
    so include_system=False there."""
    msgs: list[dict] = []
    if include_system:
        msgs.append({"role": "system", "content": SYSTEM_PROMPT})
    for u, a in _FEWSHOT:
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": _build_user_prompt(raw, terms, context)})
    return msgs


class Refiner:
    """Base: deterministic rule formatting, no LLM (the safe default)."""

    name = "rules"

    def __init__(self, auto_capitalize: bool = True):
        self.auto_capitalize = auto_capitalize

    def refine(self, raw: str, terms: list[str], context: str | None = None) -> str:
        return format_text(raw, enable=True, auto_capitalize=self.auto_capitalize)

    def _fallback(self, raw: str) -> str:
        return format_text(raw, enable=True, auto_capitalize=self.auto_capitalize)

    def _guard(self, raw: str, out: str | None) -> str:
        """Reject empty or translated LLM output; fall back to safe rule formatting.
        This is the hard guarantee that the ③ layer can never silently translate."""
        out = (out or "").strip()
        if not out or not _language_preserved(raw, out):
            return self._fallback(raw)
        return out


class OllamaRefiner(Refiner):
    """Local LLM via Ollama (http://localhost:11434). Free, on-device, safe."""

    name = "ollama"

    def __init__(self, model: str, url: str, auto_capitalize: bool = True):
        super().__init__(auto_capitalize)
        self.model = model
        self.url = url.rstrip("/")

    def refine(self, raw: str, terms: list[str], context: str | None = None) -> str:
        try:
            resp = _ollama_session.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "options": {
                        "temperature": 0.2,
                        "num_predict": _num_predict(raw),  # bound output length
                    },
                    "keep_alive": "10m",  # stay resident in VRAM for low latency
                    "messages": _chat_messages(raw, terms, context, include_system=True),
                },
                timeout=60,
            )
            resp.raise_for_status()
            out = resp.json()["message"]["content"]
            return self._guard(raw, out)
        except Exception:
            # Never break dictation — fall back to deterministic rules.
            return self._fallback(raw)

    def refine_stream(self, raw, terms, context, emit) -> tuple[str | None, bool]:
        """Stream the correction, calling emit(sentence) as each sentence completes
        (forward-append only). Returns (full_text, ok). ok=False means the output was
        empty or a translation slipped through — the caller should fall back AND must
        NOT have emitted anything yet (we language-check before the first emit)."""
        try:
            resp = _ollama_session.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "stream": True,
                    "options": {"temperature": 0.2, "num_predict": _num_predict(raw)},
                    "keep_alive": "10m",
                    "messages": _chat_messages(raw, terms, context, include_system=True),
                },
                timeout=60,
                stream=True,
            )
            resp.raise_for_status()
            buf, full, checked = "", "", False
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line).get("message", {}).get("content", "")
                if not chunk:
                    continue
                buf += chunk
                full += chunk
                # Commit completed sentences from the buffer.
                i = next((k for k, c in enumerate(buf) if c in _BOUNDARY), -1)
                while i != -1:
                    sentence, buf = buf[: i + 1], buf[i + 1:]
                    if not checked:
                        checked = True
                        if not _language_preserved(raw, full):
                            return None, False   # translated -> abort before emitting
                    emit(sentence)
                    i = next((k for k, c in enumerate(buf) if c in _BOUNDARY), -1)
            # Flush any trailing partial sentence.
            tail = buf.strip()
            if tail:
                if not checked and not _language_preserved(raw, full):
                    return None, False
                emit(buf)
            if not full.strip():
                return None, False
            return full.strip(), True
        except Exception:
            return None, False


class ClaudeRefiner(Refiner):
    """Anthropic API. Bring-your-own key via ANTHROPIC_API_KEY. Metered/cloud."""

    name = "claude"

    def __init__(self, model: str, auto_capitalize: bool = True):
        super().__init__(auto_capitalize)
        self.model = model
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    def refine(self, raw: str, terms: list[str], context: str | None = None) -> str:
        if not self.api_key:
            return self._fallback(raw)
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": _num_predict(raw),
                    "system": SYSTEM_PROMPT,
                    "messages": _chat_messages(raw, terms, context, include_system=False),
                },
                timeout=30,
            )
            resp.raise_for_status()
            return self._guard(raw, resp.json()["content"][0]["text"])
        except Exception:
            return self._fallback(raw)


class OpenAIRefiner(Refiner):
    """OpenAI API. Bring-your-own key via OPENAI_API_KEY. Metered/cloud."""

    name = "openai"

    def __init__(self, model: str, auto_capitalize: bool = True):
        super().__init__(auto_capitalize)
        self.model = model
        self.api_key = os.environ.get("OPENAI_API_KEY", "")

    def refine(self, raw: str, terms: list[str], context: str | None = None) -> str:
        if not self.api_key:
            return self._fallback(raw)
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "temperature": 0.2,
                    "max_tokens": _num_predict(raw),
                    "messages": _chat_messages(raw, terms, context, include_system=True),
                },
                timeout=30,
            )
            resp.raise_for_status()
            return self._guard(raw, resp.json()["choices"][0]["message"]["content"])
        except Exception:
            return self._fallback(raw)


def _num_predict(raw: str) -> int:
    """Cap output length to ~the input size. A post-processor mostly copies the
    input, so this bounds latency and also stops the model from rambling/adding."""
    return max(64, min(512, int(len(raw) * 1.5) + 48))


def _ollama_available(url: str) -> bool:
    try:
        r = _ollama_session.get(f"{url.rstrip('/')}/api/tags", timeout=1.5)
        return r.ok and bool(r.json().get("models"))
    except Exception:
        return False


def build_refiner(cfg) -> Refiner:
    """Pick a backend from config, honoring the local-first / safe-by-default rule."""
    backend = (getattr(cfg, "refiner_backend", "auto") or "auto").lower()
    cap = getattr(cfg, "auto_capitalize", True)

    if backend in ("none", "rules"):
        return Refiner(cap)
    if backend == "ollama":
        return OllamaRefiner(cfg.ollama_model, cfg.ollama_url, cap)
    if backend == "claude":
        return ClaudeRefiner(cfg.claude_model, cap)
    if backend == "openai":
        return OpenAIRefiner(cfg.openai_model, cap)

    # "auto": use a LOCAL Ollama only if it's already running; otherwise rules.
    if _ollama_available(cfg.ollama_url):
        return OllamaRefiner(cfg.ollama_model, cfg.ollama_url, cap)
    return Refiner(cap)
