@echo off
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo Virtual environment belum ada.
    echo Jalankan: python -m venv venv
    echo Lalu: venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
venv\Scripts\python.exe app.py
pause
