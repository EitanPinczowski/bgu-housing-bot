@echo off
REM ===================================================================
REM  The FAST pass: `main.py --live --hot`.
REM
REM  Visits only the HOT_GROUP_COUNT highest-yield groups and reads just
REM  HOT_MIN_POSTS newest posts in each — about 30 page-reads, versus ~251
REM  for a full run. It exists so a good listing is seen in ~30-40 min
REM  instead of waiting for the next 2-hourly full scan.
REM
REM  It had NEVER been scheduled. The code and the volume budget in
REM  CLAUDE.md both assumed it was running; the Task Scheduler had exactly
REM  one scraper task calling run_scraper.cmd with no arguments. Measured
REM  consequence: median time-to-detect 8.4 hours (n=44), with only 7 of 44
REM  listings seen within an hour.
REM
REM  Same conventions as run_scraper.cmd: UTF-8, unbuffered, per-run log,
REM  OSRM self-heal. Every safety rule is unchanged — real logged-in
REM  profile, randomized delays + jitter, daytime only, read-only.
REM ===================================================================
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "PY=C:\Users\eitan\AppData\Local\Python\pythoncore-3.14-64\python.exe"

docker start osrm_bgu >nul 2>&1

if not exist "data\runs" mkdir "data\runs"
set "RUNLOG=data\runs\hot-%DATE:~-4%%DATE:~3,2%%DATE:~0,2%-%TIME:~0,2%%TIME:~3,2%.log"
set "RUNLOG=%RUNLOG: =0%"

echo ==== hot run start %DATE% %TIME% ====> "%RUNLOG%"
"%PY%" -u main.py --live --hot >> "%RUNLOG%" 2>&1
echo ==== hot run end   %DATE% %TIME% (exit %ERRORLEVEL%) ====>> "%RUNLOG%"
type "%RUNLOG%" >> "data\scraper_runs.log" 2>nul
endlocal
