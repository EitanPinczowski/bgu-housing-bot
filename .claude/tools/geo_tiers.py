#!/usr/bin/env python
"""Which anchor set puts flats in the WRONG ZONE — the thing the metres are a proxy for.

p50 and p90 are not the product. The product is GREEN / AMBER / RED: whether a flat is
inside the hand-drawn zone, within a 20-minute walk of a gate, or dropped. Two anchor sets
can trade percentiles and still be indistinguishable where it counts, or agree on
percentiles and disagree on dozens of verdicts.

Measured 2026-08-11: seeding moved p50 17m -> 13m and p90 79m -> 102m, which says nothing
about whether a single listing changed tier. This does.

    python .claude/tools/geo_dump.py --local-only --anchors A.json --out a.csv
    python .claude/tools/geo_dump.py --local-only --anchors B.json --out b.csv
    python .claude/tools/geo_tiers.py a.csv b.csv

Needs OSRM up for the AMBER walk time, exactly as the live pipeline does — with it down
every walk falls back to the straight-line estimate and the boundary moves.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.environ.get(
    "CLAUDE_PROJECT_DIR", str(Path(__file__).resolve().parents[2])))

import osrm                                        # noqa: E402
import zones                                       # noqa: E402


def _rows(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return {r["address"]: r for r in csv.DictReader(fh)}


def _tier(lat: str, lon: str):
    if not lat or not lon:
        return None
    return zones.classify_effective(float(lat), float(lon))[0]


def _report(label: str, table: dict) -> Counter:
    """How often this anchor set's placement lands in a different tier than the truth."""
    tally = Counter()
    for row in table.values():
        truth = _tier(row["truth_lat"], row["truth_lon"])
        got = _tier(row["got_lat"], row["got_lon"])
        if truth is None:
            continue
        if got is None:
            tally["unplaced"] += 1
        elif got == truth:
            tally["agree"] += 1
        else:
            tally["WRONG"] += 1
            tally[f"  {truth} -> {got}"] += 1
    total = tally["agree"] + tally["WRONG"] + tally["unplaced"]
    print(f"\n=== {label}  (n={total})")
    print(f"  same tier as truth : {tally['agree']}")
    print(f"  WRONG tier         : {tally['WRONG']}")
    print(f"  never placed       : {tally['unplaced']}")
    for k, v in sorted(tally.items()):
        if k.startswith("  "):
            print(f"    {k.strip():18} {v}")
    return tally


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    args = ap.parse_args()

    if not osrm.alive():
        print("WARNING: OSRM is DOWN — the AMBER boundary IS a walk time, so every tier "
              "below is computed from the straight-line estimate and this comparison is "
              "not the one the live pipeline would make.\n")

    A, B = _rows(args.a), _rows(args.b)
    ta, tb = _report(args.a, A), _report(args.b, B)

    print("\n=== where the two sets DISAGREE with each other")
    shared = [k for k in A if k in B]
    flips = []
    for addr in shared:
        truth = _tier(A[addr]["truth_lat"], A[addr]["truth_lon"])
        ga, gb = _tier(A[addr]["got_lat"], A[addr]["got_lon"]), _tier(B[addr]["got_lat"],
                                                                     B[addr]["got_lon"])
        if ga != gb:
            flips.append((addr, truth, ga, gb))
    print(f"  {len(flips)} of {len(shared)} addresses get a different tier\n")
    if flips:
        print(f"  {'address':30} {'truth':8} {'A':8} {'B':8}  verdict")
        for addr, truth, ga, gb in flips:
            better = ("B right" if gb == truth else "A right" if ga == truth else "both wrong")
            print(f"  {addr:30} {str(truth):8} {str(ga):8} {str(gb):8}  {better}")

    print(f"\nwrong tiers: A={ta['WRONG'] + ta['unplaced']}  B={tb['WRONG'] + tb['unplaced']} "
          f"(lower is better; 'never placed' counts as wrong — a flat with no dot is a "
          f"flat the bot cannot judge)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
