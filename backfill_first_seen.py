"""
Restore each listing's real discovery date from the post archive.

    python backfill_first_seen.py            # show what would move, then move it
    python backfill_first_seen.py --dry-run  # show only

Why this exists: `replay --apply` used to write listings with `INSERT OR REPLACE`,
which resets `first_seen` to CURRENT_TIMESTAMP every time. After a few replays the
entire table read as "found today", which silently disables three things that depend
on a listing's age — `config.LISTING_STALE_DAYS`, the `/top` time windows, and the
freshness factor in `fit`. The upsert no longer resets it (see storage.save_listing),
but the rows already in the DB are wrong.

`posts.first_seen` was never clobbered — `record_post` preserves it on conflict — so
the archive is a trustworthy source. Dates only ever move BACKWARDS, which makes a
second run a no-op and means the repair can't make anything look newer than it is.

Safe to re-run. Read-only except for the one UPDATE.
"""
from __future__ import annotations
import collections
import sqlite3
import sys
from datetime import datetime

import config
import storage


def _age_histogram(label: str) -> None:
    with sqlite3.connect(config.DB_PATH) as c:
        rows = [r[0] for r in c.execute("SELECT first_seen FROM listings") if r[0]]
    buckets = collections.Counter()
    now = datetime.now()
    for stamp in rows:
        try:
            days = (now - datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")).days
        except (TypeError, ValueError):
            continue
        buckets["today" if days < 1 else
                "1-2 days" if days < 3 else
                "3-7 days" if days < 8 else
                f"8-{config.LISTING_STALE_DAYS} days" if days < config.LISTING_STALE_DAYS
                else "stale"] += 1
    print(f"  {label}: " + ", ".join(f"{k}={v}" for k, v in buckets.most_common()))


def main() -> int:
    print("listing ages before:")
    _age_histogram("before")

    if "--dry-run" in sys.argv:
        print("\n--dry-run: nothing written")
        return 0

    moved = storage.backfill_first_seen()
    print(f"\nmoved {moved} listing(s) back to their archived discovery date")
    _age_histogram("after ")
    print("\nre-run is a no-op (dates only ever move backwards).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
