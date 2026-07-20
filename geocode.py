"""
Geocoding for the BGU area.

Primary: a hand-maintained lookup table. For a bounded area this is far more
accurate and reliable than live geocoding of messy Hebrew addresses, and it
never rate-limits. Fill STATIC_TABLE from your green-area map.

If a location is unknown we return None and let the pipeline flag it
NEEDS_DATA — we never emit a guessed coordinate, because a wrong point means
a wrong walk time (and a false match or a wrong drop).
"""
from __future__ import annotations
import json
import os
import re
import time
from typing import Optional, Tuple

import config

# An address is "precise" if it names a specific street or house number — as
# opposed to a bare neighborhood ("שכונה ג"), which covers a whole area and so
# can't be trusted as GREEN (see the amber cap in pipeline).
_STREET_WORDS = ("רחוב", "רח'", "רח׳", "שדרות", "שד'", "שד׳", "דרך", "סמטת",
                 "סמטה", "שביל")


def is_precise_address(s: Optional[str]) -> bool:
    if not s:
        return False
    if any(ch.isdigit() for ch in s):        # a house number
        return True
    return any(w in s for w in _STREET_WORDS)


def is_bare_neighborhood(s: Optional[str]) -> bool:
    """A whole-neighborhood location with no specific street ("שכונה ג")."""
    if not s or ("שכונה" not in s and "שכונת" not in s):
        return False
    return not is_precise_address(s)

# name (as it tends to appear in posts) -> (lat, lon)
# Seed values below are ILLUSTRATIVE placeholders near BGU — replace/extend
# with your real green-area list. Keys are matched by normalized substring,
# so "רסקו" will match a post that says "גר ברסקו ליד האוניברסיטה".
STATIC_TABLE: dict[str, Tuple[float, float]] = {
    # Keys are BARE tokens (no "רחוב", no house number) so a post saying
    # "רינגלבלום 5" or "גר ברינגלבלום" still matches. Coordinates are a point
    # INSIDE that area — replace/extend with your own.
    "רינגלבלום": (31.2668, 34.7987),   # OSM: the actual Ringelblum street (was ~700m off)
    "שכונה ג": (31.25507, 34.80471),    # whole-neighborhood centroid (spans the zone
                                        # boundary; centroid is GREEN, ~14 min walk)
    "שכונה ד": (31.2635, 34.7975),
    "שכונה ב": (31.2585, 34.7950),
    "שכונה ו": (31.2625, 34.7990),
    "וינגייט": (31.2600, 34.8015),
    # "הבלוק" — student-building cluster, GREEN zone, ~8 min to שער סורוקה.
    # Both forms so it matches whether the model writes "הבלוק" or "בבלוק".
    "הבלוק": (31.259386, 34.796130),
    "בבלוק": (31.259386, 34.796130),
    # -------------------------------------------
}


def _normalize(text: str) -> str:
    return (text or "").replace("״", "").replace("׳", "").strip().lower()


# --- persistent cache: each distinct location string is resolved (and billed)
# once, then remembered across runs. Only successful hits are cached, so an
# as-yet-unresolvable name is retried later (e.g. after you add it to the table).
_CACHE_PATH = config.DATA_DIR / "geocode_cache.json"
_cache: Optional[dict] = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        _CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False, indent=0),
                               encoding="utf-8")
    except Exception:
        pass


def geocode(location_text: Optional[str]) -> Optional[Tuple[float, float]]:
    """Return (lat, lon) or None. Order: static table -> cache -> Google -> Nominatim.
    A guessed point is never emitted — unknown locations return None so the
    pipeline flags NEEDS_DATA rather than inventing a wrong coordinate."""
    if not location_text:
        return None
    norm = _normalize(location_text)

    # 1) static table: substring match in either direction (instant, exact)
    for key, coords in STATIC_TABLE.items():
        k = _normalize(key)
        if k and (k in norm or norm in k):
            return coords

    # 2) cache of earlier external lookups
    cache = _load_cache()
    if cache.get(norm):
        return tuple(cache[norm])

    # 3) external geocoders, most accurate first
    coords = None
    if _google_enabled():
        coords = _google(location_text)
    if coords is None and config.USE_NOMINATIM_FALLBACK:
        coords = _nominatim(location_text)

    if coords:
        cache[norm] = list(coords)
        _save_cache()
    return coords


# --- Google Maps geocoding (optional; see config.USE_GOOGLE_GEOCODE) -----------
def _google_key() -> Optional[str]:
    return os.environ.get("GOOGLE_MAPS_API_KEY")


def _google_enabled() -> bool:
    return bool(getattr(config, "USE_GOOGLE_GEOCODE", False) and _google_key())


def _bs_bounds() -> Tuple[float, float, float, float]:
    """Be'er Sheva box as (lat_min, lon_min, lat_max, lon_max), parsed from the
    Nominatim-ordered viewbox 'lon_left,lat_top,lon_right,lat_bottom'."""
    lon_l, lat_t, lon_r, lat_b = (float(x) for x in config.BEER_SHEVA_VIEWBOX.split(","))
    return min(lat_t, lat_b), min(lon_l, lon_r), max(lat_t, lat_b), max(lon_l, lon_r)


def _in_beer_sheva(lat: float, lon: float) -> bool:
    la0, lo0, la1, lo1 = _bs_bounds()
    return la0 <= lat <= la1 and lo0 <= lon <= lo1


def _google(location_text: str) -> Optional[Tuple[float, float]]:
    """Geocoding API for real addresses; Places text-search for slang/POI names
    (e.g. 'הבלוק'). Both are constrained to the Be'er Sheva box so a same-named
    street/place elsewhere can't leak in."""
    return _google_geocode(location_text) or _google_places(location_text)


def _google_geocode(location_text: str) -> Optional[Tuple[float, float]]:
    import requests
    la0, lo0, la1, lo1 = _bs_bounds()
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={
                "address": f"{location_text}, באר שבע",
                "key": _google_key(),
                "language": "he",
                "region": "il",
                "components": "country:IL",
                "bounds": f"{la0},{lo0}|{la1},{lo1}",
            },
            timeout=15,
        )
        r.raise_for_status()
        for res in r.json().get("results", []):
            loc = res["geometry"]["location"]
            if _in_beer_sheva(loc["lat"], loc["lng"]):
                return loc["lat"], loc["lng"]
    except Exception:
        pass
    return None


def _google_places(location_text: str) -> Optional[Tuple[float, float]]:
    import requests
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={
                "query": f"{location_text} באר שבע",
                "key": _google_key(),
                "language": "he",
                "region": "il",
            },
            timeout=15,
        )
        r.raise_for_status()
        for res in r.json().get("results", []):
            loc = res["geometry"]["location"]
            if _in_beer_sheva(loc["lat"], loc["lng"]):
                return loc["lat"], loc["lng"]
    except Exception:
        pass
    return None


def _nominatim(location_text: str) -> Optional[Tuple[float, float]]:
    import requests

    try:
        time.sleep(1.1)  # policy: max ~1 req/sec
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": f"{location_text}, באר שבע",
                "format": "json",
                "limit": 1,
                # Hard-constrain to a Be'er Sheva bounding box. Without this,
                # Nominatim happily returns a same-named street in another city
                # (a "יעקב כהן" 30km south geocoded far outside the zone and got
                # falsely dropped). bounded=1 makes the viewbox a filter, not a
                # hint; countrycodes=il is a cheap extra guard.
                "viewbox": config.BEER_SHEVA_VIEWBOX,
                "bounded": 1,
                "countrycodes": "il",
            },
            headers={"User-Agent": config.NOMINATIM_USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None
