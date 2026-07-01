# Build a standalone celeborn.exe on Windows. Output: dist\celeborn.exe
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

python -m pip install --quiet --upgrade pyinstaller
python -m PyInstaller --clean --noconfirm packaging/celeborn.spec

Write-Host "OK built dist\celeborn.exe"
& .\dist\celeborn.exe version
