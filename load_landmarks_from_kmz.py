"""
Import hand-surveyed LANDMARK polygons from a Google My Maps KMZ/KML export.

Usage:
    python load_landmarks_from_kmz.py path\\to\\Untitled_layer.kmz

Draw one shape per landmark in My Maps, NAME EACH SHAPE with exactly the term posts use
for it (`הבלוק`, `מגדלי דוד`, …), export "KML/KMZ" (entire map), and point this at it.

WHY A POLYGON AND NOT JUST A POINT. A landmark's size IS its uncertainty, and guessing it
goes wrong in both directions. `הבלוק` sat in `geocode._AREA_KEYS` described as "the whole
student quarter, several streets across" and so was graded `area` — not a location at all.
Surveyed, it is 85 x 96 m: a 123 m diagonal, TIGHTER than a typical street centroid.
The opposite error is just as easy: `אביסרור` measures 299 m, so calling it exact would
claim a precision it does not have. Grading from the drawn extent (see
`geocode._landmark_grade`) removes the guess from both.

MEASURED CAUTION — do not re-derive an area's size from the streets that co-occur with it
in addresses. Doing that put `הבלוק` at ~680 m and made it look no better than `שכונה ג`.
A street centroid is not where a flat is: `אברהם אבינו` is long, and its midpoint lies well
outside the part of it that is inside הבלוק. Only a survey answers this.
"""
from __future__ import annotations
import itertools
import json
import re
import statistics
import sys
from pathlib import Path

import config
from load_zone_from_kmz import read_kml_text          # same KMZ/KML unwrapping

OUT_PATH = config.DATA_DIR.parent / "landmarks.json"


def _placemarks(kml: str):
    """(name, [(lat, lon), …]) per <Placemark>.

    Parsed placemark by placemark, NOT as two flat lists of names and coordinate
    blocks: a My Maps export also carries the layer name and per-style <name> tags, so
    zipping the two would pair every polygon with the wrong label."""
    for pm in re.findall(r"<Placemark>(.*?)</Placemark>", kml, re.S):
        name = re.search(r"<name>(.*?)</name>", pm, re.S)
        coords = re.search(r"<coordinates>(.*?)</coordinates>", pm, re.S)
        if not (name and coords):
            continue
        pts = []
        for tok in coords.group(1).split():
            lon, lat, *_ = tok.split(",")
            pts.append([round(float(lat), 7), round(float(lon), 7)])
        if len(pts) >= 3:                              # a polygon, not a pin or a line
            yield name.group(1).strip(), pts


def _extent_m(pts) -> float:
    """The widest distance across the shape — its diameter, not its perimeter."""
    import zones
    return max(zones._haversine_m(a[0], a[1], b[0], b[1])
               for a, b in itertools.combinations(pts, 2))


def build(path: Path) -> dict:
    out = {}
    for name, pts in _placemarks(read_kml_text(path)):
        out[name] = {
            "polygon_latlon": pts,
            "centroid": [round(statistics.mean(p[0] for p in pts), 7),
                         round(statistics.mean(p[1] for p in pts), 7)],
            "extent_m": round(_extent_m(pts), 1),
            "source": path.name,
        }
    return out


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    path = Path(sys.argv[1])
    data = build(path)
    if not data:
        print("No named polygons found. Name each shape in My Maps before exporting.")
        sys.exit(1)

    import geocode
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {OUT_PATH} with {len(data)} landmark(s):")
    for name, d in sorted(data.items(), key=lambda kv: kv[1]["extent_m"]):
        grade = geocode._landmark_grade(d["extent_m"])
        print(f"  {name:14} {len(d['polygon_latlon']):2} pts · {d['extent_m']:6.0f} m "
              f"across · grades {grade}")
    print("\nRun `python replay.py` (dry) to see which listings move, then --apply.")


if __name__ == "__main__":
    main()
