@echo off
REM ===================================================================
REM Build the dated dashboard snapshot and post it to the Telegram GROUP.
REM
REM One self-contained HTML file (~800 KB, far inside Telegram's 50 MB
REM document limit): the map, every listing, contacts and WhatsApp links.
REM No account, no install, opens on a phone straight from the chat.
REM
REM It is a SNAPSHOT: write buttons are removed and a dated banner says so,
REM so a three-day-old copy is never mistaken for live data. For live data
REM and shared voting, use the server (run_dashboard.cmd) over Tailscale.
REM
REM The file carries landlords' phone numbers and addresses. It goes to the
REM group only ('group' target in notifier.send_document) - the same place
REM the alerts already go.
REM
REM Scheduled daily by update_schedule.cmd as "BGU Dashboard Share".
REM ===================================================================

cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe
if not exist "%PY%" set PY=python

REM --publish also pushes it to the always-on public URL (GitHub Pages), which is
REM the only copy that survives this PC being switched off. Needs SITE_REPO_URL in
REM .env; without it that step prints one line and does nothing.
"%PY%" -u dashboard.py --share --publish --send
