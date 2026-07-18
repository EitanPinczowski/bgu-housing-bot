"""
Walking time to campus via a locally self-hosted OSRM foot-routing server.

We query every gate and return the MINIMUM walk, because a flat near one gate
can be far from another. This is the ONLY place we convert to OSRM's (lon,lat)
coordinate order.
"""
from __future__ import annotations
from typing import Optional, Tuple

import requests

import config


def _foot_minutes(lat: float, lon: float, gate: dict) -> Optional[float]:
    # OSRM wants lon,lat  ->  {src_lon},{src_lat};{dst_lon},{dst_lat}
    coords = f"{lon},{lat};{gate['lon']},{gate['lat']}"
    url = f"{config.OSRM_BASE_URL}/route/v1/foot/{coords}"
    try:
        r = requests.get(url, params={"overview": "false"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("code") == "Ok" and data.get("routes"):
            return data["routes"][0]["duration"] / 60.0
    except Exception:
        return None
    return None


def walk_to_nearest(lat: Optional[float], lon: Optional[float]
                    ) -> Tuple[Optional[float], Optional[str]]:
    """(minutes, gate name) for the CLOSEST configured gate, or (None, None).
    The gate name (config.GATES[...]["name"], else the key) lets the alert say
    which gate the walk time is to."""
    if lat is None or lon is None:
        return None, None
    best_min, best_name = None, None
    for key, g in config.GATES.items():
        m = _foot_minutes(lat, lon, g)
        if m is not None and (best_min is None or m < best_min):
            best_min, best_name = m, g.get("name", key)
    return best_min, best_name


def walk_minutes(lat: Optional[float], lon: Optional[float]) -> Optional[float]:
    """Minimum walking minutes to the nearest configured gate, or None."""
    return walk_to_nearest(lat, lon)[0]
