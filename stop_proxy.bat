@echo off
chcp 65001 >nul
title Stop Antigravity Proxy
echo ========================================================
echo             Stopping Antigravity Proxy
echo ========================================================
echo.
powershell -NoProfile -Command ^
    "$conns = Get-NetTCPConnection -LocalPort 8877 -State Listen -ErrorAction SilentlyContinue; " ^
    "if ($conns) { " ^
    "    foreach ($c in $conns) { " ^
    "        $pidNum = $c.OwningProcess; " ^
    "        Stop-Process -Id $pidNum -Force -ErrorAction SilentlyContinue; " ^
    "        Write-Host ('Successfully stopped proxy process PID ' + $pidNum); " ^
    "    } " ^
    "} else { " ^
    "    Write-Host 'No Antigravity Proxy listening on port 8877.'; " ^
    "}"
echo.
echo Done.
timeout /t 2 >nul
