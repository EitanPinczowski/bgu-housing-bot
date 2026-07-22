"""
Walking time to campus via a locally self-hosted OSRM foot-routing server.

We query every gate and return the MINIMUM walk, because a flat near one gate
can be far from another. This is the ONLY place we convert to OSRM's (lon,lat)
coordinate order.
"""
from __future__ import annotations
import json
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


# Persistent walk-time cache, keyed on the rounded coordinate. Routing is the slow part
# of a replay/map rebuild (geocode is cached but walk-times weren't); reuse them across
# runs. ~4-decimal rounding ≈ 11 m — plenty for a walk-minute figure.
_WALK_CACHE_PATH = config.DATA_DIR / "walk_cache.json"
_walk_cache: Optional[dict] = None


def _load_walk_cache() -> dict:
    global _walk_cache
    if _walk_cache is None:
        try:
            _walk_cache = json.loads(_WALK_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _walk_cache = {}
    return _walk_cache


def _save_walk_cache() -> None:
    try:
        _WALK_CACHE_PATH.write_text(json.dumps(_walk_cache, ensure_ascii=False),
                                    encoding="utf-8")
    except Exception:
        pass


def _table_walk(lat: float, lon: float, tries: int = 3):
    """(minutes, gate name) via ONE OSRM /table call — source × all gates in a single
    request instead of a /route per gate — taking the nearest. (None, None) on failure."""
    gates = list(config.GATES.items())
    # OSRM wants lon,lat; point 0 = the listing, points 1..N = the gates
    coords = f"{lon},{lat}" + "".join(f";{g['lon']},{g['lat']}" for _, g in gates)
    url = f"{config.OSRM_BASE_URL}/table/v1/foot/{coords}"
    for i in range(tries):
        try:
            r = requests.get(url, params={"sources": "0", "annotations": "duration"}, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != "Ok" or not data.get("durations"):
                return None, None                      # answered, no table — don't retry
            durs = data["durations"][0][1:]            # source 0 -> each gate (skip self at 0)
            best = min((d for d in durs if d is not None), default=None)
            if best is None:
                return None, None
            gk, gv = gates[durs.index(best)]
            return best / 60.0, gv.get("name", gk)
        except Exception:
            if i == tries - 1:
                return None, None
            time.sleep(0.5 * (2 ** i))
    return None, None


def walk_to_nearest(lat: Optional[float], lon: Optional[float]
                    ) -> Tuple[Optional[float], Optional[str]]:
    """(minutes, gate name) for the CLOSEST configured gate, or (None, None) — cached
    per rounded coordinate, one OSRM /table call on a miss. The gate name lets the alert
    say which gate the walk is to."""
    if lat is None or lon is None:
        return None, None
    cache = _load_walk_cache()
    key = f"{round(lat, 4)},{round(lon, 4)}"
    if key in cache:
        v = cache[key]
        return (v[0], v[1]) if v else (None, None)
    if not _alive_check():
        return None, None                              # OSRM down → straight-line (don't cache)
    minutes, gate = _table_walk(lat, lon)
    if minutes is not None:
        cache[key] = [minutes, gate]                   # cache successes only
        _save_walk_cache()
    return minutes, gate


def walk_minutes(lat: Optional[float], lon: Optional[float]) -> Optional[float]:
    """Minimum walking minutes to the nearest configured gate, or None."""
    return walk_to_nearest(lat, lon)[0]
