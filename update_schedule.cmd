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
echo === 1/4  Re-timing the full scrape to 6 runs, skipping the dead noon slot ===
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$t = @('08:00','10:00','14:00','16:00','18:00','20:00') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }; Set-ScheduledTask -TaskName 'BGU Housing Scraper' -Trigger $t | Out-Null; Write-Host '  [ok] BGU Housing Scraper -> 08:00 10:00 14:00 16:00 18:00 20:00'"

echo.
echo === 2/4  Creating the hot pass at the gaps in the afternoon peak ===
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%HERE%run_scraper_hot.cmd' -WorkingDirectory '%HERE%'; $t = @('12:00','15:00','17:00','19:00') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries; Register-ScheduledTask -TaskName 'BGU Housing Scraper Hot' -Action $a -Trigger $t -Settings $s -Force | Out-Null; Write-Host '  [ok] BGU Housing Scraper Hot -> 12:00 15:00 17:00 19:00'"

echo.
echo === 3/4  Daily dashboard snapshot to the Telegram group ===
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%HERE%run_dashboard_share.cmd' -WorkingDirectory '%HERE%'; $t = New-ScheduledTaskTrigger -Daily -At '21:00'; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries; Register-ScheduledTask -TaskName 'BGU Dashboard Share' -Action $a -Trigger $t -Settings $s -Force | Out-Null; Write-Host '  [ok] BGU Dashboard Share -> 21:00 daily (after the last scrape)'"

echo.
echo === 4/4  Refresh the public copy hourly (no Telegram post) ===
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%HERE%run_dashboard_publish.cmd' -WorkingDirectory '%HERE%'; $t = @('08:30','09:30','10:30','11:30','12:30','13:30','14:30','15:30','16:30','17:30','18:30','19:30','20:30') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries; Register-ScheduledTask -TaskName 'BGU Dashboard Publish' -Action $a -Trigger $t -Settings $s -Force | Out-Null; Write-Host '  [ok] BGU Dashboard Publish -> hourly :30, 08:30-20:30 (21:00 Share is the last)'"

echo.
echo === 5/5  Nightly DB snapshot (the votes and the archive do not come back) ===
REM backup_db.py said "Schedule it weekly in Task Scheduler" from the day it was
REM written and never was: on 2026-08-09 nine BGU * tasks existed and none was this,
REM and all 14 snapshots on disk had been made by hand. Daily, not weekly, so KEEP=14
REM means a fortnight of history. 21:30 sits after Dashboard Share (21:00) and clear
REM of every scrape slot.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%HERE%run_backup.cmd' -WorkingDirectory '%HERE%'; $t = New-ScheduledTaskTrigger -Daily -At '21:30'; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries; Register-ScheduledTask -TaskName 'BGU Backup' -Action $a -Trigger $t -Settings $s -Force | Out-Null; Write-Host '  [ok] BGU Backup -> 21:30 daily (doctor FAILs if the newest is >48h old)'"

echo.
echo === done ===
echo   Check with:   python doctor.py        (wake timers and backups should PASS)
echo   Measure with: python stats.py         ("time to detect" should fall
echo                                          from its 8.4h / n=44 baseline)
echo.
echo   UNDO:
echo     schtasks /Delete /TN "BGU Housing Scraper Hot" /F
echo     schtasks /Delete /TN "BGU Dashboard Share" /F
echo     schtasks /Delete /TN "BGU Dashboard Publish" /F
echo     schtasks /Delete /TN "BGU Backup" /F
echo     ...and restore the 7 old triggers (08 10 12 14 16 18 20) in Task Scheduler.
echo.
pause
endlocal
