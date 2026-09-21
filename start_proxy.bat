@echo off
chcp 65001 >nul
title Antigravity Proxy
echo ========================================================
echo             Antigravity Proxy Launcher
echo ========================================================
echo.
echo Starting local OpenAI-compatible proxy on http://127.0.0.1:8877/v1
echo Press Ctrl+C in this window to stop the server.
echo.
python antigravity_proxy.py %*
if %errorlevel% neq 0 (
    echo.
    echo ========================================================
    echo Proxy exited with an error code %errorlevel%.
    echo If this is an authentication error, please run login.bat
    echo ========================================================
    pause
)
