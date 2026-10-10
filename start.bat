@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title LessonForge

echo ============================================================
echo   LessonForge  --  video to lesson-plan generator
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" goto :nosetup
if not exist ".env" goto :noconf

echo   Starting. Your browser will open  http://127.0.0.1:8760
echo   To stop: close this window, or press Ctrl+C
echo.
".venv\Scripts\python.exe" -u run.py
set "RC=%ERRORLEVEL%"
goto :done

:nosetup
echo   [Setup needed] The virtual environment is missing.
echo   Double-click setup.bat in this folder first, then run this again.
echo.
pause
exit /b 1

:noconf
echo   [Config needed] Creating .env from .env.example ...
copy ".env.example" ".env" >nul
echo   Opening Notepad now.
echo   Put your MiniMax PRIMARY_API_KEY into it, save, close Notepad,
echo   then run this file again.
echo.
notepad ".env"
pause
exit /b 1

:done
echo.
if not "%RC%"=="0" echo   Exited with code %RC% -- check the traceback above.
echo   Press any key to close.
pause >nul
endlocal
