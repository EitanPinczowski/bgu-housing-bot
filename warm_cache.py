"""
Geocode every address in the listings table, so every listing has a dot.

    python warm_cache.py            # resolve what is missing
    python warm_cache.py --all      # re-resolve everything, not just the gaps
    python warm_cache.py --dry-run  # count the work, touch nothing

WHY THIS EXISTS
---------------
`replay.py` re-geocodes as a side effect of re-classifying all 3,680 archived posts,
which takes over an hour — most of it spent on posts that were never listings. When the
only thing wrong is that the geocode cache has gaps, that is the wrong tool: there are
~410 listing addresses, and resolving just those takes minutes.

It was written after `geo_accuracy.py` was found to blank the cache (it used
`geocode._cache = {}` as scratch while `geocode_detailed` persists on success, so one run
wrote the empty dict to disk). That bug is fixed, but the recovery still needed a way to
rebuild the cache without an hour of replay — and gaps also appear whenever Overpass or
Nominatim is down during a run.

It only fills the CACHE. It does not touch verdicts, tiers or scores — `replay.py --apply`
remains the only thing that rewrites those.
"""
from __future__ import annotations
import sys
import time

import geocode
import storage


def addresses() -> list:
    with storage._conn() as c:
        rows = [r[0] for r in
                c.execute("SELECT DISTINCT address FROM listings "
                          "WHERE address IS NOT NULL AND address <> ''").fetchall()]
    return sorted({(a or "").strip() for a in rows if (a or "").strip()})


def main() -> int:
    dry = "--dry-run" in sys.argv
    every = "--all" in sys.argv
    addrs = addresses()
    todo = addrs if every else [a for a in addrs if not geocode.geocode_cached(a)]
    print(f"{len(addrs)} distinct listing addresses; {len(todo)} to resolve"
          + ("" if every else " (already-placed ones skipped)"))
    if dry:
        for a in todo[:20]:
            print(f"   {a[:60]}")
        print("\n--dry-run: nothing sent")
        return 0

    t0 = time.time()
    ok = 0
    for i, a in enumerate(todo, 1):
        coords, src = geocode.geocode_detailed(a)
        if coords:
            ok += 1
        if i % 25 == 0 or i == len(todo):
            print(f"  [{i}/{len(todo)}] {ok} resolved ({time.time() - t0:.0f}s)")
    print(f"\n{ok}/{len(todo)} resolved; "
          f"{sum(1 for a in addrs if geocode.geocode_cached(a))}/{len(addrs)} "
          f"listing addresses now have a point")
    print("run `python replay.py --apply` if you want the verdicts recomputed too")
    return 0


if __name__ == "__main__":
    sys.exit(main())
