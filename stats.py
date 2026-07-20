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
import sqlite3
import sys

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


if __name__ == "__main__":
    main()
