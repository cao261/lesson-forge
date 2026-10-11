@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title LessonForge · AI 视频智能教案工坊

echo ============================================================
echo   LessonForge · AI 视频智能教案工坊
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [提示] 未检测到 .venv 虚拟环境。
    if exist "setup.bat" (
        echo 正在调用 setup.bat 初始化安装环境...
        call setup.bat
    ) else (
        echo 请先执行 setup.bat 安装环境后重试。
        pause
        exit /b 1
    )
)

if not exist ".env" (
    echo [提示] 未找到 .env 配置文件，正在创建...
    copy ".env.example" ".env" >nul
    echo 正在打开记事本，请填入 PRIMARY_API_KEY 后保存关闭。
    notepad ".env"
)

echo 正在启动 LessonForge 网页控制台...
echo 浏览器即将自动打开 http://127.0.0.1:8760
echo (如需停止服务，直接关闭此窗口或按 Ctrl+C)
echo ------------------------------------------------------------
echo.

".venv\Scripts\python.exe" -u run.py %*
if errorlevel 1 pause
endlocal
