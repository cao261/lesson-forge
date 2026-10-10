@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title LessonForge Setup

echo ============================================================
echo   LessonForge  --  first-time setup  [run once]
echo ============================================================
echo.

set "BOOT="
call :probe "E:\ruanjian\python.exe"
call :probe "python"
call :probe "py"

if not defined BOOT goto :nopython

echo   Using Python: %BOOT%
"%BOOT%" -c "import sys;print('  version', sys.version.split()[0])"
echo.

if not exist ".venv" goto :mkvenv
echo   [1/3] Virtual environment already exists, skipping.
goto :deps

:mkvenv
echo   [1/3] Creating virtual environment, about 1 minute ...
"%BOOT%" -m venv .venv
if errorlevel 1 goto :fail

:deps
echo.
echo   [2/3] Installing dependencies. The first run takes a while:
echo         faster-whisper pulls CUDA packages, several hundred MB.
echo.
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo   [3/3] Optional: GPU acceleration.
echo         Much faster with an NVIDIA GPU. Not required, just slower.
echo.
choice /c YN /n /m "         Install GPU packages now? [Y/N] "
if errorlevel 2 goto :skipgpu
".venv\Scripts\python.exe" -m pip install -r requirements-gpu.txt
if errorlevel 1 echo   [Note] GPU packages failed to install. CPU still works.
goto :finish

:skipgpu
echo         OK, skipping GPU packages.

:finish
echo.
echo ============================================================
echo   Setup finished.
echo.
echo   Next step: double-click the desktop icon, or run start.bat
echo.
echo   On first start you will be asked for your MiniMax API key.
echo   Backup model agnes-3.0-flash is preconfigured; LessonForge
echo   switches to it automatically when the primary model runs out.
echo ============================================================
echo.
pause >nul
exit /b 0

:nopython
echo.
echo   [ERROR] Python 3.10 or newer was not found.
echo   Download it from  https://www.python.org/downloads/
echo   During install, tick the box that adds python.exe to PATH.
echo.
pause
exit /b 1

:fail
echo.
echo   [FAILED] Something went wrong above. Please screenshot the error.
echo   Most common cause: slow network causing a pip timeout.
echo   Just run this file again.
echo.
pause
exit /b 1

:probe
%~1 -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 if not defined BOOT set "BOOT=%~1"
exit /b 0
