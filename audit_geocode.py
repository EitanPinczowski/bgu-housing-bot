"""
Is every geocoded point actually ON the street it names?

    python audit_geocode.py            # audit the static table + stored listings
    python audit_geocode.py --static   # just the static table (fast, no DB)
    python audit_geocode.py --fix      # …and drop bad CACHED points so they re-resolve

A geocoding error is silent: a listing lands somewhere plausible, gets a tier, gets
a walk time, and nothing looks wrong. This checks placements against an independent
source — the street's real OSM geometry from `area_features.json` — and reports
anything further than AUDIT_MAX_OFFSET_M from the street it claims to be on.

That is how the וינגייט bug surfaced (2026-07-30): a STATIC_TABLE street entry sat
520 m off its own street AND answered every house number with the same coordinate,
so `interpolate_house` never ran. Median offset across stored listings was 6 m, so
anything past ~150 m is a real defect, not noise.
"""
from __future__ import annotations
import sqlite3
import statistics
import sys

import config
import geocode
import streets

# Generous: a street-level point legitimately sits anywhere along the street, and a
# long street's own points span hundreds of metres. This is a blunder detector.
AUDIT_MAX_OFFSET_M = 150


def _offset_from_street(name: str, coords) -> float | None:
    """Metres from `coords` to the nearest point of the street `name` names, or None
    when we have no geometry to check against (so it is never a false alarm)."""
    canon, _how = streets.canonical(name)
    geo = streets.geometry(canon) if canon else []
    if not geo:
        return None
    return min(geocode._haversine_m(coords[0], coords[1], p[0], p[1])
               for seg in geo for p in seg)


def audit_static() -> list:
    """Static-table entries that name a known street but sit far from it."""
    bad = []
    for name, pt in geocode.STATIC_TABLE.items():
        d = _offset_from_street(name, pt)
        if d is not None and d > AUDIT_MAX_OFFSET_M:
            bad.append((round(d), name))
    return sorted(bad, reverse=True)


def audit_listings() -> tuple:
    """(offsets, far) for every stored listing we can place and check."""
    with sqlite3.connect(config.DB_PATH) as c:
        rows = c.execute("SELECT address, geocode_source FROM listings "
                         "WHERE address IS NOT NULL").fetchall()
    offsets, far = [], []
    for addr, src in rows:
        coords = geocode.geocode(addr)
        if not coords:
            continue
        d = _offset_from_street(addr, coords)
        if d is None:
            continue
        offsets.append(d)
        if d > AUDIT_MAX_OFFSET_M:
            far.append((round(d), addr, src))
    return offsets, sorted(far, reverse=True)


def main() -> int:
    print("=== static table vs real street geometry ===")
    bad = audit_static()
    for d, name in bad:
        print(f"  {d:5} m  {name}")
    print(f"  {len(bad)} entr{'y' if len(bad) == 1 else 'ies'} beyond "
          f"{AUDIT_MAX_OFFSET_M} m")

    if "--static" in sys.argv:
        return 1 if bad else 0

    print("\n=== stored listings vs the street they name ===")
    offsets, far = audit_listings()
    if offsets:
        offsets.sort()
        print(f"  checked {len(offsets)}  median {statistics.median(offsets):.0f} m  "
              f"p90 {offsets[int(.9 * len(offsets))]:.0f} m  max {offsets[-1]:.0f} m")
    for d, addr, src in far[:15]:
        print(f"  {d:5} m  [{src}]  {addr[:52]}")
    print(f"  {len(far)} listing(s) beyond {AUDIT_MAX_OFFSET_M} m from their street")

    # A CACHED point never expires, so one bad lookup is wrong forever (ביאליק sat
    # 344 m out until it was dropped, after which it re-resolved to 36 m). Cached
    # entries are the only ones safe to auto-fix: dropping one just forces a fresh
    # lookup, whereas a bad static entry needs a human to pick the right coordinate.
    if "--fix" in sys.argv:
        dropped = 0
        for _d, addr, src in far:
            if src == "cache":
                geocode.uncache(addr)
                dropped += 1
        print(f"\n  --fix: dropped {dropped} cached point(s); re-run to confirm")
        if dropped:
            return 0
    elif far:
        print("  (re-run with --fix to drop the cached ones)")
    return 1 if (bad or far) else 0


if __name__ == "__main__":
    sys.exit(main())
