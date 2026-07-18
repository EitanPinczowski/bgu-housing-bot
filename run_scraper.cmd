@echo off
REM ===================================================================
REM Launcher for the BGU housing scraper, invoked by Windows Task
REM Scheduler ~2x/day (see README "Schedule it"). Kept as a file so the
REM scheduled command stays simple and every run is logged.
REM
REM  - %~dp0 = this file's own folder, so it works even if the project
REM    is moved (no hardcoded project path).
REM  - PYTHONUTF8=1 guards against the Windows console choking on Hebrew.
REM  - Full python.exe path: the Store "python" shim is unreliable under
REM    Task Scheduler; use the real interpreter that has the deps.
REM  - All output is appended to data\scraper_runs.log with timestamps so
REM    you can confirm runs happened (silence in Telegram then means a
REM    real break, not just a skipped run).
REM ===================================================================
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY=C:\Users\eitan\AppData\Local\Python\pythoncore-3.14-64\python.exe"

echo ==== run start %DATE% %TIME% ====>> "data\scraper_runs.log"
"%PY%" main.py --live >> "data\scraper_runs.log" 2>&1
echo ==== run end   %DATE% %TIME% (exit %ERRORLEVEL%) ====>> "data\scraper_runs.log"
endlocal
