"""
Import a WIDE set of Be'er Sheva neighborhood boundaries into `map_neighborhoods.json`,
purely so the dashboard map has something to orient by.

    python load_map_neighborhoods.py

WHY THIS IS A SEPARATE FILE FROM neighborhoods.json
---------------------------------------------------
`zones.in_allowed_neighborhood()` returns True if a point falls inside ANY polygon in
`neighborhoods.json`. That file therefore holds ONLY ב/ג/ד — the neighborhoods this
search accepts. Adding שכונה א/ה/ו there to label the map would silently make listings
in those neighborhoods pass the ב/ג/ד gate: a classification regression wearing a
cosmetic disguise.

So these polygons live in their own file, `zones.py` never reads it, and a test
(`test_zones.py`) asserts that a point inside שכונה ו is still rejected with this file
present. Display only. Nothing here affects which flats you get alerted about.

Reuses the relation-stitching from load_neighborhoods.py and the same free Overpass
mirrors; no API key. Overpass is flaky, so a partial result is normal and fine — the
map just labels fewer areas.
"""
from __future__ import annotations
import json
import sys

import config
import load_neighborhoods as base

OUT_PATH = config.ROOT / "map_neighborhoods.json"

# Everything worth orienting by, including the areas this search REJECTS — knowing that
# a dot sits in שכונה ו is exactly why you'd want the outline drawn.
TARGETS = [
    "שכונה א", "שכונה ב", "שכונה ג", "שכונה ד", "שכונה ה", "שכונה ו",
    "שכונה ז", "שכונה ח", "שכונה ט", "שכונה י", "שכונה יא",
    "רמות", "נווה זאב", "נחל בקע", "נאות לון", "הרובע", "נווה מנחם",
    "שכונת הפארק", "העיר העתיקה",
]


def main() -> int:
    out = []
    for name in TARGETS:
        print(f"fetching {name} …")
        ring = base._fetch_ring(name)
        if ring:
            out.append({"name": name, "polygon_latlon": ring})
            print(f"  {name}: {len(ring)} points")
        else:
            print(f"  {name}: not found (skipped)")
    if not out:
        print("\nnothing resolved — leaving any existing file alone "
              "(Overpass is often down; re-run later)")
        return 1
    OUT_PATH.write_text(json.dumps({"neighborhoods": out}, ensure_ascii=False),
                        encoding="utf-8")
    print(f"\nwrote {OUT_PATH}: {len(out)} of {len(TARGETS)} areas")
    print("display only — zones.py still reads neighborhoods.json for ב/ג/ד")
    return 0


if __name__ == "__main__":
    sys.exit(main())
