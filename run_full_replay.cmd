@echo off
REM Full replay preview, start to finish, unattended.
REM
REM   run_full_replay.cmd            preview only  (safe: writes nothing)
REM   run_full_replay.cmd --apply    preview, then WRITE the verdicts + rebuild the Sheet
REM
REM Everything it needs is checked first and it refuses rather than half-runs. Output
REM lands in data\full_replay_<timestamp>.txt.
cd /d "%~dp0"
python -u full_replay.py %*
echo.
echo Exit code: %ERRORLEVEL%
pause
