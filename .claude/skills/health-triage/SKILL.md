---
name: health-triage
description: >
  Diagnose why scheduled runs are not producing listings. Use for "the scraper didn't
  run", "no alerts today", "doctor is failing", "another scraper session is running", a
  wedged lock, a run that started and never ended, or when checking scheduling and
  reliability with doctor.py / stats.py.
---

# Triaging a lost run

    python doctor.py

Every dependency with remediation attached. Start here — three separate outages in
2026-08-04/05 were found only because somebody happened to run it. `doctor`'s **FAILs
now ride the DM digest** (`dm_digest._health_section`) so silence means healthy rather
than unobserved.

## A run can be lost FOUR different ways, and they need different fixes

`stats.py` separates them. Do not treat them as one problem.

| symptom in `scraper_runs.log` | what happened | fix |
|---|---|---|
| `SKIP another scraper session is running` | the lock is held | find the holder — see below |
| `START` with no `END`, process gone | a **crash** | releases the lock; next slot runs normally, so nothing downstream complains. 7 in the 7 days to 08-05 |
| `START`, no `END`, process **alive** | a **hang or a crawl** | the self-watchdog should abort it |
| `ABORT` | the watchdog did its job | read why: stalled, or past `MAX_RUN_MINUTES` |

**A SKIP is a run that did not happen.** `stats.py`'s reliability row used to count
`END|SKIP` together and so read healthiest exactly when runs were being lost — 08-03
reported 11 runs / **119%** of target while 5 were lock-held and 4 actually ran. The
~1-in-8 designed `random human-like skip` is counted apart, because flagging it trains
you to ignore the row.

## Is a run actually running right now?

    python -c "import scraper; print(scraper.run_in_progress())"

`True` / `False` / `None` ("couldn't ask the OS" — not evidence of a hang).

**A stale heartbeat only means something while a run is live.** The file is never cleared
on exit, so between scheduled runs its age just keeps growing. `doctor`'s
`scraper progress` row once FAILed with "no progress for 31 min" on a completely idle
machine while the same report's `last run` row said PASS.

## Clearing a wedged lock

The lock is an **OS file lock**, so only the holding process exiting frees it.

1. `python -c "import scraper; print(scraper.heartbeat_pid())"`
2. Confirm that pid's command line really is `main.py` — never match on process name;
   `python.exe` says nothing about whose script it is.
3. `scraper.reap_orphan_browsers()` clears browsers a dead run left behind. **Scoped by
   the profile path on the command line**, because most `chrome.exe` on this machine is
   the user's own browser (36 of 39 when measured).
4. Windows can leave a process `TerminateProcess` accepts but never reaps. Those are
   unkillable until a reboot — but measured, **the profile still opens with them
   present**, so this is a warning, not a blocker.

## The known root causes

- **A failed browser launch.** Every traceback in `scraper_runs.log` is the same call,
  `open_browser()` → `launch_persistent_context`, dying before a post is read: no `END`,
  nothing downstream to complain, the slot simply gone. Nine in 7 days. Two transient
  causes — a leftover Chromium on `auth/chrome_profile`, and `Timeout 180000ms exceeded`.
  Now retried `BROWSER_LAUNCH_RETRIES` times with a reap between attempts.
- **A crash in CLEANUP.** Playwright's node subprocess went down with `EPIPE`,
  `context.close()` never returned, and the python process sat alive holding the lock.
  `main._bounded_teardown` gives close() and stop() 30 s each. **A hang is not
  catchable** — a bare try/except would sail into the same permanent wait.
- **A sleeping PC — and until 2026-08-12 the guard against it did nothing here.** The
  00:46 run took 8.5 h for ~23 min of work. `setup_always_on.cmd` wakes the PC to START a
  run; `scraper.start_keep_awake()` holds it awake **during** one — on mains only, and not
  when the power state is unknown.

  **`SetThreadExecutionState` does not hold off MODERN STANDBY, which is all this machine
  has.** `powercfg /a` reports only `Standby (S0 Low Power Idle) Network Connected`, with
  S1/S2/S3 "disabled when S0 low power idle is supported". The legacy flag was written for
  S3, so the guard read as working while the machine idled into standby anyway — and it
  leaves no trace in `powercfg /requests`, so nothing looked wrong.

  How it was found: the two multi-hour runs each began within seconds of a Kernel-Power
  SLEEP event (start 00:46:19 vs SLEEP 00:46:23; 14:27:46 vs SLEEP 14:27:43). The
  sleep→wake pairs are 4–20 s apart, which is the S0 signature — the system reports itself
  awake while background processes are throttled to nothing.

      Get-WinEvent -FilterHashtable @{LogName='System'
        ProviderName='Microsoft-Windows-Kernel-Power'} | Where-Object Id -in 42,107

  **The watchdog is throttled with everything else**, which is why it aborted at 388 and
  438 min against a 120-minute ceiling. An abort that late is evidence of standby, not of
  a broken watchdog.

  Now asserts `PowerRequestExecutionRequired` via `PowerCreateRequest`/`PowerSetRequest`,
  which is the supported mechanism on S0, *and* the legacy flag for any S3 machine. Verify
  with an ELEVATED `powercfg /requests` during a run — it should list the process under
  **EXECUTION** with `BGU housing scraper: reading Facebook groups`. If EXECUTION is empty
  while a run is live, the guard is not working.
- **A crawl, not a hang.** A run on Ollama at ~2 min/post has a fresh heartbeat and is
  healthy by every progress measure while holding the lock all day. `MAX_RUN_MINUTES`
  (120) is a wall-clock ceiling for exactly this.

## Before concluding "the schedule is wrong"

**The lag is lost runs, not cadence.** Only 20 of 42 scheduled full runs completed in the
7 days to 08-05, with 17 slots lost to a held lock — and the three lock repairs landed
*after* almost all of that data. Re-measure over clean days before touching the schedule.
See `scraper-volume` before changing cadence.

    python stats.py            # funnel, reliability, time-to-detect, all with their n
    python group_report.py     # per-group yield
