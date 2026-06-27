@echo off
rem Koe — double-click to start dictation.
cd /d "%~dp0"
".venv\Scripts\python.exe" run.py %*
echo.
pause
