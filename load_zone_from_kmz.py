"""
Regenerate green_zone.json from a Google My Maps KMZ/KML export.

Usage:
    python load_zone_from_kmz.py path\\to\\Untitled_layer.kmz

Draw the in-range area as a single shape/line in My Maps, export
"KML/KMZ" (entire map), and point this script at the file.
"""
from __future__ import annotations
import json
import re
import sys
import zipfile
from pathlib import Path

import config


def read_kml_text(path: Path) -> str:
    if path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.endswith(".kml"))
            return z.read(name).decode("utf-8")
    return path.read_text(encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    path = Path(sys.argv[1])
    kml = read_kml_text(path)

    block = re.search(r"<coordinates>(.*?)</coordinates>", kml, re.S)
    if not block:
        print("No <coordinates> found. Draw a shape/line before exporting.")
        sys.exit(1)

    poly = []
    for tok in block.group(1).split():
        lon, lat, *_ = tok.split(",")
        poly.append([round(float(lat), 7), round(float(lon), 7)])

    out = {"name": "green_zone", "source": path.name, "polygon_latlon": poly}
    Path(config.GREEN_ZONE_PATH).write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {config.GREEN_ZONE_PATH} with {len(poly)} points.")


if __name__ == "__main__":
    main()
