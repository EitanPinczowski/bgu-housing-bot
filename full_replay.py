"""
Warm the geocode cache, then replay the whole archive — unattended, in one command.

    python full_replay.py              # preview only; writes nothing
    python full_replay.py --apply      # preview, then write verdicts + rebuild the Sheet
    python full_replay.py --skip-warm  # cache is already warm; go straight to the replay

WHY THIS EXISTS
---------------
A full `replay.py` re-classifies every archived post, and re-geocoding is almost all of
the cost. Measured 2026-08-12: of the archive's **2,686 distinct addresses, 521 still need
a network call**, at roughly 3 resolutions a minute — so warming is a ~2-3 hour job, while
the same 10,565 posts re-classify in **under 20 seconds** once it is done.

(Count what needs the NETWORK, not what is missing from `geocode_cache.json`: the first
estimate here was 2,148, four times too high, because `geocode_cached` also answers from
the static table, anchors, interpolation and street geometry.)

So the order matters: warm first (network-bound, resumable), replay second (fast).
`warm_cache.py --archive` is the warming step, and it is resumable because every success
is written to the geocode cache — if this dies, run it again and it continues.

THE TWO FAILURES THIS GUARDS AGAINST, both hit on 2026-08-12:
  * THE MACHINE SLEPT. A run stalled for 30 minutes and looked exactly like a hung
    network call — idle CPU, one open TCP connection. `scraper.start_keep_awake()` is the
    supported fix on S0 standby, but it deliberately does nothing ON BATTERY, so this
    refuses to start on battery instead of pretending to be protected.
  * THE OUTPUT WAS LOST. Redirecting a buffered run to a file left it empty for 30
    minutes with no way to see progress, and CLAUDE.md records that piping this command
    to `tail` cost two full previews. Everything here is line-buffered, timestamped, and
    written to a file AND the console.

Read-only unless you pass --apply. Preconditions are checked BEFORE any long work.
"""
from __future__ import annotations
import os
import subprocess
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import config  # noqa: E402
import scraper  # noqa: E402

_LOG = None


def say(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if _LOG:
        _LOG.write(line + "\n")
        _LOG.flush()


def preconditions(apply_mode: bool) -> list:
    """Everything that has to hold, checked before the hours-long part. Returns failures.

    Each of these has cost a run before, so none of them is theoretical."""
    bad = []
    if scraper.run_in_progress():
        bad.append("a scrape is running — it writes the same SQLite; wait for it to end")
    if scraper.on_ac_power() is not True:
        bad.append("on battery — keep-awake does nothing on battery (user's rule), so the "
                   "machine will sleep mid-run. Plug in and retry.")
    try:
        import requests
        r = requests.get(f"{config.OSRM_BASE_URL}/route/v1/foot/34.79,31.25;34.80,31.26",
                         params={"overview": "false"}, timeout=6)
        if not (r.ok and r.json().get("code") == "Ok"):
            bad.append("OSRM is not answering — the AMBER boundary IS a walk time, so a "
                       "replay without it bakes the straight-line estimate into every tier")
    except Exception as exc:
        bad.append(f"OSRM unreachable ({exc}) — start the container first")
    if apply_mode:
        nxt = _next_scrape_gap()
        if nxt:
            bad.append(nxt)
    return bad


def _next_scrape_gap():
    """--apply must not collide with a scheduled run. Full runs are hourly-ish through the
    day; the safe window is the evening after the last one."""
    h = datetime.now().hour
    if 7 <= h < 21:
        return ("--apply between 07:00 and 21:00 risks a scheduled run starting mid-write "
                "(they hold the same DB). Either run it after 21:00, or disable the "
                "'BGU Housing Scraper*' tasks first and re-enable them after.")
    return None


def run(cmd: list, label: str) -> int:
    say(f"--- {label}: {' '.join(cmd)}")
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", bufsize=1)
    for line in p.stdout:
        line = line.rstrip()
        print(line, flush=True)
        if _LOG:
            _LOG.write(line + "\n")
            _LOG.flush()
    p.wait()
    say(f"--- {label} finished in {time.time() - t0:,.0f}s (exit {p.returncode})")
    return p.returncode


def main() -> int:
    global _LOG
    apply_mode = "--apply" in sys.argv
    skip_warm = "--skip-warm" in sys.argv
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.DATA_DIR / f"full_replay_{stamp}.txt"
    _LOG = open(path, "w", encoding="utf-8")
    say(f"writing to {path}")
    say(f"mode: {'APPLY (writes verdicts + Sheet)' if apply_mode else 'preview (writes nothing)'}")

    bad = preconditions(apply_mode)
    if bad:
        say("REFUSING TO START:")
        for b in bad:
            say(f"  * {b}")
        return 2
    say("preconditions OK (no scrape, on mains, OSRM up)")

    stop_awake = scraper.start_keep_awake()
    t0 = time.time()
    try:
        if not skip_warm:
            say("STEP 1/2 — warming the geocode cache over the archive.")
            say("  Network-bound and resumable: if this dies, re-run the same command.")
            if run([sys.executable, "-u", "warm_cache.py", "--archive"], "warm_cache") != 0:
                say("warm_cache failed — stopping before the replay so the diff is not "
                    "measured against a half-warm cache")
                return 1
        else:
            say("STEP 1/2 — skipped (--skip-warm)")

        say("STEP 2/2 — replaying the archive (--frozen: cache + local tiers only).")
        say("  The warm above already asked the network about every address, so freezing")
        say("  here costs no coverage and makes the run reproducible.")
        cmd = ([sys.executable, "-u", "replay.py", "--frozen"]
               + (["--apply"] if apply_mode else []))
        rc = run(cmd, "replay --apply" if apply_mode else "replay (preview)")
    finally:
        stop_awake()
        say(f"TOTAL {time.time() - t0:,.0f}s — full output in {path}")
        _LOG.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
