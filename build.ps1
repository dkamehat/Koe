# Koe — build a self-contained Windows app folder with PyInstaller.
#
#   .\build.ps1
#
# Output: dist\Koe\Koe.exe (+ DLLs). Zip the dist\Koe folder to distribute.
# The Whisper model is downloaded on first run, not bundled.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "No .venv found. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "Ensuring PyInstaller is installed…" -ForegroundColor Cyan
& $py -m pip install --upgrade pyinstaller

Write-Host "Cleaning previous build…" -ForegroundColor Cyan
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }

Write-Host "Building Koe.exe (this takes a few minutes)…" -ForegroundColor Cyan
& ".\.venv\Scripts\pyinstaller.exe" koe.spec --noconfirm

if (Test-Path "dist\Koe\Koe.exe") {
    Write-Host ""
    Write-Host "Built dist\Koe\Koe.exe" -ForegroundColor Green
    Write-Host "Test it:   .\dist\Koe\Koe.exe" -ForegroundColor Yellow
    Write-Host "Distribute: zip the dist\Koe folder." -ForegroundColor Yellow
} else {
    Write-Host "Build did not produce dist\Koe\Koe.exe — check the log above." -ForegroundColor Red
    exit 1
}
