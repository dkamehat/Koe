"""Reply suggestion for Koe Interpreter — the North Star apex.

Given the recent transcript of what the other party said in a live call, suggest
ONE concise, natural reply the user can say back, in the call's language, so a
non-native speaker can respond with confidence. Local-only via the same Ollama
server. Returns "" on any error so the caller can simply skip the suggestion.
"""

from __future__ import annotations

import requests

from .translator import language_name


def _system_prompt(reply_lang: str, role: str | None, context: str | None) -> str:
    base = (
        f"You help a non-native speaker reply in a live conversation. Read what the "
        f"other party just said and suggest ONE natural, concise reply IN {reply_lang} "
        f"that the user can say right now. Output ONLY the reply itself in {reply_lang} "
        f"— no preamble, no quotes, no translation, no notes, no options."
    )
    if role:
        base += f" The user's context/goal: {role}. Make the reply fit that."
    if context:
        base += (
            "\n\nThe user prepared the following background material (their notes, "
            "resume, the job description, the meeting agenda, etc.). Ground the reply "
            "in relevant facts from it and stay consistent with it; do NOT invent "
            "details beyond it:\n----- BACKGROUND -----\n" + context + "\n----- END -----"
        )
    return base


class ReplySuggester:
    def __init__(self, model: str, url: str, role: str | None = None,
                 context: str | None = None):
        self.model = model
        self.url = url.rstrip("/")
        self.role = role
        self.context = context
        # Own session, thread-confined to the suggestion worker.
        self._session = requests.Session()
        self._session.trust_env = False

    def suggest(self, transcript: list[str], reply_lang: str) -> str:
        if not transcript:
            return ""
        convo = "\n".join(transcript[-8:])
        try:
            resp = self._session.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "options": {"temperature": 0.4, "num_predict": 220},
                    "keep_alive": "10m",
                    "messages": [
                        {"role": "system",
                         "content": _system_prompt(language_name(reply_lang),
                                                   self.role, self.context)},
                        {"role": "user",
                         "content": "The other party said (most recent last):\n"
                                    + convo + "\n\nSuggest my reply:"},
                    ],
                },
                timeout=60,
            )
            resp.raise_for_status()
            return (resp.json()["message"]["content"] or "").strip()
        except Exception:
            return ""
