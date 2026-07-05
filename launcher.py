#!/usr/bin/env python
"""Koe control center — one launcher window to start/stop every Koe service.

Entry point for the "Koe コントロールセンター": one row per end-user service
(Dictation, Interpreter+captions, Talk) with a status lamp and a start/stop
button. Closing the window stops everything it started — no orphan
processes the user can't see.

The window (koe/launchergui.py) is a thin tkinter edge over the pure
koe/launcher.py ServiceManager. v1 ships as a window, not a tray icon
(discoverability for the "誰でも使える" audience); a tray front-end later
would only need to swap that thin edge, not rewrite the manager underneath.

Run with: .venv\\Scripts\\python.exe launcher.py   (or double-click Koe.bat)

MUST be named launcher.py, not koe.py — a root koe.py would shadow the koe
package on import.
"""

from koe.launchergui import main

if __name__ == "__main__":
    main()
