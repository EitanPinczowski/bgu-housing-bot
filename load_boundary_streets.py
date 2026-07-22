"""
Build `boundary_streets.json` — the set of streets whose OSM geometry crosses the
in-range↔RED boundary. A long street can run half through the green/ב-ג-ד area and
half through red; Overpass geocodes it by NAME to an arbitrary point on it (often the
wrong side), and OSM rarely has the exact house number — so a name-only placement on
such a street can't be trusted as GREEN. The pipeline reads this set and classifies a
name-only listing on a boundary street as RED (see geocode.is_boundary_street).

    python load_boundary_streets.py

Re-run whenever the green zone / ב-ג-ד polygons change. Free Overpass (no key).
"""
from __future__ import annotations
import json
import time

import requests

import config
import geocode
import zones

OUT_PATH = config.ROOT / "boundary_streets.json"


def _overpass(query: str):
    timeout = max(getattr(config, "OVERPASS_TIMEOUT_SEC", 15), 50)
    for url in config.OVERPASS_URLS:
        try:
            time.sleep(1.0)
            r = requests.post(url, data={"data": query},
                              headers={"User-Agent": config.NOMINATIM_USER_AGENT}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            print(f"  mirror {url.split('/')[2]} failed ({type(exc).__name__})")
    return None


def build() -> list:
    la0, lo0, la1, lo1 = geocode._bs_bounds()
    bbox = f"{la0},{lo0},{la1},{lo1}"
    # every named drivable/walkable street in the box, with geometry
    q = (f'[out:json][timeout:90];'
         f'way["highway"]["name"]({bbox});'
         f'out geom;')
    data = _overpass(q)
    if not data:
        print("could not fetch streets — leaving boundary_streets.json unchanged")
        return []
    # aggregate every vertex per street NAME (a street is many ways)
    tiers_by_name: dict = {}
    for w in data.get("elements", []):
        name = w.get("tags", {}).get("name")
        if not name:
            continue
        seen = tiers_by_name.setdefault(name, set())
        for p in w.get("geometry", []):
            seen.add(zones.classify_effective(p["lat"], p["lon"]))
    # a boundary street has BOTH an in-range vertex and a RED one
    boundary = sorted(n for n, ts in tiers_by_name.items()
                      if "RED" in ts and (("GREEN" in ts) or ("AMBER" in ts)))
    OUT_PATH.write_text(json.dumps({"streets": boundary}, ensure_ascii=False),
                        encoding="utf-8")
    print(f"scanned {len(tiers_by_name)} named streets → {len(boundary)} cross the boundary")
    print(f"wrote {OUT_PATH}")
    return boundary


if __name__ == "__main__":
    build()
