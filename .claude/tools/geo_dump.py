#!/usr/bin/env python
"""Per-address hold-out errors, so a geocoding regression can be ATTRIBUTED, not guessed.

`geo_accuracy.py` prints percentiles. That is enough to see that seeding took max from
436 m to 715 m and coverage from 249 to 248 on 2026-08-11, and useless for saying which
address moved — so the first explanation offered that day (the consistency guard getting
stricter as density rises) was wrong, and stayed wrong until this existed. The rejection
set had grown by exactly two addresses, one of them for an unrelated reason.

Same hold-out loop as `geo_accuracy.run`, same seed and sample, one row per address.

    python .claude/tools/geo_dump.py --out data/geo_before.csv
    # ... change something ...
    python .claude/tools/geo_dump.py --out data/geo_after.csv
    python .claude/tools/geo_diff.py data/geo_before.csv data/geo_after.csv

To grade a DIFFERENT anchor set without touching the real file — comparing HEAD's anchors
against the working tree's, say — pass `--anchors`. `geocode._load_anchors()` reads the
govmap path at call time, so pointing it elsewhere first is enough:

    git show HEAD:govmap_anchors.json > /tmp/head.json
    python .claude/tools/geo_dump.py --anchors /tmp/head.json --out data/geo_head.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get(
    "CLAUDE_PROJECT_DIR", str(Path(__file__).resolve().parents[2])))

import geo_accuracy                                # noqa: E402
import geocode                                     # noqa: E402

# The FROZEN ruler. Grading against the live anchors would improve the answer key and the
# geocoder together, indistinguishably — which is exactly the mistake that made the first
# pinned truth file report p50 12 / max 992 where the real baseline was 16 / 2093.
DEFAULT_TRUTH = "data/truth_merged_20260810.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anchors", help="govmap anchors to geocode WITH (default: the live file)")
    ap.add_argument("--truth", default=DEFAULT_TRUTH, help=f"answer key (default: {DEFAULT_TRUTH})")
    ap.add_argument("--out", required=True, help="CSV to write")
    ap.add_argument("--sample", type=int, default=geo_accuracy.SAMPLE)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--local-only", action="store_true",
                    help="silence Overpass and Nominatim — see the docstring below")
    args = ap.parse_args()

    # THE HARNESS IS NOT REPRODUCIBLE WITH THE EXTERNAL TIERS ON, AND THE TAIL IS MOSTLY
    # THEM. Measured 2026-08-11: the SAME anchors run twice moved 4 addresses, one of them
    # by 122 m and one from placed to UNPLACED, purely because a different Overpass mirror
    # answered (`overpass-api.de` was down, `maps.mail.ru` up). p50 moved 11 -> 12 and
    # coverage 249 -> 248 with no change to the data at all — so `max` and `coverage`,
    # two of the gate's own criteria, were being read off a coin flip. A 715 m max was
    # reported as a regression that day and was not one.
    #
    # With this flag the run measures only what anchors and interpolation decide, which is
    # the part any seeding or placement change can actually move, and it is deterministic.
    # Leave it OFF to see what the live pipeline really does; turn it ON to compare two
    # anchor sets and have the difference mean something.
    if args.local_only:
        # (coords, source, responded). responded=True on purpose: it means "the mirrors
        # answered and had nothing", which is a clean miss. responded=False means every
        # mirror FAILED, and the caller treats that differently — silencing the tier must
        # not look like the network being down.
        geocode._overpass = lambda *a, **k: (None, None, True)
        geocode._nominatim = lambda *a, **k: None

    if args.anchors:
        geocode._GOVMAP_ANCHORS_PATH = Path(args.anchors)
        geocode._anchors = None                    # force the re-read
    anchors = geocode._load_anchors()

    key = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    truth = sorted((st, num, tuple(pt))
                   for st, nums in key.items() for num, pt in nums.items()
                   if str(num).isdigit())
    random.Random(args.seed).shuffle(truth)
    truth = truth[:args.sample]
    print(f"holding out {len(truth)} addresses from {args.truth}")

    rows = []
    original, real_cache, real_save = anchors, geocode._load_cache(), geocode._save_cache
    # geocode_detailed PERSISTS the cache after a successful external lookup, so the blank
    # dict below would be written straight to disk — one run of the original harness left
    # the real cache with a single entry.
    geocode._save_cache = lambda: None
    try:
        for street, number, (tlat, tlon) in truth:
            geocode._anchors = geo_accuracy._held_out(street, number, anchors)
            geocode._cache = {}                    # never answer from a cached hit
            got, source = geocode.geocode_detailed(f"{street} {number}")
            if not got:
                rows.append((f"{street} {number}", "", "UNPLACED", ""))
                continue
            err = geocode._haversine_m(tlat, tlon, got[0], got[1])
            rows.append((f"{street} {number}", f"{err:.1f}",
                         geocode.confidence(source), source))
    finally:
        geocode._anchors = original
        geocode._cache = real_cache                # the REAL one back, not a blank
        geocode._save_cache = real_save

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["address", "error_m", "tier", "source"])
        w.writerows(rows)
    placed = [r for r in rows if r[2] != "UNPLACED"]
    print(f"wrote {len(rows)} rows ({len(rows) - len(placed)} unplaced) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
