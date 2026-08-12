"""
Geocode every address in the listings table, so every listing has a dot.

    python warm_cache.py            # resolve what is missing (listings table)
    python warm_cache.py --archive  # ALSO every address in the post archive — do this
                                    #   before a full `replay.py`, which geocodes those
    python warm_cache.py --all      # re-resolve everything, not just the gaps
    python warm_cache.py --dry-run  # count the work, touch nothing

Resumable by construction: every success is written to the geocode cache, so a run that
is killed (or a machine that sleeps) picks up where it left off — re-run the same command.

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
import json
import sys
import time

import geocode
import storage


def addresses(archive: bool = False) -> list:
    """Distinct addresses to resolve — the listings table, or the whole POST ARCHIVE.

    `--archive` exists because the listings table is NOT what a replay geocodes.
    `replay.py` re-classifies every archived post, so it resolves the archive's addresses,
    and warming only the listings table leaves the replay to do the rest at network speed.
    Measured 2026-08-12: 396 distinct listing addresses (9 unresolved) against **2,686 in
    the archive, of which 521 still need a network call** — which is why a full replay
    ran for hours instead of the ~26 minutes the notes remember.

    COUNT THE ONES THAT NEED THE NETWORK, NOT THE ONES MISSING FROM THE CACHE FILE. The
    first estimate of this backlog was 2,148 — four times too high — because it tested
    membership of `geocode_cache.json`. `geocode_cached` also answers from the static
    table, the anchors, house-number interpolation and street geometry, so most uncached
    addresses never reach a mirror. `geocode_cached(a) is None` is the honest test, and it
    is the one the loop below already used.

    Deliberately the same normalisation the classifier uses (`_postprocess_extract` is NOT
    applied — the address is taken as stored), so a warmed entry is one the replay will
    actually hit rather than a near-miss."""
    with storage._conn() as c:
        rows = [r[0] for r in
                c.execute("SELECT DISTINCT address FROM listings "
                          "WHERE address IS NOT NULL AND address <> ''").fetchall()]
        if archive:
            for (pj,) in c.execute("SELECT parsed_json FROM posts "
                                   "WHERE parsed_json IS NOT NULL"):
                try:
                    rows.append(json.loads(pj).get("street_address_or_neighborhood"))
                except Exception:
                    continue
    return sorted({(a or "").strip() for a in rows if (a or "").strip()})


def main() -> int:
    dry = "--dry-run" in sys.argv
    every = "--all" in sys.argv
    archive = "--archive" in sys.argv
    addrs = addresses(archive=archive)
    todo = addrs if every else [a for a in addrs if not geocode.geocode_cached(a)]
    scope = "archive + listing" if archive else "listing"
    print(f"{len(addrs)} distinct {scope} addresses; {len(todo)} to resolve"
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
