"""Resolve where Koe keeps its user data (config.json, dictionary.txt).

Running from source, that's the project root (the repo). Running as a frozen
PyInstaller .exe, ``__file__`` points *inside* the bundle (a throwaway temp dir
for one-file builds), so we instead use the folder the .exe lives in — keeping
config.json / dictionary.txt right next to the app where the user can find and
edit them.
"""

from __future__ import annotations

import sys
from pathlib import Path


def data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
