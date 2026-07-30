@echo off
REM ===================================================================
REM  Re-time the scrapes around when listings are ACTUALLY posted, and
REM  finally schedule the --hot pass.  RUN AS ADMINISTRATOR.
REM
REM  WHY
REM  ---
REM  Measured on the archive 2026-07-30:
REM    * of 63 timed posts, 45 land between 14:00 and 20:00; 11:00-13:00
REM      is nearly dead - yet runs were spaced evenly 08/10/12/14/16/18/20,
REM      giving the busiest hours the same 2-hour lag as the empty ones.
REM    * median time-to-detect was 8.4 HOURS (n=44); only 7 of 44 listings
REM      were seen within an hour of being posted.
REM    * `main.py --hot` - built specifically to cut that lag, and counted
REM      in CLAUDE.md's volume budget - had never been scheduled at all.
REM
REM  WHAT CHANGES
REM  ------------
REM    Full runs (~251 page-reads each): 6/day, was 7. The dead 12:00 slot
REM      goes.                08:00 10:00 14:00 16:00 18:00 20:00
REM    Hot runs (~30 reads each): 4/day, new.
REM                              12:00 15:00 17:00 19:00
REM
REM  Between 14:00 and 20:00 something now runs EVERY HOUR (alternating
REM  full and hot) instead of every two hours.
REM
REM  VOLUME GOES DOWN, NOT UP
REM  ------------------------
REM    before: 7 x 251                  = 1757 page-reads/day
REM    after:  6 x 251  +  4 x 30       = 1626 page-reads/day   (-7.5%)
REM  Trading one expensive full run in an empty window for four cheap
REM  passes across the peak. No change to group membership, delays,
REM  jitter, the daytime-only rule, or the read-only posture.
REM ===================================================================
setlocal

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: not running as Administrator.
    echo   Right-click update_schedule.cmd and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

set "HERE=%~dp0"

echo.
echo === 1/2  Re-timing the full scrape to 6 runs, skipping the dead noon slot ===
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$t = @('08:00','10:00','14:00','16:00','18:00','20:00') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }; Set-ScheduledTask -TaskName 'BGU Housing Scraper' -Trigger $t | Out-Null; Write-Host '  [ok] BGU Housing Scraper -> 08:00 10:00 14:00 16:00 18:00 20:00'"

echo.
echo === 2/2  Creating the hot pass at the gaps in the afternoon peak ===
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%HERE%run_scraper_hot.cmd' -WorkingDirectory '%HERE%'; $t = @('12:00','15:00','17:00','19:00') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries; Register-ScheduledTask -TaskName 'BGU Housing Scraper Hot' -Action $a -Trigger $t -Settings $s -Force | Out-Null; Write-Host '  [ok] BGU Housing Scraper Hot -> 12:00 15:00 17:00 19:00'"

echo.
echo === done ===
echo   Check with:   python doctor.py        (wake timers should PASS)
echo   Measure with: python stats.py         ("time to detect" should fall
echo                                          from its 8.4h / n=44 baseline)
echo.
echo   UNDO:
echo     schtasks /Delete /TN "BGU Housing Scraper Hot" /F
echo     ...and restore the 7 old triggers (08 10 12 14 16 18 20) in Task Scheduler.
echo.
pause
endlocal
