@echo off
setlocal
title Celery EMG EXE Builder
pushd "%~dp0"

where py >nul 2>nul
if errorlevel 1 goto no_python

if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv ".venv"
    if errorlevel 1 py -3 -m venv ".venv"
    if errorlevel 1 goto build_failed
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "requirements-dev.txt"
if errorlevel 1 goto build_failed

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name "CeleryEMG" --workpath "%TEMP%\CeleryEMG-PyInstaller" --specpath "%TEMP%" --distpath "%CD%\dist" "celery_emg_app.py"
if errorlevel 1 goto build_failed

echo.
echo Build complete: %CD%\dist\CeleryEMG.exe
explorer "%CD%\dist"
pause
popd
exit /b 0

:no_python
echo Python 3 is required to build the EXE.
pause
popd
exit /b 1

:build_failed
echo.
echo The EXE build failed. Review the messages above for details.
pause
popd
exit /b 1
