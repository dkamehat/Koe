"""Unit tests for the first-run setup wizard's pure logic (koe/firstrun.py).

Pure tests only — koe/firstrun.py keeps tkinter/sounddevice imports lazy
(inside run_wizard and its helpers), so these run on any OS with no
display/mic (invariant 2). Run with:  python -m pytest
"""

import builtins
import importlib

from koe.firstrun import (input_device_choices, level_fraction,
                           recommend_model)


# --- recommend_model ---------------------------------------------------------

def test_recommend_model_gpu_and_cpu():
    model, rationale = recommend_model(True)
    assert model == "large-v3-turbo"
    assert rationale.strip()

    model, rationale = recommend_model(False)
    assert model == "small"
    assert rationale.strip()


# --- input_device_choices -----------------------------------------------------

def test_device_choices_filters_outputs():
    devices = [
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "USB Mic", "max_input_channels": 1, "max_output_channels": 0},
    ]
    choices = input_device_choices(devices, default_index=None)
    labels = [label for _, label in choices]
    assert not any("Speakers" in label for label in labels)
    assert any(label == "1: USB Mic" for label in labels)


def test_device_choices_marks_default():
    devices = [
        {"name": "USB Mic", "max_input_channels": 1, "max_output_channels": 0},
        {"name": "Built-in Mic", "max_input_channels": 2, "max_output_channels": 0},
    ]
    choices = input_device_choices(devices, default_index=1)
    assert choices[0] == (None, "既定のマイク（Windows設定に従う）")
    marked = [label for idx, label in choices if idx == 1]
    assert marked and marked[0].endswith(" ← 既定")
    unmarked = [label for idx, label in choices if idx == 0]
    assert unmarked and not unmarked[0].endswith(" ← 既定")


def test_device_choices_empty_list():
    choices = input_device_choices([], default_index=None)
    assert choices == [(None, "既定のマイク（Windows設定に従う）")]


# --- level_fraction ------------------------------------------------------------

def test_level_fraction_clamps():
    assert level_fraction(0.0) == 0.0
    assert level_fraction(0.15) == 1.0
    assert level_fraction(1.0) == 1.0
    assert level_fraction(0.05) < level_fraction(0.1) < level_fraction(0.15)


# --- module import safety (invariant 2) -----------------------------------------

def test_firstrun_module_imports_headless(monkeypatch):
    """koe/firstrun.py must not import tkinter/sounddevice at module scope —
    block both via an import shim (the overlay test's technique), so this
    stays meaningful even on a machine that has them installed."""
    import koe.firstrun as firstrun_module

    blocked = {"tkinter", "sounddevice"}
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] in blocked:
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    importlib.reload(firstrun_module)  # re-run the module body under the shim


def test_app_compiles():
    """koe/app.py can't be *imported* headless (it is an I/O edge: `keyboard`
    at module scope, by design and predating this spec) — so guard the wizard
    hook's syntax via compilation instead of import."""
    import py_compile
    from pathlib import Path
    py_compile.compile(str(Path(__file__).resolve().parent.parent / "koe" / "app.py"),
                       doraise=True)
