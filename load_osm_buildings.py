"""
Build `buildings.json` — every building footprint's centre in the Be'er Sheva box.

    python load_osm_buildings.py            # rebuild from C:\\osrm\\*.osm.pbf
    python load_osm_buildings.py --dry-run  # measure, write nothing

WHY THIS EXISTS
---------------
An address is a BUILDING. Until now nothing in the pipeline knew where any building was:
`house_anchors.json` holds the 1,082 elements that carry an `addr:housenumber` tag, and
that is **3.7% of the 19,214 footprints** the same extract already contains. The other
96% were invisible, so an interpolated house number landed wherever the arithmetic put
it — a garden, a car park, the middle of the road.

With this file `geocode` can snap a computed point onto a real structure, and can count
the buildings between two anchors instead of assuming even frontages.

Same source as `load_osm_addresses.py`: the local OSRM extract. Offline, free, repeatable.

Output shape (a coarse grid index, so a nearest-building query touches ~9 cells rather
than 19k points):
    {"cell": 0.002, "cells": {"1562:17399": [[lat, lon], …], …}}
"""
from __future__ import annotations
import json
import math
import sys
import time
from pathlib import Path

import config
import geocode

PBF_DIR = Path(r"C:\osrm")
OUT_PATH = config.ROOT / "buildings.json"

# Grid cell size in degrees. 0.002° is ~222 m north-south and ~190 m east-west here, so
# a 3x3 block of cells always contains everything within the snap radii we use, and each
# cell holds a handful of buildings.
CELL = 0.002

# Anything this big is not a home — the Grand Canyon Mall, the hospital, a hangar. Its
# centroid is hundreds of metres from any door, so snapping an address to it would move
# the point further from the truth, not closer.
MAX_FOOTPRINT_M = 120.0


def _pbf() -> Path | None:
    files = sorted(PBF_DIR.glob("*.osm.pbf"))
    return files[-1] if files else None


def _centre_and_size(o) -> tuple | None:
    """((lat, lon), longest_side_m) for a way's footprint, or None.

    The mean of the vertices, matching `load_osm_addresses._way_centre` so an address
    anchor and its own footprint agree."""
    try:
        nodes = list(o.nodes)
    except Exception:
        return None
    if len(nodes) > 1 and nodes[0].ref == nodes[-1].ref:
        nodes = nodes[:-1]
    lats, lons = [], []
    for nd in nodes:
        try:
            loc = nd.location
        except Exception:
            continue
        if loc.valid():
            lats.append(loc.lat)
            lons.append(loc.lon)
    if not lats:
        return None
    lat = sum(lats) / len(lats)
    lon = sum(lons) / len(lons)
    size = max((max(lats) - min(lats)) * 111320.0,
               (max(lons) - min(lons)) * 111320.0 * math.cos(math.radians(lat)))
    return (lat, lon), size


def _collect(path: Path) -> list:
    """[(lat, lon)] — the centre of every building footprint inside the box."""
    import osmium

    la0, lo0, la1, lo1 = geocode._bs_bounds()
    out, oversize = [], 0
    fp = osmium.FileProcessor(str(path)).with_locations()
    for o in fp:
        if o.type_str() != "w":
            continue
        if not o.tags.get("building"):
            continue
        got = _centre_and_size(o)
        if not got:
            continue
        (lat, lon), size = got
        if not (la0 <= lat <= la1 and lo0 <= lon <= lo1):
            continue
        if size > MAX_FOOTPRINT_M:
            oversize += 1
            continue
        out.append((round(lat, 6), round(lon, 6)))
    print(f"  {oversize} footprints skipped as too large (> {MAX_FOOTPRINT_M:.0f} m across)")
    return out


def _index(points: list) -> dict:
    cells: dict = {}
    for lat, lon in points:
        cells.setdefault(f"{int(lat / CELL)}:{int(lon / CELL)}", []).append([lat, lon])
    return cells


def build(dry_run: bool = False) -> dict:
    path = _pbf()
    if not path:
        print(f"no .osm.pbf under {PBF_DIR} — is the OSRM data directory there?")
        return {}
    print(f"reading {path.name} ({path.stat().st_size // (1024 * 1024)} MB) …")
    t = time.time()
    pts = _collect(path)
    print(f"  {len(pts)} buildings in the Be'er Sheva box ({time.time() - t:.0f}s)")
    cells = _index(pts)
    sizes = sorted(len(v) for v in cells.values())
    if sizes:
        print(f"  {len(cells)} grid cells, median {sizes[len(sizes) // 2]} "
              f"buildings each, largest {sizes[-1]}")
    out = {"cell": CELL, "cells": cells}
    if dry_run:
        print("\n--dry-run: nothing written")
        return out
    OUT_PATH.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"\nwrote {OUT_PATH} ({OUT_PATH.stat().st_size // 1024} KB)")
    return out


if __name__ == "__main__":
    build(dry_run="--dry-run" in sys.argv)
