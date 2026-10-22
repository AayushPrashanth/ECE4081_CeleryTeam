@echo off
setlocal
title Celery EMG Launcher
pushd "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import serial" >nul 2>nul
    if not errorlevel 1 goto launch
)

echo Preparing Celery EMG for first use...
where py >nul 2>nul
if errorlevel 1 goto no_python

if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv ".venv"
    if errorlevel 1 py -3 -m venv ".venv"
    if errorlevel 1 goto setup_failed
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "requirements.txt"
if errorlevel 1 goto setup_failed

:launch
start "Celery EMG" ".venv\Scripts\pythonw.exe" "celery_emg_app.py"
popd
exit /b 0

:no_python
echo.
echo Python 3 is required for the first launch.
echo Install Python 3.12 from https://www.python.org/downloads/windows/
echo and select "Add python.exe to PATH", then run this file again.
pause
popd
exit /b 1

:setup_failed
echo.
echo Celery EMG setup did not complete. Check your internet connection and Python installation.
pause
popd
exit /b 1
