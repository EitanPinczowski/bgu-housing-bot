"""
Scheduled dependency health-check — a thin wrapper around `doctor` that DM-alerts on a
hard failure. Kept as a separate entry point so the existing Task Scheduler job doesn't
change; the actual checks now live in doctor.py (run `python doctor.py` for the full,
human-readable status table).

    python watchdog.py        # == python doctor.py --alert

Schedule it a bit before each scrape so you fix a down dependency BEFORE a run silently
degrades. Headless (no browser). Facebook-login loss is caught by the scraper itself.
"""
from __future__ import annotations

import doctor

if __name__ == "__main__":
    # --fix first (auto-start a down OSRM container), then --alert on whatever's still broken
    raise SystemExit(doctor.main(["--fix", "--alert"]))
