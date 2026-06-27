# Koe — one-time setup (Windows PowerShell)
# Creates a virtual environment and installs all dependencies.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "Creating virtual environment (.venv)…" -ForegroundColor Cyan
python -m venv .venv

Write-Host "Upgrading pip…" -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip

Write-Host "Installing dependencies (this downloads ~1-2 GB incl. CUDA libs)…" -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host ""
Write-Host "Done. Run it with:" -ForegroundColor Green
Write-Host "    .\.venv\Scripts\python.exe run.py" -ForegroundColor Yellow
Write-Host "(first run downloads the Whisper model once, then works offline)"
