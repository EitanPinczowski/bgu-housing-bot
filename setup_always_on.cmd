@echo off
REM ===================================================================
REM  Make the scheduled runs actually happen.  RUN AS ADMINISTRATOR.
REM  (right-click this file -> "Run as administrator")
REM
REM  THE PROBLEM THIS FIXES
REM  ----------------------
REM  Every "BGU *" scheduled task was created with "Wake the computer to
REM  run this task" OFF.  That means a run scheduled for, say, 10:00 while
REM  the PC is asleep is silently SKIPPED.  Task Scheduler reports no error
REM  and the only symptom is a quiet Telegram - which is exactly the
REM  "why didn't the last run run?" mystery.
REM
REM  Measured on this machine before the fix:
REM    - WakeToRun          = False on all 6 BGU tasks
REM    - sleep after (AC)   = never          <- fine while plugged in
REM    - sleep after (DC)   = 3 minutes      <- on battery it sleeps at once
REM    - wake timers (DC)   = disabled       <- and can't be woken back up
REM
REM  WHAT IT CHANGES (all reversible - see UNDO at the bottom)
REM    1. WakeToRun = True on every BGU task, so a due run wakes the PC.
REM    2. Wake timers enabled on battery as well as mains.
REM    3. Sleep on battery raised to 30 min, so a run that is already
REM       working isn't cut off mid-scrape.
REM
REM  These are Windows power/scheduler settings, not project settings, so
REM  this is deliberately a script YOU run rather than something the bot
REM  changes behind your back.  Nothing here touches Facebook, the scraper's
REM  volume, or any of the safety rules in CLAUDE.md.
REM ===================================================================
setlocal

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: not running as Administrator.
    echo   Right-click setup_always_on.cmd and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

echo.
echo === 1/3  Letting the BGU tasks wake the PC ===
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$n=0; Get-ScheduledTask | Where-Object {$_.TaskName -like 'BGU*'} | ForEach-Object { $s=$_.Settings; $s.WakeToRun=$true; $s.StartWhenAvailable=$true; Set-ScheduledTask -TaskName $_.TaskName -Settings $s | Out-Null; Write-Host ('  [ok] ' + $_.TaskName); $n++ }; Write-Host ('  ' + $n + ' task(s) updated')"

echo.
echo === 2/3  Allowing wake timers on battery as well as mains ===
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
echo   [ok] wake timers enabled (AC + battery)

echo.
echo === 3/3  Not falling asleep mid-run on battery ===
REM  AC is already "never sleep"; battery was 3 minutes, which cut runs short.
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 1800
echo   [ok] battery sleep timeout set to 30 minutes

powercfg /setactive SCHEME_CURRENT

echo.
echo === done ===
echo   Verify with:  python doctor.py      ("wake timers" should read PASS)
echo.
echo   UNDO:
echo     powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 180
echo     powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 0
echo     powercfg /setactive SCHEME_CURRENT
echo     (and untick "Wake the computer to run this task" in Task Scheduler)
echo.
pause
endlocal
