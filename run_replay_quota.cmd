@echo off
REM ===================================================================
REM Launcher for the one-shot "run the clean Gemini replay when quota
REM resets" job (see replay_on_quota.py). Invoked hourly by the "BGU
REM Replay Quota" scheduled task; the wrapper no-ops until Gemini quota
REM is back, runs the replay once, then deletes the task. Mirrors
REM run_scraper.cmd (own-folder cd, UTF-8, pinned interpreter, logged).
REM ===================================================================
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY=C:\Users\eitan\AppData\Local\Python\pythoncore-3.14-64\python.exe"

echo ==== replay-quota check %DATE% %TIME% ====>> "data\replay_quota.log"
"%PY%" replay_on_quota.py >> "data\replay_quota.log" 2>&1
endlocal
