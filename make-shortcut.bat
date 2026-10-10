@echo off
setlocal
cd /d "%~dp0"
title LessonForge - Desktop Shortcut

echo.
echo   Creating the desktop shortcut ...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$proj=(Get-Location).Path; $n=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('5a625pWZ6KeG6aKR6L2s5pWZ5qGI')); $d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('57KY6LS0IEIg56uZ5pWZ5a2m6KeG6aKR6ZO+5o6l77yM6Ieq5Yqo55Sf5oiQ5a625pWZ5pWZ5qGIIFdvcmQ=')); $lnk=Join-Path ([Environment]::GetFolderPath('Desktop')) ($n + '.lnk'); $sh=New-Object -ComObject WScript.Shell; $s=$sh.CreateShortcut($lnk); $s.TargetPath=(Join-Path $proj 'start.bat'); $s.WorkingDirectory=$proj; $s.Description=$d; $s.IconLocation=([Environment]::GetFolderPath('System') + '\imageres.dll,15'); $s.WindowStyle=1; $s.Save(); if (Test-Path -LiteralPath $lnk) { Write-Host ('   OK  ->  ' + $lnk) -ForegroundColor Green } else { Write-Host '   FAILED to create the shortcut.' -ForegroundColor Red; exit 1 }"

echo.
pause >nul
