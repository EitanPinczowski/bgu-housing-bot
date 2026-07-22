"""
Walking time to campus via a locally self-hosted OSRM foot-routing server.

We query every gate and return the MINIMUM walk, because a flat near one gate
can be far from another. This is the ONLY place we convert to OSRM's (lon,lat)
coordinate order.
"""
from __future__ import annotations
import time
from typing import Optional, Tuple

import requests

import config

# Circuit breaker: probe OSRM ONCE per process; if it's down, skip it for every
# listing (fall back to the straight-line walk estimate in zones) instead of paying a
# multi-second retry per gate per listing — a down OSRM must not turn a replay/run into
# an hours-long crawl. `osrm_down` is exposed so a run can report it (see #41 metrics).
_alive: Optional[bool] = None
osrm_down = False


def _alive_check() -> bool:
    """True if OSRM answered a quick probe (cached for the process). One short,
    no-retry request so a dead server costs ~2s total, not 2s × every listing."""
    global _alive, osrm_down
    if _alive is None:
        try:
            r = requests.get(f"{config.OSRM_BASE_URL}/route/v1/foot/34.8,31.25;34.8015,31.262",
                             params={"overview": "false"}, timeout=3)
            _alive = r.status_code == 200 and r.json().get("code") == "Ok"
        except Exception:
            _alive = False
        osrm_down = not _alive
    return _alive


def _foot_minutes(lat: float, lon: float, gate: dict, tries: int = 3) -> Optional[float]:
    # OSRM wants lon,lat  ->  {src_lon},{src_lat};{dst_lon},{dst_lat}
    coords = f"{lon},{lat};{gate['lon']},{gate['lat']}"
    url = f"{config.OSRM_BASE_URL}/route/v1/foot/{coords}"
    # A transient blip (server busy / momentary network) here silently drops the walk
    # time and can misclassify a listing, so retry a couple of times with backoff
    # before giving up. A real "no route" (code != Ok) is returned immediately.
    for i in range(tries):
        try:
            r = requests.get(url, params={"overview": "false"}, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == "Ok" and data.get("routes"):
                return data["routes"][0]["duration"] / 60.0
            return None                                    # answered, but no route — don't retry
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(0.5 * (2 ** i))                     # 0.5s, 1s backoff
    return None


def walk_to_nearest(lat: Optional[float], lon: Optional[float]
                    ) -> Tuple[Optional[float], Optional[str]]:
    """(minutes, gate name) for the CLOSEST configured gate, or (None, None).
    The gate name (config.GATES[...]["name"], else the key) lets the alert say
    which gate the walk time is to."""
    if lat is None or lon is None or not _alive_check():
        return None, None                              # no coord, or OSRM down → straight-line
    best_min, best_name = None, None
    for key, g in config.GATES.items():
        m = _foot_minutes(lat, lon, g)
        if m is not None and (best_min is None or m < best_min):
            best_min, best_name = m, g.get("name", key)
    return best_min, best_name


def walk_minutes(lat: Optional[float], lon: Optional[float]) -> Optional[float]:
    """Minimum walking minutes to the nearest configured gate, or None."""
    return walk_to_nearest(lat, lon)[0]
