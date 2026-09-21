@echo off
chcp 65001 >nul
title Antigravity Proxy - Google Login
echo ========================================================
echo        Antigravity Proxy - Google Authentication
echo ========================================================
echo.
echo This will open your default browser to sign in with your
echo Google account and save the required Antigravity token.
echo.
echo Callback URL: http://localhost:51121/oauth-callback
echo.
python antigravity_proxy.py --login %*
echo.
echo ========================================================
pause
