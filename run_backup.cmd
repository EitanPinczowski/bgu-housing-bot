@echo off
REM ===================================================================
REM Timestamped snapshot of the source-of-truth SQLite DB.
REM
REM backup_db.py's own docstring has said "Schedule it weekly in Task
REM Scheduler" since it was written, and it never was: on 2026-08-09 there
REM were nine BGU * tasks and none of them was this one. The 14 snapshots on
REM disk were all made BY HAND, and the newest was already 40h old - eight
REM hours from the staleness threshold below, by luck rather than by design.
REM
REM What is at stake is not the listings - those come back from Facebook.
REM It is the group's votes and the post archive, which do not. The vote
REM data MIN_ALERT_SCORE is waiting on is n=3 after weeks of running.
REM
REM Daily at 21:30, after BGU Dashboard Share (21:00) and clear of every
REM scrape slot. backup() uses SQLite's online backup API so it is safe
REM mid-write anyway; the timing is belt and braces.
REM
REM Output is appended to data\backup.log so a job that silently stops
REM leaves a trail. doctor.py's `backups` row FAILs if the newest snapshot
REM is older than 48h - a backup you trust and do not have is worse than
REM no backup at all.
REM ===================================================================

cd /d "%~dp0"
set "PYTHONUTF8=1"
set PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe
if not exist "%PY%" set PY=python

echo ==== backup %DATE% %TIME% ====>> "data\backup.log"
"%PY%" -u backup_db.py >> "data\backup.log" 2>&1
