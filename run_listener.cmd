@echo off
REM Supervised launcher for the Telegram listener.
REM
REM bot_listener.py long-polls Telegram for vote taps and DM commands. It loads its code
REM once at startup, so it must be RESTARTED after any code change — and if it ever exits
REM (network wobble, crash, Windows update), the votes and /commands silently stop working.
REM This wrapper relaunches it automatically.
REM
REM Use this instead of pointing the Startup shortcut straight at pythonw bot_listener.py:
REM   shell:startup  ->  shortcut to this file (Run: minimized)
REM
REM Stop it by closing this window (or ending the python process).

cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe
if not exist "%PY%" set PY=python

:loop
echo [%DATE% %TIME%] starting bot_listener...
"%PY%" bot_listener.py
echo [%DATE% %TIME%] bot_listener exited (code %ERRORLEVEL%) - restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
