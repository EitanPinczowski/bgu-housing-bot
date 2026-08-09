---
name: scraper-notes
description: >
  Reference notes on the Facebook reader and orchestrator: the single-instance lock,
  wedged and crashed runs, the self-watchdog, browser launch retries, orphan reaping,
  keep-awake, and the scheduled tasks. Load before editing scraper.py, main.py,
  login.py, watchdog.py, or the run_*.cmd scripts.
---

# Scraper Notes

How a run is started, kept alive, and stopped. To DIAGNOSE a lost run use `health-triage`; to change volume or cadence use `scraper-volume`.

> Moved verbatim from `CLAUDE.md`. Do not reword in place — see the
> `write-a-note` skill.

- `scraper.py` / `login.py` / `main.py` — Playwright reader, one-time login, orchestrator.
- `manual.py` — paste-a-post CLI (risk-free entry point).

- **A SLEEPING PC wedges it a third way** (2026-08-05). The 00:46 hot run started, the
  machine slept, and it did not finish until 09:15 — 8.5 h wall clock for ~23 min of
  work (Windows logged the wake at 08:52). It held the lock throughout, so the 08:58 full
  run logged SKIP. `setup_always_on.cmd` sets "wake the computer" on the scheduled TASKS,
  which wakes the PC to START a run; nothing kept it awake DURING one.
  `scraper.start_keep_awake()` holds `ES_SYSTEM_REQUIRED` for the life of a run, and
  `main.run` releases it next to the lock.
  - **ONLY ON MAINS** (user's rule): it polls `on_ac_power()` rather than setting the
    flag once, because the cable can come out mid-run. On battery — and when the power
    state is UNKNOWN — it holds nothing; an unanswered question must not pin the machine
    awake.
- **THE "STARTED AND NEVER FINISHED" RUNS ARE A FAILED BROWSER LAUNCH** (2026-08-05).
  Every traceback in `scraper_runs.log` is the same call, `open_browser()` →
  `launch_persistent_context`, and the run dies before reading a post: no `END`, nothing
  downstream to complain, the slot simply gone. Nine in the 7 days to 08-05. Two causes,
  both transient: *"the profile is already in use by another instance of Chromium"* (a
  leftover Chromium on `auth/chrome_profile`) and *"Timeout 180000ms exceeded"*.
  - `open_browser()` now retries `BROWSER_LAUNCH_RETRIES` times, calling
    `reap_orphan_browsers()` between attempts — which was already written and documented
    as exactly the cure, and was just never called BEFORE a launch, only after a wedge.
  - Reaping is safe here for the reason its own docstring gives: `main.py` calls this
    **while holding the lock**, so anything still driving the profile is a leftover.
  - **Each failed attempt stops its playwright handle first.** Otherwise every retry
    leaks a driver process — which is how orphans accumulate, so getting that wrong
    would feed the very problem this fixes.
- **A CRASHED run wedges the day differently from a HUNG one** (2026-08-04). The
  self-watchdog aborts a run that stops making PROGRESS; it does nothing for a run that
  finished scraping and then died in CLEANUP. Playwright's node subprocess went down with
  `EPIPE` at group 11/15, `context.close()` never returned, and the python process sat
  alive holding the lock — which is an OS file lock, so only that process exiting frees
  it. The 17:00 hot pass and the 00:46 full run both logged "another scraper session is
  running", and the 00:46 launcher then found the holder unkillable and gave up.
  `main._bounded_teardown` gives `context.close()` and `p.stop()` a thread and a 30 s
  deadline each, so `release_lock()` is always reached. **A hang is not catchable** — a
  bare try/except around close() would have sailed straight into the same permanent wait.
  Abandoning a half-closed browser is the cheap side of the trade: `reap_orphan_browsers`
  clears it next run, while a held lock costs every scheduled run until someone notices.
- **A CRAWLING RUN IS NOT A HUNG ONE, AND THE STALL TEST CANNOT SEE IT** (user, 2026-08-05).
  `MAX_RUN_MINUTES` (120) is a hard WALL-CLOCK ceiling in the same watchdog thread. The
  18:00 run that day was 90 minutes in, still on group 1 of 15 at ~2 min/post with a fresh
  heartbeat throughout — healthy by every existing measure, holding the lock against every
  later slot, and it managed 5 of 15 groups in 88 minutes.
  - **It reverses the decision recorded right above it in `config.py`** ("judged by
    PROGRESS, not elapsed time", because legitimate local-Ollama runs reached 99/195/268
    minutes). Safe now only because of two later mechanisms: the fallback cap (40 posts)
    ends those grinds, and a transient 429/503 no longer latches a run onto Ollama at all.
    **If either is ever weakened, raise or remove this ceiling too.**
  - `validate()` refuses a ceiling at or below `STALL_MINUTES` — it would fire first every
    time, making the stall test dead code and killing healthy runs.
  - **Wall clock, not monotonic**, on purpose: the 00:46 run that slept through the night
    took 8.5 h for ~23 min of work, and that is exactly what this must catch.
  - The abort appends an `ABORT` line to `search_log.txt`, because `os._exit` skips
    main's `END` line and the run would otherwise be indistinguishable from a silent
    crash. `stats.py` counts aborts separately and does not also call them "started and
    never finished".
  - **`_abort` writes through `config.DATA_DIR`** — a test that does not patch it appends
    fake aborts to the real operational log. That happened; the fixture patches it now.
- **A hung run must not block the day.** `scraper.start_self_watchdog()` (started by
  `main.py` right after the lock) aborts a run that makes no progress for
  `STALL_MINUTES`. Before it existed, `is_wedged()` was only consulted by the NEXT run,
  so a run that hung at group 4 of 15 held the lock for **six hours** and every
  scheduled run logged "another scraper session is running" — 3 starts, 0 completions
  in a day. Recovery has three parts, all needed:
  - `_kill()` does **not** use `taskkill /T` (walking a Chromium tree blew past the
    30 s budget, and the timeout was treated as total failure) and judges success by
    whether the pid is actually gone.
  - `reap_orphan_browsers()` closes browsers a dead run left behind. **Scoped by the
    profile path on the command line, never by process name** — most `chrome.exe` on
    this machine is the user's own browser (36 of 39 when measured).
  - Windows can leave a process `TerminateProcess` accepts but never reaps; those are
    unkillable until a reboot. Measured: the profile still opens with them present, so
    the reaper says so and continues rather than refusing to run.
  - **A STALE HEARTBEAT ONLY MEANS SOMETHING WHILE A RUN IS LIVE.** The file is never
    cleared on exit, so between scheduled runs its age just keeps growing. `is_wedged()`
    is safe because `_clear_wedged_holder` calls it only when a live process HOLDS the
    lock; `doctor`'s `scraper progress` row had no such guard and FAILed on an idle
    machine — 2026-08-03 13:30, "no progress for 31 min", the 08:00 run finished cleanly
    at 13:11, no `main.py` process anywhere, and the same report's `last run` row said
    PASS 0.5h ago. It now consults `scraper.run_in_progress()`, which matches the
    heartbeat's pid against **that pid's own command line** (`main.py`) — never a
    process name, same rule as `reap_orphan_browsers`, because `python.exe` says nothing
    about whose script it is. "Couldn't ask the OS" returns None and reports WARN: a
    failed query is not evidence of a hang.
- `setup_always_on.cmd` — run ONCE as Administrator. The `BGU *` tasks ship with
  "wake the computer" OFF, so a run due while the PC sleeps is silently skipped —
  the real cause of "why didn't it run". Also fixes battery wake timers/sleep.
  `doctor`'s `wake timers` row makes the failure visible.
