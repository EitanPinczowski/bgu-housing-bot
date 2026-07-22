"""
Import Be'er Sheva neighborhood boundary polygons (שכונה ב / ג / ד) from
OpenStreetMap via Overpass into `neighborhoods.json`.

`zones.neighborhood_of(lat, lon)` uses these polygons to tell which neighborhood a
listing's coordinate falls in — which drives two rules:
  • the ב > ג = ד fit-score preference (a tie-breaker), and
  • the hard-drop of any listing whose neighborhood is NOT ב/ג/ד.

OSM has `boundary=administrative` relations for these neighborhoods; each relation's
outer boundary is a handful of ways whose endpoints chain into one ring, which we
stitch here. ד is also seeded from the existing `no_amber_zones.json` if OSM is
unreachable, so the file is never empty for the neighborhood we already had.

    python load_neighborhoods.py

Re-run whenever you want to refresh the boundaries. Uses the same free Overpass
mirrors as geocode.py (config.OVERPASS_URLS); no API key.
"""
from __future__ import annotations
import json
import time
from typing import Optional

import requests

import config
import geocode

# The neighborhoods we actually care about (letter -> OSM name). Only these get a
# polygon; every other named שכונה is handled by the text rule (dropped).
TARGETS = {"ב": "שכונה ב", "ג": "שכונה ג", "ד": "שכונה ד"}
OUT_PATH = config.ROOT / "neighborhoods.json"


def _close(a, b, tol=1e-6) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def _stitch(ways: list) -> list:
    """Chain a relation's outer ways (each a list of (lat,lon)) into a single ring by
    matching endpoints, reversing a way when needed. Returns [] if nothing connects."""
    ways = [list(w) for w in ways if w]
    if not ways:
        return []
    ring = ways.pop(0)
    changed = True
    while ways and changed:
        changed = False
        for i, w in enumerate(ways):
            if _close(w[0], ring[-1]):
                ring += w[1:]
            elif _close(w[-1], ring[-1]):
                ring += list(reversed(w))[1:]
            else:
                continue
            ways.pop(i)
            changed = True
            break
    return ring


def _fetch_ring(name: str) -> Optional[list]:
    """The stitched outer ring [[lat,lon],…] of the named boundary relation, or None."""
    la0, lo0, la1, lo1 = geocode._bs_bounds()
    bbox = f"{la0},{lo0},{la1},{lo1}"
    q = (f'[out:json][timeout:60];'
         f'relation["name"="{name}"]["boundary"="administrative"]({bbox});'
         f'out geom;')
    timeout = getattr(config, "OVERPASS_TIMEOUT_SEC", 15)
    for url in config.OVERPASS_URLS:
        try:
            time.sleep(1.0)
            r = requests.post(url, data={"data": q},
                              headers={"User-Agent": config.NOMINATIM_USER_AGENT},
                              timeout=max(timeout, 40))
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            print(f"  [{name}] mirror failed ({type(exc).__name__}) — trying next")
            continue
        for rel in data.get("elements", []):
            outers = [[(p["lat"], p["lon"]) for p in m["geometry"]]
                      for m in rel.get("members", [])
                      if m.get("role") == "outer" and m.get("geometry")]
            ring = _stitch(outers)
            if len(ring) >= 4:
                return [[lat, lon] for lat, lon in ring]
        return None          # a valid response with no usable relation — authoritative
    return None


def _seed_dalet() -> Optional[list]:
    """ד polygon from the existing no_amber_zones.json, as an OSM-unreachable fallback."""
    try:
        with open(config.NO_AMBER_ZONES_PATH, encoding="utf-8") as f:
            for z in json.load(f).get("zones", []):
                if "ד" in z.get("name", ""):
                    return z["polygon_latlon"]
    except Exception:
        pass
    return None


def main() -> None:
    out = []
    for letter, name in TARGETS.items():
        print(f"fetching {name} …")
        ring = _fetch_ring(name)
        if not ring and letter == "ד":
            ring = _seed_dalet()
            if ring:
                print("  ד: seeded from no_amber_zones.json")
        if ring:
            out.append({"letter": letter, "name": name, "polygon_latlon": ring})
            print(f"  {name}: {len(ring)} points")
        else:
            print(f"  {name}: NOT found (left out — text rule still applies)")
    OUT_PATH.write_text(json.dumps({"neighborhoods": out}, ensure_ascii=False),
                        encoding="utf-8")
    print(f"wrote {OUT_PATH} with {len(out)} neighborhood(s)")


if __name__ == "__main__":
    main()
