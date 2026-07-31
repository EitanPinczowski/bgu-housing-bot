@echo off
REM ===================================================================
REM Refresh the always-on PUBLIC copy of the dashboard, without posting
REM anything to Telegram.
REM
REM This is the copy that keeps working when this PC is off: a dated,
REM self-contained snapshot on GitHub Pages. It is as fresh as the last
REM run, never live - the scraper and OSRM both need this machine.
REM
REM Scheduled at 09:00 / 13:00 / 17:00 by update_schedule.cmd. The 21:00
REM slot uses run_dashboard_share.cmd instead, which also posts the file
REM to the Telegram group - you don't want four of those a day.
REM
REM Needs SITE_REPO_URL in .env. Without it this prints one line and exits.
REM ===================================================================

cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe
if not exist "%PY%" set PY=python

"%PY%" -u dashboard.py --share --publish
