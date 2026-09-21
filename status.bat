@echo off
chcp 65001 >nul
title Antigravity Proxy Status
echo ========================================================
echo             Antigravity Proxy Status
echo ========================================================
echo.
powershell -NoProfile -Command ^
    "try { " ^
    "    $res = Invoke-RestMethod -Uri 'http://127.0.0.1:8877/health' -TimeoutSec 2; " ^
    "    Write-Host 'Service:            ' $res.service; " ^
    "    Write-Host 'Status:             ONLINE' -ForegroundColor Green; " ^
    "    Write-Host 'Authenticated:      ' $res.auth.authenticated; " ^
    "    Write-Host 'Has Refresh Token:  ' $res.auth.has_refresh_token; " ^
    "    Write-Host 'Expires In:         ' ($res.auth.expires_in_seconds.ToString() + ' seconds'); " ^
    "    Write-Host 'Project ID:         ' $res.auth.project_id; " ^
    "    Write-Host 'Token File:         ' $res.token_file; " ^
    "} catch { " ^
    "    Write-Host 'Proxy Service:      OFFLINE (port 8877 not responding)' -ForegroundColor Yellow; " ^
    "    Write-Host ''; " ^
    "    Write-Host 'Checking token file on disk...'; " ^
    "    python antigravity_proxy.py --check-token; " ^
    "}"
echo.
echo ========================================================
pause
