"""Unit tests for koe/recorder.py's pure diagnostics helper and its
invariant-2 contract (importable with no audio stack installed).
"""

import builtins
import importlib

from koe.recorder import format_device_label


# --- format_device_label (pure) ---------------------------------------------

def test_format_device_label_explicit_index():
    info = {"name": "Microphone Array (Realtek)"}
    assert format_device_label(3, info) == "#3: Microphone Array (Realtek)"


def test_format_device_label_default_index():
    info = {"name": "Realtek(R) Audio"}
    assert format_device_label(None, info) == "既定: Realtek(R) Audio"


def test_format_device_label_query_failed():
    # sounddevice couldn't resolve the device (stale index, driver hiccup) —
    # must degrade to a plain tag, never raise, never silently disappear.
    assert format_device_label(2, None) == "#2（デバイス名を取得できませんでした）"
    assert format_device_label(None, None) == "既定（デバイス名を取得できませんでした）"


def test_format_device_label_missing_name_key():
    assert format_device_label(0, {}) == "#0: ?"


# --- module import safety (invariant 2) -------------------------------------

def test_recorder_module_imports_headless(monkeypatch):
    """koe/recorder.py must not import sounddevice at module scope — block it
    via an import shim (same technique as test_launcher.py's headless-import
    tests), so this stays meaningful even on a machine that has it installed."""
    import koe.recorder as recorder_module

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] == "sounddevice":
            raise ImportError("blocked for test: sounddevice")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    importlib.reload(recorder_module)  # re-run the module body under the shim
