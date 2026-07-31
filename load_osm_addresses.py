"""
Build `house_anchors.json` from the LOCAL Israel OSM extract — no Overpass.

    python load_osm_addresses.py            # rebuild from C:\\osrm\\*.osm.pbf
    python load_osm_addresses.py --dry-run  # measure, write nothing

WHY THIS EXISTS
---------------
`load_house_numbers.py` fetches the same thing from Overpass, and Overpass is down far
more often than not — all four mirrors failed every attempt on 2026-07-31, which is
exactly why the anchor set is thin (811 points, only 97 of 177 streets with the ≥2 that
`geocode.interpolate_house` requires).

The authoritative data is already on this machine: the OSRM routing container is built
from `israel-and-palestine-latest.osm.pbf`, which contains every address in Israel.
Reading it is offline, free, unlimited and repeatable.

WHAT THIS GETS THAT OVERPASS COULDN'T
-------------------------------------
The Overpass query required `["addr:street"]`. Measured in the Be'er Sheva box, the
extract holds 1,082 address points and **99 of them carry a house number with no street
tag** — common in Israeli OSM, and structurally unreachable by that query. Those are
bound here to the nearest street centreline, within a strict radius so a building in the
middle of a block is dropped rather than attached to the wrong road.

The output format is byte-compatible with what `geocode._load_anchors` already reads:
    {street_name: {house_number: [lat, lon]}}
"""
from __future__ import annotations
import json
import math
import sys
import time
from pathlib import Path

import config
import geocode
import streets

PBF_DIR = Path(r"C:\osrm")
OUT_PATH = config.ROOT / "house_anchors.json"

# How far a house-numbered building with no addr:street may sit from a street
# centreline and still be attributed to it. A Be'er Sheva block is ~60-80 m across, so
# 40 m keeps us on the near side of the street; beyond that the nearest centreline is a
# coin-flip between two roads and the anchor would be worse than no anchor at all.
MAX_BIND_METRES = 40.0

# An anchor tagged with a street name must actually BE near that street. Street names
# repeat inside the bounding box (two different ההגנה), and binding on the name alone
# merged them. Generous, because a street's own geometry can be a partial fragment:
# this is a "that is a different street" test, not a precision one.
MAX_ANCHOR_OFFSET_M = 200.0


def _pbf() -> Path | None:
    files = sorted(PBF_DIR.glob("*.osm.pbf"))
    return files[-1] if files else None


def _collect(path: Path) -> list:
    """[(lat, lon, housenumber, street_or_None)] inside the Be'er Sheva box.

    Nodes and building ways both carry addresses — in this extract 350 nodes against
    732 ways, so skipping ways would lose two thirds of the data. A way is represented
    by its first node, which for a building footprint is a corner: within a few metres
    of its centre and far inside the precision this feeds."""
    import osmium

    la0, lo0, la1, lo1 = geocode._bs_bounds()
    out = []
    fp = osmium.FileProcessor(str(path)).with_locations()
    for o in fp:
        tags = o.tags
        hn = tags.get("addr:housenumber")
        if not hn:
            continue
        kind = o.type_str()
        if kind == "n":
            loc = o.location
            if not loc.valid():
                continue
            lat, lon = loc.lat, loc.lon
        elif kind == "w":
            try:
                loc = o.nodes[0].location
                if not loc.valid():
                    continue
                lat, lon = loc.lat, loc.lon
            except Exception:
                continue
        else:
            continue
        if not (la0 <= lat <= la1 and lo0 <= lon <= lo1):
            continue
        out.append((lat, lon, hn.strip(), (tags.get("addr:street") or "").strip() or None))
    return out


def _street_points() -> dict:
    """{street: [(lat, lon), …]} for every named street we know the geometry of."""
    pts = {}
    for name in {s for s in streets._index().values()}:
        flat = [p for seg in streets.geometry(name) for p in seg]
        if flat:
            pts[name] = flat
    return pts


def _nearest_street(lat, lon, street_pts) -> tuple:
    """(street, metres) for the closest centreline point, or (None, inf).

    Brute force over ~1,170 streets is fine: this runs once per address element, a
    few hundred times, offline."""
    best, best_d = None, float("inf")
    for name, pts in street_pts.items():
        for pl, po in pts:
            # cheap planar distance — everything here is inside one city
            d = math.hypot((pl - lat) * 111320.0,
                           (po - lon) * 111320.0 * math.cos(math.radians(lat)))
            if d < best_d:
                best, best_d = name, d
    return best, best_d


def build(dry_run: bool = False) -> dict:
    path = _pbf()
    if not path:
        print(f"no .osm.pbf under {PBF_DIR} — is the OSRM data directory there?")
        return {}
    before = geocode._load_anchors()
    print(f"reading {path.name} ({path.stat().st_size // (1024 * 1024)} MB) …")
    t = time.time()
    raw = _collect(path)
    print(f"  {len(raw)} address points in the Be'er Sheva box ({time.time() - t:.0f}s)")

    street_pts = _street_points()
    anchors: dict = {}
    bound_by_name = bound_by_distance = dropped = far_from_named = 0
    for lat, lon, hn, street in raw:
        name = None
        if street:
            real, _how = streets.canonical(street)
            name = real or street
            # A street name is not unique inside the bounding box, and binding on the
            # name ALONE silently merged two different streets: ההגנה ended up with
            # anchors 10 m from its geometry AND anchors 2,887 m away, which then
            # placed ההגנה 89 some 3.5 km from the real address — the single worst
            # error in the hold-out. An anchor has to be near the street it claims.
            pts = street_pts.get(name)
            if pts:
                off = min(math.hypot((p[0] - lat) * 111320.0,
                                     (p[1] - lon) * 111320.0
                                     * math.cos(math.radians(lat))) for p in pts)
                if off > MAX_ANCHOR_OFFSET_M:
                    far_from_named += 1
                    continue
            bound_by_name += 1
        else:
            cand, dist = _nearest_street(lat, lon, street_pts)
            if cand and dist <= MAX_BIND_METRES:
                name, _ = cand, None
                bound_by_distance += 1
            else:
                dropped += 1
                continue
        anchors.setdefault(name, {})[hn] = [round(lat, 6), round(lon, 6)]

    usable = sum(1 for v in anchors.values() if len(v) >= 2)
    was_usable = sum(1 for v in before.values() if len(v) >= 2)
    print(f"  bound by addr:street       : {bound_by_name}")
    print(f"  rejected, far from that name: {far_from_named}  (a different street "
          f"sharing the name)")
    print(f"  bound to the nearest street: {bound_by_distance}  (<= {MAX_BIND_METRES:.0f} m)")
    print(f"  dropped, too far from any  : {dropped}")
    print()
    print(f"anchor points : {sum(len(v) for v in before.values())} -> {sum(len(v) for v in anchors.values())}")
    print(f"streets       : {len(before)} -> {len(anchors)}")
    print(f"…with >=2 (interpolation works): {was_usable} -> {usable}")

    if dry_run:
        print("\n--dry-run: nothing written")
        return anchors
    OUT_PATH.write_text(json.dumps(anchors, ensure_ascii=False, sort_keys=True),
                        encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")
    return anchors


if __name__ == "__main__":
    build(dry_run="--dry-run" in sys.argv)
