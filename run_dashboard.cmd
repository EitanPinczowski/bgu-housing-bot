@echo off
REM ===================================================================
REM Supervised launcher for the live dashboard server.
REM
REM Serves data/dashboard.html from SQLite on every request, so the page
REM is never stale and can be opened from a phone. A TOKEN IS REQUIRED on
REM every request — the page lists landlords' phone numbers and addresses.
REM The console prints the URLs with the token already in them.
REM
REM   shell:startup  ->  shortcut to this file (Run: minimized)
REM
REM This server is LAN-only on purpose — no tunnel, no VPN. Away from home,
REM use the published snapshot (run_dashboard_share.cmd + publish.py).
REM
REM Stop it by closing this window.
REM ===================================================================

cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe
if not exist "%PY%" set PY=python

:loop
echo [%DATE% %TIME%] starting dashboard server...
"%PY%" -u serve_dashboard.py
echo [%DATE% %TIME%] server exited (code %ERRORLEVEL%) - restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
