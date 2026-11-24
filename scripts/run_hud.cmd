@echo off
setlocal
cd /d "%~dp0\.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m arisu_cua.hud.overlay
) else (
    echo Virtualenv .venv\Scripts\python.exe tidak ditemukan. Jalankan uv sync terlebih dahulu.
    exit /b 1
)
