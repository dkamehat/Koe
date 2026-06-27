@echo off
rem Koe — start as Administrator so global hotkeys are captured in every app.
cd /d "%~dp0"
powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList 'run.py' -WorkingDirectory '%~dp0'"
