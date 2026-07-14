@echo off
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo Virtual environment belum ada.
    pause
    exit /b 1
)
set /p SOURCE="Masukkan 0 untuk webcam atau path video: "
set /p SERVER_IP="Masukkan IP server UDP: "
venv\Scripts\python.exe video_sender.py "%SOURCE%" --host "%SERVER_IP%" --port 9002 --audio-port 9003
pause
