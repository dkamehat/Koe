"""User configuration. Loaded from config.json next to the project root.

A default config.json is written on first run so the user can tweak it without
touching code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .paths import data_dir

PROJECT_ROOT = data_dir()
CONFIG_PATH = PROJECT_ROOT / "config.json"


@dataclass
class Config:
    # --- Model ---
    # "large-v3-turbo" = best speed/accuracy on a good GPU.
    # Lighter options for quick tests / weak machines: "small", "medium", "large-v3".
    model: str = "large-v3-turbo"
    # "cuda" to use the GPU, "cpu" to force CPU, "auto" to detect.
    device: str = "auto"
    # "float16" on GPU, "int8" on CPU. "auto" picks per device.
    compute_type: str = "auto"
    # Language hint. None / "auto" = autodetect (great for JP/EN mixing).
    language: str | None = None

    # --- Hotkey ---
    # Push-to-talk: hold this key, speak, release to transcribe & type.
    # Examples: "right ctrl", "right alt", "f9", "scroll lock".
    hotkey: str = "right ctrl"
    # "ptt" = push-to-talk (hold). "toggle" = press once to start, again to stop.
    hotkey_mode: str = "toggle"

    # --- Audio ---
    sample_rate: int = 16000
    # None = system default input device. Or set a device index (see --list-devices).
    input_device: int | None = None
    # Preroll prepends the last N seconds of audio so the first word is never
    # clipped by mic start-up latency. Requires an always-on mic while Koe runs;
    # set enable_preroll=false to only open the mic during a take (no preroll).
    enable_preroll: bool = True
    preroll_sec: float = 0.3

    # --- Output ---
    # "paste"  = copy to clipboard + Ctrl+V into the active app (Unicode-safe, fast).
    # "type"   = simulate keystrokes (works in clipboard-restricted apps, slower).
    # "clipboard" = only copy to clipboard, never auto-paste.
    output_mode: str = "paste"
    # Add a trailing space after each dictation so phrases don't run together.
    trailing_space: bool = True
    # Stream the ③ refiner output and type each sentence as soon as it's ready
    # (forward-append only), so text starts appearing seconds sooner on long
    # dictation. Only applies to the local ollama backend with paste/type output.
    stream_output: bool = True

    # --- Formatting ---
    # Apply lightweight cleanup + spoken-command processing ("new line", "改行" etc.).
    enable_formatting: bool = True
    # Initial capitalization for English sentences.
    auto_capitalize: bool = True

    # --- Terminology dictionary (local STT-quality booster) ---
    # Bias Whisper toward your proper nouns/jargon and auto-correct recurring
    # mis-transcriptions. Edit dictionary.txt to add terms.
    enable_dictionary: bool = True

    # --- Context grounding (local visual grounding) ---
    # Read the focused window title (+ focused field text) and feed it to the ③
    # refiner so it can re-rank ambiguous words toward what's on screen. Local-only.
    enable_context: bool = True
    # Also read the focused control's text, not just the window title.
    context_include_field: bool = True
    context_max_chars: int = 400
    # Hard cap on how long we'll wait for context (UI Automation can be slow on
    # some apps). It runs in parallel with STT, so this rarely adds latency.
    context_timeout: float = 0.6

    # --- ③ Refiner: context-aware correction layer (pluggable) ---
    # "auto"   = use a LOCAL Ollama if it's running, else deterministic rules.
    #            (Safe default: never sends data out unless YOU pick a cloud backend.)
    # "rules"  = no LLM, deterministic formatting only (fastest, 100% offline).
    # "ollama" = local LLM (free, on-device). Best "zero-yen + high accuracy" option.
    # "claude" = Anthropic API. Needs env var ANTHROPIC_API_KEY (metered, cloud).
    # "openai" = OpenAI API.    Needs env var OPENAI_API_KEY   (metered, cloud).
    refiner_backend: str = "auto"
    ollama_model: str = "qwen2.5:7b"
    # Use 127.0.0.1, NOT "localhost": on Windows, Python resolving "localhost"
    # incurs a ~2s IPv6→IPv4 fallback delay on every request.
    ollama_url: str = "http://127.0.0.1:11434"
    claude_model: str = "claude-haiku-4-5-20251001"
    openai_model: str = "gpt-4o-mini"

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            known = {f for f in cls().__dict__}
            filtered = {k: v for k, v in data.items() if k in known}
            cfg = cls(**filtered)
        else:
            cfg = cls()
            cfg.save()
        return cfg

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
