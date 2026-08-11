#!/usr/bin/env python
"""What actually moved between two `geo_dump.py` runs.

A percentile table can tell you the tail got worse. It cannot tell you that the whole of a
715 m max is ONE address, that five of the twelve worst errors are unchanged and therefore
nothing to do with the change you just made, or that 22 addresses got better while 21 got
worse. All three were true on 2026-08-11 and none were visible until this existed.

    python .claude/tools/geo_diff.py data/geo_before.csv data/geo_after.csv

Prints what changed in each direction, then the worst absolute errors AFTER the change —
which is the list that says whether the tail is your problem or an older one.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys

CHANGED_M = 1.0            # ignore sub-metre float wobble


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return {r["address"]: r for r in csv.DictReader(fh)}


def _err(row: dict):
    """Metres, or None for an address with no dot on the map."""
    if not row or row["tier"] == "UNPLACED" or not row["error_m"]:
        return None
    return float(row["error_m"])


def _fmt(v) -> str:
    return "UNPLACED" if v is None else f"{v:.0f}m"


def _stats(label: str, table: dict) -> None:
    errs = sorted(e for e in (_err(r) for r in table.values()) if e is not None)
    if not errs:
        print(f"{label:8} nothing placed")
        return
    p90 = errs[min(len(errs) - 1, int(len(errs) * 0.9))]
    unplaced = sum(1 for r in table.values() if _err(r) is None)
    print(f"{label:8} n={len(errs):3}  p50={statistics.median(errs):4.0f}m  "
          f"p90={p90:4.0f}m  max={errs[-1]:4.0f}m  unplaced={unplaced}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    before, after = _load(args.before), _load(args.after)

    moved = []
    for addr, rb in before.items():
        ra = after.get(addr)
        if ra is None:
            continue
        b, a = _err(rb), _err(ra)
        if b is None and a is None:
            continue
        if b is None or a is None or abs(a - b) >= CHANGED_M:
            moved.append((addr, b, a, rb["tier"], ra["tier"], ra["source"]))

    # UNPLACED counts as infinitely bad in both directions: losing a dot is a regression
    # even from 400 m, and gaining one is an improvement even at 400 m.
    worse = [m for m in moved if m[1] is not None and (m[2] is None or m[2] > m[1])]
    better = [m for m in moved if m[2] is not None and (m[1] is None or m[2] < m[1])]
    print(f"{len(moved)} addresses changed ({len(better)} better, {len(worse)} worse)\n")

    for title, rows in (("WORSE", worse), ("BETTER", better)):
        rows = sorted(rows, key=lambda m: -abs((m[2] if m[2] is not None else 1e9)
                                               - (m[1] if m[1] is not None else 0)))
        print(f"===== {title} ({len(rows)}) — largest change first")
        if rows:
            print(f"  {'address':30} {'before':>9} {'after':>9}  {'tier':14} source")
        for addr, b, a, tb, ta, src in rows[:args.top]:
            tier = f"{tb}->{ta}" if tb != ta else tb
            print(f"  {addr:30} {_fmt(b):>9} {_fmt(a):>9}  {tier:14} {src or '-'}")
        if len(rows) > args.top:
            print(f"  ... and {len(rows) - args.top} more")
        print()

    # THE TAIL IS OFTEN NOT YOUR CHANGE. Five of the twelve worst on 2026-08-11 were
    # identical before and after, so any effort spent on them would have been misdirected.
    print(f"===== WORST {args.top} AFTER — 'before' identical means it is not your change")
    placed = [r for r in after.values() if _err(r) is not None]
    print(f"  {'address':30} {'after':>9} {'before':>9}  source")
    for r in sorted(placed, key=lambda r: -_err(r))[:args.top]:
        print(f"  {r['address']:30} {_fmt(_err(r)):>9} "
              f"{_fmt(_err(before.get(r['address']))):>9}  {r['source']}")
    print()
    _stats("before", before)
    _stats("after", after)
    return 0


if __name__ == "__main__":
    sys.exit(main())
