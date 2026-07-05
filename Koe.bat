@echo off
rem Koe — double-click to open the control center.
cd /d "%~dp0"
".venv\Scripts\python.exe" launcher.py %*
echo.
pause
