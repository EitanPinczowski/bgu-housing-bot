"""
Fetch static map landmarks for area_map.py from OpenStreetMap (Overpass) into
`area_features.json`: the BGU campus + Soroka hospital footprints and the main
named streets around the search area. Cached to a file so the map generator needs
no network. Re-run to refresh.

    python load_area_features.py

Free Overpass mirrors (config.OVERPASS_URLS); no API key.
"""
from __future__ import annotations
import json
import time

import requests

import config
import geocode

OUT_PATH = config.ROOT / "area_features.json"

# The two big landmarks, by their OSM way id (found via an amenity query).
_LANDMARKS = [
    ("university", "אוניברסיטת בן גוריון", 135310095),
    ("hospital", "סורוקה", 135312395),
]


def _overpass(query: str):
    timeout = max(getattr(config, "OVERPASS_TIMEOUT_SEC", 15), 40)
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


def _way_geom(el) -> list:
    return [[p["lat"], p["lon"]] for p in el.get("geometry", [])]


def fetch_landmarks() -> list:
    ids = "".join(f"way({wid});" for _, _, wid in _LANDMARKS)
    data = _overpass(f"[out:json][timeout:40];({ids});out geom;")
    if not data:
        return []
    by_id = {e["id"]: e for e in data.get("elements", [])}
    out = []
    for kind, name, wid in _LANDMARKS:
        el = by_id.get(wid)
        if el and el.get("geometry"):
            out.append({"kind": kind, "name": name, "polygon_latlon": _way_geom(el)})
            print(f"  {name}: {len(el['geometry'])} points")
    return out


def fetch_streets() -> list:
    la0, lo0, la1, lo1 = geocode._bs_bounds()
    bbox = f"{la0},{lo0},{la1},{lo1}"
    q = (f'[out:json][timeout:60];'
         f'way["highway"~"^(primary|secondary|trunk)$"]["name"]({bbox});'
         f'out geom;')
    data = _overpass(q)
    if not data:
        return []
    # merge segments of the same street name into one entry (keep the longest few)
    streets: dict = {}
    for el in data.get("elements", []):
        nm = el.get("tags", {}).get("name")
        g = _way_geom(el)
        if nm and len(g) >= 2:
            streets.setdefault(nm, []).append(g)
    out = [{"name": nm, "segments": segs} for nm, segs in streets.items()]
    print(f"  streets: {len(out)} named arteries")
    return out


def main() -> None:
    print("fetching landmarks (BGU, Soroka) …")
    landmarks = fetch_landmarks()
    print("fetching main streets …")
    streets = fetch_streets()
    OUT_PATH.write_text(json.dumps({"landmarks": landmarks, "streets": streets},
                                   ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT_PATH}: {len(landmarks)} landmarks, {len(streets)} streets")


if __name__ == "__main__":
    main()
