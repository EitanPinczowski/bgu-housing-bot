"""Post-age capture per scrape run, read from the archived rows.

    python .claude/tools/age_capture.py [--days 2]

WHY THIS EXISTS AS A COMMAND. On 2026-08-13 age capture moved 90% -> 37% -> 10% -> 68% ->
24% across five full runs, and every reading of it was retyped SQL. Two of the three
explanations offered that day were wrong, and one of them survived as long as it did
because the number being argued over was computed slightly differently each time.

WHAT IT MEASURES, AND WHY NOT THE RUN SUMMARY. The summary's `post age:` line counts every
post a run READ — including ones skipped as too old or already seen. This counts ARCHIVED
ROWS, which is the population the detection-lag metric actually uses. The two disagree by
a lot and answer different questions; when they conflict, this one is the honest per-post
rate. (A first version of the summary counter was worse still: it tallied once per SCROLL
PASS rather than once per post, and read 67/231 where the rows said 27/40.)

CLOCKS. `first_seen` is SQLite's CURRENT_TIMESTAMP, i.e. **UTC**, while the runs are named
by LOCAL time (UTC+3 here). Bucketing without the shift files a 14:00 run under 11:00 —
the same confusion that corrupted `posted_at` itself until it was fixed that morning.

READING IT. Only the full runs (08/10/14/16/18/20) carry a usable sample; a hot run reads
1-4 new posts and its percentage is noise, so they are marked. `impossible` counts rows
whose `posted_at` lands after `first_seen` — that is the clock bug, and it must stay 0.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402

_LOG = os.path.join(os.path.dirname(config.DB_PATH), "scraper_runs.log")


def _modes() -> dict[str, str]:
    """`{'08-13 14': 'LIVE'}` from the run log — which slots were FULL runs and which were
    hot passes. Read rather than inferred from row count: a hot run usually archives 1-4
    posts, but the 12:00 hot pass on 2026-08-13 archived 12 and a size threshold filed it
    with the full runs. The log states the mode, so ask it."""
    out: dict[str, str] = {}
    try:
        with open(_LOG, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                # 2026-08-13 14:39:39  END    LIVE  2372s  posts=130 …
                if len(parts) >= 4 and parts[2] in ("END", "START"):
                    out[f"{parts[0][5:]} {parts[1][:2]}"] = parts[3]
    except OSError:
        pass
    return out


def rows(days: int) -> list[sqlite3.Row]:
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    return list(con.execute(
        """SELECT strftime('%m-%d %H', datetime(first_seen, '+3 hours')) AS slot,
                  COUNT(*)                                   AS n,
                  SUM(posted_at IS NOT NULL)                 AS with_age,
                  SUM(posted_at IS NOT NULL
                      AND posted_at > first_seen)            AS impossible
             FROM posts
            WHERE first_seen > datetime('now', ?)
            GROUP BY slot ORDER BY slot""", (f"-{days} days",)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=2, help="how far back to look (default 2)")
    args = ap.parse_args()

    data = rows(args.days)
    if not data:
        print(f"no archived posts in the last {args.days} day(s)")
        return 0

    modes = _modes()
    print(f"post-age capture by run, last {args.days} day(s) — archived rows, local time")
    full_n = full_age = 0
    for r in data:
        pct = 100 * r["with_age"] / r["n"]
        mode = modes.get(r["slot"], "?")
        hot = "HOT" in mode
        note = "  (hot pass — its rate is noise)" if hot else \
               "  (mode unknown — not in the run log)" if mode == "?" else ""
        if not hot and mode != "?":
            full_n += r["n"]
            full_age += r["with_age"]
        bad = f"   ← {r['impossible']} IMPOSSIBLE (posted_at after first_seen)" \
            if r["impossible"] else ""
        print(f"  {r['slot']}:00  n={r['n']:4d}  age={r['with_age']:4d}  {pct:3.0f}%  "
              f"{'#' * round(pct / 5):20}{note}{bad}")
    if full_n:
        print(f"\n  full runs only: {full_age}/{full_n} = {100 * full_age / full_n:.0f}%")
    print("\n  A run's own summary prints `post age:` and `tooltips:` — those count every")
    print("  post READ, not just the archived ones. See the module docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
