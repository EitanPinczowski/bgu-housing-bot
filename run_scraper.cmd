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
REM UNBUFFERED: with output redirected to a file Python block-buffers stdout, so a run
REM that never exits flushes NOTHING. On 2026-07-27 a run worked for ~7h then wedged for
REM ~30h and its log showed only the start banner — impossible to diagnose. -u fixes that.
set "PYTHONUNBUFFERED=1"
set "PY=C:\Users\eitan\AppData\Local\Python\pythoncore-3.14-64\python.exe"

REM Self-heal the OSRM walk-time server. This used to be a bare
REM `docker start osrm_bgu >nul 2>&1`, which cannot help when the Docker ENGINE
REM itself is down — and 14 of 88 completed runs logged "OSRM DOWN" anyway, with
REM >nul hiding the reason every time. doctor --fix starts Docker Desktop first
REM when the engine is unreachable, then the container.
REM Output goes to the run log, NOT to nul: the whole point is that a failed heal
REM should leave a trace. --quiet keeps it to the repair lines — the full dependency
REM table 7x/day would bury the log it is written to. Never fatal: the bot
REM deliberately still classifies without OSRM, on the straight-line walk estimate.
"%PY%" -u doctor.py --fix --quiet >> "data\scraper_runs.log" 2>&1

REM Per-run log file. A single shared log is a single point of failure: a wedged
REM predecessor holding that handle can stop every later run from even starting.
if not exist "data\runs" mkdir "data\runs"
set "RUNLOG=data\runs\scraper-%DATE:~-4%%DATE:~3,2%%DATE:~0,2%-%TIME:~0,2%%TIME:~3,2%.log"
set "RUNLOG=%RUNLOG: =0%"

echo ==== run start %DATE% %TIME% ====> "%RUNLOG%"
"%PY%" -u main.py --live >> "%RUNLOG%" 2>&1
echo ==== run end   %DATE% %TIME% (exit %ERRORLEVEL%) ====>> "%RUNLOG%"
REM keep the familiar rolling log as a summary trail too (best-effort)
type "%RUNLOG%" >> "data\scraper_runs.log" 2>nul
endlocal
