"""
The green (in-range) zone you drew in Google My Maps.

`green_zone.json` holds the polygon as [lat, lon] points. Location is graded in
three tiers:
  GREEN  = inside the polygon                      -> preferred
  AMBER  = outside, but within BUFFER_METERS of it -> acceptable, not preferred
  RED    = beyond the buffer                        -> dropped
To update the zone later, re-draw it in My Maps, export a new KMZ, and run:

    python load_zone_from_kmz.py path\\to\\NewLayer.kmz
"""
from __future__ import annotations
import json
import math
from functools import lru_cache
from typing import Optional

import config

_R = 6371000.0  # earth radius, metres


@lru_cache(maxsize=1)
def _polygon() -> list[tuple[float, float]]:
    with open(config.GREEN_ZONE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return [(lat, lon) for lat, lon in data["polygon_latlon"]]


@lru_cache(maxsize=1)
def _lat0() -> float:
    poly = _polygon()
    return sum(p[0] for p in poly) / len(poly)


@lru_cache(maxsize=1)
def _no_amber_polys() -> list:
    """Neighborhood polygons (e.g. שכונה ד') where the 500m amber buffer does NOT
    apply — outside the green polygon there is red. From no_amber_zones.json."""
    try:
        with open(config.NO_AMBER_ZONES_PATH, encoding="utf-8") as f:
            return [z["polygon_latlon"] for z in json.load(f).get("zones", [])]
    except Exception:
        return []


def _point_in(lat: float, lon: float, poly: list) -> bool:
    x, y = lon, lat
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if ((xi > x) != (xj > x)) and (y < (yj - yi) * (x - xi) / (xj - xi) + yi):
            inside = not inside
        j = i
    return inside


def in_no_amber_zone(lat: Optional[float], lon: Optional[float]) -> bool:
    if lat is None or lon is None:
        return False
    return any(_point_in(lat, lon, p) for p in _no_amber_polys())


def _to_xy(lat: float, lon: float) -> tuple[float, float]:
    """Local equirectangular projection to metres (accurate over a few km)."""
    x = math.radians(lon) * _R * math.cos(math.radians(_lat0()))
    y = math.radians(lat) * _R
    return x, y


def in_green_zone(lat: Optional[float], lon: Optional[float]) -> bool:
    """Ray-casting point-in-polygon. False if no coordinate."""
    if lat is None or lon is None:
        return False
    poly = _polygon()
    x, y = lon, lat
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if ((xi > x) != (xj > x)) and (y < (yj - yi) * (x - xi) / (xj - xi) + yi):
            inside = not inside
        j = i
    return inside


def _dist_point_to_polygon_m(lat: float, lon: float) -> float:
    """Minimum distance in metres from the point to the polygon boundary."""
    px, py = _to_xy(lat, lon)
    poly_xy = [_to_xy(la, lo) for la, lo in _polygon()]
    best = float("inf")
    n = len(poly_xy)
    for i in range(n):
        ax, ay = poly_xy[i]
        bx, by = poly_xy[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        cx, cy = ax + t * dx, ay + t * dy
        best = min(best, math.hypot(px - cx, py - cy))
    return best


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _R * math.asin(math.sqrt(a))


def est_walk_to_gate_min(lat: float, lon: float) -> float:
    """Estimated walk time (minutes) to the NEAREST campus gate, from straight-
    line distance × detour ÷ speed. Used for the map and as the OSRM-down
    fallback; real listings pass the actual OSRM walk time to classify_location."""
    nearest = min(_haversine_m(lat, lon, g["lat"], g["lon"]) for g in config.GATES.values())
    return nearest * config.WALK_DETOUR_FACTOR / config.WALK_SPEED_M_PER_MIN


def classify_location(lat: Optional[float], lon: Optional[float],
                      walk_min: Optional[float] = None) -> str:
    """'GREEN' | 'AMBER' | 'RED' | 'UNKNOWN'. GREEN = inside the hand-drawn
    polygon (preferred). Otherwise AMBER if the walk to the nearest gate is within
    MAX_WALK_MINUTES, else RED. Pass the real OSRM walk_min when you have it; when
    it's None (map cells, OSRM down) a straight-line estimate is used instead."""
    if lat is None or lon is None:
        return "UNKNOWN"
    if in_green_zone(lat, lon):
        return "GREEN"
    wmin = walk_min if walk_min is not None else est_walk_to_gate_min(lat, lon)
    return "AMBER" if wmin <= config.MAX_WALK_MINUTES else "RED"


def classify_effective(lat: Optional[float], lon: Optional[float],
                       walk_min: Optional[float] = None) -> str:
    """classify_location, but with the no-amber rule applied: an AMBER point that
    falls inside a no-amber neighborhood (e.g. שכונה ד') becomes RED. This is the
    tier the pipeline and the map both use."""
    t = classify_location(lat, lon, walk_min)
    if t == "AMBER" and in_no_amber_zone(lat, lon):
        return "RED"
    return t
