"""
Funnel stats from the local post archive — what the filters are actually doing.
No browser, no network.

    python stats.py

Shows how many archived posts (those that reached the LLM) landed in each verdict
(MATCH / NEEDS_DATA / DROP / NOT_AD), WHY posts were dropped, and store totals.
The archive fills as the scraper runs; see replay.py to re-test against it.
"""
from __future__ import annotations
import os
import re
import sqlite3
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
try:
    sys.stdout.reconfigure(encoding="utf-8")   # Hebrew reasons/addresses
except Exception:
    pass

import config
import storage


def main() -> None:
    vc = storage.verdict_counts()
    total = sum(vc.values())
    print(f"=== archive: {total} posts (reached the LLM) ===")
    for v in ("MATCH", "NEEDS_DATA", "DROP", "NOT_AD"):
        if vc.get(v):
            pct = round(100 * vc[v] / total) if total else 0
            print(f"  {v:11} {vc[v]:4}  ({pct}%)")

    drops = storage.drop_reason_counts()
    if drops:
        print("--- why dropped ---")
        for reason, c in drops:
            print(f"  {c:4}  {reason}")

    with sqlite3.connect(config.DB_PATH) as con:
        listings = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        matches = con.execute("SELECT COUNT(*) FROM listings WHERE status='MATCH'").fetchone()[0]
        votes = con.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
    print("--- store ---")
    print(f"  listings: {listings} ({matches} MATCH)   votes: {votes}")

    gy = storage.group_yield()
    if gy:
        print("--- per-group yield (match | needs | drop | total) — drop dead groups ---")
        for g, tot, m, n, d, _na in gy:
            gid = g.rstrip("/").split("/")[-1].split("?")[0]
            flag = "   ← 0 matches, candidate to drop from FB_GROUPS" if m == 0 else ""
            print(f"  {gid:>18}   {m:>3} | {n:>3} | {d:>3} | {tot:>3}{flag}")

    uk = storage.unknown_locations(days=3650)
    if uk:
        print("--- top unmapped locations (pin these) ---")
        for loc, cnt, _ in uk[:8]:
            print(f"  {cnt:3}  {loc}")

    _detection_lag()
    _run_reliability()


def _detection_lag() -> None:
    """How long a listing sits on Facebook before we see it.

    The `--hot` pass exists to cut this to ~30-40 min and that had never actually been
    measured. Every figure is printed WITH its n: the first attempt at this had n=6,
    which is worth nothing, and a lag number quoted without its sample size is how you
    end up believing a feature works."""
    with sqlite3.connect(config.DB_PATH) as con:
        rows = con.execute("SELECT posted_at, first_seen FROM posts "
                           "WHERE posted_at IS NOT NULL AND first_seen IS NOT NULL").fetchall()
    lags = []
    for posted, seen in rows:
        try:
            d = (datetime.strptime(seen, "%Y-%m-%d %H:%M:%S")
                 - datetime.strptime(posted, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
        except (TypeError, ValueError):
            continue
        if 0 <= d < 60 * 48:                 # ignore clock skew and absurd outliers
            lags.append(d)
    print("--- time to detect (post published -> we saw it) ---")
    if len(lags) < 5:
        print(f"  n={len(lags)} — too few to mean anything yet")
        return
    lags.sort()
    p = lambda q: lags[int(q * (len(lags) - 1))]        # noqa: E731 - local helper
    print(f"  n={len(lags)}   median {p(.5):.0f} min   p25 {p(.25):.0f}   "
          f"p75 {p(.75):.0f}   worst {lags[-1]:.0f}")
    print(f"  within 1h: {sum(1 for x in lags if x <= 60)}   "
          f"1-3h: {sum(1 for x in lags if 60 < x <= 180)}   "
          f"over 3h: {sum(1 for x in lags if x > 180)}")


def _run_reliability() -> None:
    """Completed scrapes per day against the target. Measured 2026-07-30 at ~5 of 7
    (a 28% loss) — sleeping through a scheduled slot is invisible otherwise, which is
    exactly the failure setup_always_on.cmd addresses."""
    path = config.DATA_DIR / "search_log.txt"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    per_day: dict = {}
    for line in lines:
        m = re.match(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\s+(END|SKIP)\b", line)
        if m:
            per_day.setdefault(m.group(1), 0)
            per_day[m.group(1)] += 1
    if not per_day:
        return
    days = sorted(per_day)[-7:]
    target = config.SCRAPER_RUNS_PER_DAY
    done = sum(per_day[d] for d in days)
    want = target * len(days)
    print(f"--- run reliability (last {len(days)} logged days, target {target}/day) ---")
    print("  " + "  ".join(f"{d[5:]}:{per_day[d]}" for d in days))
    pct = round(100 * done / want) if want else 0
    print(f"  {done}/{want} runs = {pct}%" +
          ("" if pct >= 90 else "   ← missed runs; check `python doctor.py` wake timers"))


if __name__ == "__main__":
    main()
