"""
Build `house_anchors.json` — the OSM `addr:housenumber` nodes for every named Be'er
Sheva street, so geocode can INTERPOLATE where house N sits along a street instead of
returning an arbitrary point on the line.

Why it matters: a street is a line, not a point. "אברהם אבינו 38" resolved to whatever
point OSM handed back, which is how a red-end address read as green. With two or more
anchors we can place the number properly and judge the listing on its real position —
which also rescues good apartments on boundary-crossing streets.

    python load_house_numbers.py

Re-run occasionally (OSM address coverage grows). Free Overpass; no API key.
"""
from __future__ import annotations
import json
import time

import requests

import config
import geocode

OUT_PATH = config.ROOT / "house_anchors.json"


def _overpass(query: str):
    timeout = max(getattr(config, "OVERPASS_TIMEOUT_SEC", 15), 60)
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


def build() -> dict:
    la0, lo0, la1, lo1 = geocode._bs_bounds()
    bbox = f"{la0},{lo0},{la1},{lo1}"
    # every element carrying a street + house number (nodes and building ways)
    q = (f'[out:json][timeout:180];'
         f'(node["addr:housenumber"]["addr:street"]({bbox});'
         f' way["addr:housenumber"]["addr:street"]({bbox}););'
         f'out center tags;')
    print("fetching address nodes …")
    data = _overpass(q)
    if not data:
        print("could not fetch — leaving house_anchors.json unchanged")
        return {}
    anchors: dict = {}
    for el in data.get("elements", []):
        t = el.get("tags", {})
        street, num = t.get("addr:street"), t.get("addr:housenumber")
        c = el.get("center") or el
        if not (street and num and c.get("lat")):
            continue
        try:
            n = int(str(num).split("-")[0].strip())      # "12-14" -> 12
        except ValueError:
            continue
        anchors.setdefault(street, {})[str(n)] = [round(c["lat"], 6), round(c["lon"], 6)]
    usable = {s: v for s, v in anchors.items() if len(v) >= 2}     # need 2+ to interpolate
    OUT_PATH.write_text(json.dumps(anchors, ensure_ascii=False), encoding="utf-8")
    print(f"streets with address nodes: {len(anchors)}  ·  interpolatable (≥2): {len(usable)}")
    print(f"wrote {OUT_PATH}")
    return anchors


if __name__ == "__main__":
    build()
