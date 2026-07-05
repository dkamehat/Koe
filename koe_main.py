"""PyInstaller entry point for Koe.exe (see koe.spec) — NOT a source entry
point. Running from source, use run.py (dictation), launcher.py (Control
Center), interpreter.py, or talk.py directly, exactly as before this file
existed; this script only exists so the frozen build has ONE Analysis(...)
entry that can become any of them at runtime (see koe/cli.py:main). Named
koe_main.py, never koe.py — a root koe.py would shadow the koe package on
import.
"""

from koe.cli import main

main()
