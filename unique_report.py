"""
How many listings have a location of their OWN?

    python unique_report.py            # the score, and what is holding it back
    python unique_report.py --streets  # …plus the per-street work list

WHY THIS EXISTS
---------------
"Most of the points on the map are clusters, even when zoomed in." That is a complaint
about the DATA, not the rendering: 231 of 308 placed listings share a coordinate with
another listing, and no zoom level can separate two flats that resolved to one point.

`geo_accuracy.py` answers "how far off is a point?" and `audit_geocode.py` answers "is it
on the right street?". Neither answers "how many flats can you actually tell apart?",
which is the thing the user asked to maximise. This does, and it names the cause for each
pile so the next fix is chosen by size rather than by guess.

The ceiling is not 392. A post that says only `שכונה ד` cannot be placed to a building by
anyone, so the honest maximum is the number of DISTINCT (street, house number) addresses
the posts actually contain.
"""
from __future__ import annotations
import sys
from collections import Counter, defaultdict

import geocode
import storage
import streets


def _street_of(address: str):
    """The canonical street an address names, or None — the same resolution the
    geocoder itself uses, so this measures the real pipeline and not an idealised one."""
    for cand in geocode._candidate_tokens(address or "")[:2]:
        real, _how = streets.canonical(cand)
        if real:
            return real
    return None


def collect() -> dict:
    with storage._conn() as c:
        rows = [(r[0] or "", r[1]) for r in
                c.execute("SELECT address, geocode_source FROM listings").fetchall()]

    at = defaultdict(list)                       # coordinate -> [(address, source)]
    unplaced = 0
    for addr, src in rows:
        pt = geocode.geocode_cached(addr)
        if not pt:
            unplaced += 1
            continue
        at[(round(pt[0], 6), round(pt[1], 6))].append((addr, src))

    numbered, pairs = 0, {}
    kinds = Counter()
    for addr, _src in rows:
        hn = geocode._house_number(addr)
        if hn:
            numbered += 1
            st = _street_of(addr)
            if st:
                pairs.setdefault((st, hn), addr)
            continue
        t = addr.strip()
        if not t:
            kinds["empty"] += 1
        elif geocode.is_bare_neighborhood(t):
            kinds["bare neighbourhood"] += 1
        elif _street_of(t):
            kinds["street, no number"] += 1
        else:
            kinds["landmark / description"] += 1

    # where do the distinct numbered addresses actually land?
    addr_at = defaultdict(list)
    for (st, hn), sample in pairs.items():
        pt = geocode.place_house(st, hn)[0] or geocode.geocode_cached(sample)
        if pt:
            addr_at[(round(pt[0], 6), round(pt[1], 6))].append(f"{st} {hn}")

    return {"rows": rows, "at": at, "unplaced": unplaced, "numbered": numbered,
            "kinds": kinds, "pairs": pairs, "addr_at": addr_at}


def report(d: dict) -> int:
    rows, at, addr_at = d["rows"], d["at"], d["addr_at"]
    placed = sum(len(v) for v in at.values())
    alone = sum(1 for v in at.values() if len(v) == 1)
    addr_alone = sum(1 for v in addr_at.values() if len(v) == 1)

    print("=== the score ===")
    print(f"  listings                              {len(rows)}")
    print(f"    with a house number                 {d['numbered']}")
    print(f"    without                             {len(rows) - d['numbered']}")
    for k, n in d["kinds"].most_common():
        print(f"      {k:34} {n}")
    print()
    print(f"  DISTINCT (street, number) addresses   {len(d['pairs'])}   <- the honest ceiling")
    print(f"    …on their own point                 {addr_alone}   <- MAXIMISE THIS")
    print()
    print(f"  listings placed                       {placed} on {len(at)} points")
    print(f"    alone on their own point            {alone}")
    print(f"    sharing a point with another        {placed - alone}")
    print(f"  listings with no coordinate at all    {d['unplaced']}")

    print("\n=== what is holding it back — biggest piles ===")
    print(f"  {'n':>4}  {'distinct addresses':>18}  cause")
    for pt, v in sorted(at.items(), key=lambda kv: -len(kv[1]))[:10]:
        if len(v) < 2:
            continue
        srcs = Counter(s for _a, s in v)
        distinct = len({(_street_of(a), geocode._house_number(a)) for a, _s in v})
        top = srcs.most_common(1)[0][0]
        cause = ("same building — one point is correct" if distinct == 1 else
                 f"{distinct} DIFFERENT addresses forced onto one point [{top}]")
        print(f"  {len(v):4}  {distinct:>18}  {cause}")
        print(f"        e.g. {v[0][0][:44]!r}")
    return 0


def street_worklist(d: dict) -> None:
    """Streets where placing the street would fix several listings at once."""
    anchors = geocode._load_anchors()
    need = defaultdict(set)
    listings = Counter()
    for addr, _src in d["rows"]:
        hn = geocode._house_number(addr)
        if not hn:
            continue
        st = _street_of(addr)
        if not st or geocode.place_house(st, hn)[0]:
            continue
        need[st].add(hn)
        listings[st] += 1
    print("\n=== streets whose numbered addresses cannot be placed ===")
    print(f"  {'street':26} {'addresses':>9} {'listings':>9} {'anchors':>8}")
    for st in sorted(need, key=lambda s: -listings[s]):
        n = len([k for k in (anchors.get(st) or {}) if str(k).isdigit()])
        print(f"  {st:26} {len(need[st]):9} {listings[st]:9} {n:8}")
    print(f"  {sum(len(v) for v in need.values())} addresses / "
          f"{sum(listings.values())} listings on {len(need)} streets")


if __name__ == "__main__":
    data = collect()
    rc = report(data)
    if "--streets" in sys.argv:
        street_worklist(data)
    sys.exit(rc)
