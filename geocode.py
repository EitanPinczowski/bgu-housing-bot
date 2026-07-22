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
from datetime import datetime, timedelta
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


# Minimum length of a location string for the REVERSE static-table match (the post
# text being a fragment of a longer table key). Below this, a stray token like "ג"
# would false-match a whole neighborhood — so short strings only match FORWARD.
_MIN_REVERSE_MATCH = 4


def _normalize(text: str) -> str:
    return (text or "").replace("״", "").replace("׳", "").strip().lower()


# --- persistent cache: each distinct location string is resolved (and billed)
# once, then remembered across runs. We cache successes AND negative results (with a
# TTL) — a miss is expensive now that Overpass is in the chain (~1s/mirror), so an
# unresolvable name shouldn't be re-queried every run. The static table is always
# checked FIRST, so pinning a name resolves it immediately even if a miss was cached.
# Cache value shapes:  {"c": [lat, lon], "s": <source>}  |  {"m": <iso-ts>}  |
# a bare [lat, lon] list (legacy successes written before this change).
_CACHE_PATH = config.DATA_DIR / "geocode_cache.json"
_MISS_TTL_DAYS = 7
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


def _cache_lookup(norm: str):
    """('hit', coords, source) for a cached success, ('miss', None, None) for a
    negative result still within its TTL, or ('none', None, None) — meaning nothing
    usable, so go query (an expired miss falls here and is re-tried)."""
    v = _load_cache().get(norm)
    if isinstance(v, list) and len(v) == 2:                 # legacy success
        return "hit", (v[0], v[1]), "cache"
    if isinstance(v, dict):
        if "c" in v:
            return "hit", (v["c"][0], v["c"][1]), v.get("s", "cache")
        if "m" in v:
            try:
                fresh = datetime.now() - datetime.fromisoformat(v["m"]) < timedelta(days=_MISS_TTL_DAYS)
            except Exception:
                fresh = False
            if fresh:
                return "miss", None, None
    return "none", None, None


def geocode(location_text: Optional[str]) -> Optional[Tuple[float, float]]:
    """Return (lat, lon) or None (see geocode_detailed). A guessed point is never
    emitted — unknown locations return None so the pipeline flags NEEDS_DATA."""
    return geocode_detailed(location_text)[0]


def geocode_detailed(location_text: Optional[str]):
    """(coords, source) or (None, None). source ∈
    static/cache/google/overpass/nominatim — which tier resolved the name, so a
    lower-confidence hit (overpass/nominatim) can be flagged for a human check.
    Order: static table -> cache -> Google -> Overpass -> Nominatim."""
    if not location_text:
        return None, None
    norm = _normalize(location_text)

    # 1) static table: substring match. FORWARD (the table key appears inside the
    #    post text) is always safe — "רינגלבלום" in "גר ברינגלבלום ליד האוני'".
    #    REVERSE (the post text is a fragment of a longer key) is only trusted for a
    #    long-enough fragment, so a stray 1–2 char location ("ג", "ד") can't map onto
    #    a whole-neighborhood centroid and invent a wrong coordinate.
    for key, coords in STATIC_TABLE.items():
        k = _normalize(key)
        if not k:
            continue
        if k in norm or (len(norm) >= _MIN_REVERSE_MATCH and norm in k):
            return coords, "static"

    # 2) cache of earlier lookups (success or a still-fresh miss)
    kind, coords, source = _cache_lookup(norm)
    if kind == "hit":
        return coords, source
    if kind == "miss":
        return None, None                                   # recent negative — don't re-query

    # 3) external geocoders, most accurate first
    coords = source = None
    if _google_enabled():
        coords, source = _google(location_text), "google"
    if coords is None and getattr(config, "USE_OVERPASS_FALLBACK", True):
        coords, source = _overpass(location_text), "overpass"
    if coords is None and config.USE_NOMINATIM_FALLBACK:
        coords, source = _nominatim(location_text), "nominatim"

    cache = _load_cache()
    if coords:
        cache[norm] = {"c": [coords[0], coords[1]], "s": source}
        _save_cache()
        return coords, source
    cache[norm] = {"m": datetime.now().isoformat(timespec="seconds")}   # remember the miss
    _save_cache()
    return None, None


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


# Strip house numbers and street-type words so the query matches the OSM `name`
# tag of the street itself ("רחוב רינגלבלום 5" -> "רינגלבלום").
_OVERPASS_STRIP = re.compile(r"\d+|רחוב|רח['׳]|שדרות|שד['׳]|דרך|סמטת|סמטה|שביל")


def _overpass_name(location_text: str) -> str:
    s = _OVERPASS_STRIP.sub(" ", location_text)
    s = s.translate(str.maketrans("", "", '"\\')).strip()   # keep the QL string safe
    return re.sub(r"\s+", " ", s)


def _overpass(location_text: str) -> Optional[Tuple[float, float]]:
    """Resolve a Be'er Sheva street/place name via the free public Overpass API.
    OSM's `name` index resolves many Hebrew street names Nominatim returns nothing
    for (see the geocode memory note). Bounded to the BS box; first hit wins. Paced
    ~1 req/s to be polite to the shared instance; failures return None (→ Nominatim)."""
    import requests

    name = _overpass_name(location_text)
    if len(name) < _MIN_REVERSE_MATCH:
        return None
    la0, lo0, la1, lo1 = _bs_bounds()
    bbox = f"{la0},{lo0},{la1},{lo1}"                       # Overpass: S,W,N,E
    # Ask for named streets (highways) AND any named node/way; we rank client-side so
    # a real road wins over an unrelated POI that happens to share the name.
    q = (f'[out:json][timeout:25];'
         f'(way["highway"]["name"~"{name}"]({bbox});'
         f'way["name"~"{name}"]({bbox});'
         f'node["name"~"{name}"]({bbox}););'
         f'out center tags 20;')
    timeout = getattr(config, "OVERPASS_TIMEOUT_SEC", 15)
    for url in config.OVERPASS_URLS:                        # first mirror that responds wins
        try:
            time.sleep(1.0)                                # be polite to the shared instance
            r = requests.post(url, data={"data": q},
                              headers={"User-Agent": config.NOMINATIM_USER_AGENT}, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception:
            continue                                       # this mirror timed out — try the next
        # A valid response is authoritative (OSM data is identical across mirrors):
        # take the best-ranked in-box hit, or None — never keep hammering other mirrors.
        return _overpass_pick(data.get("elements", []), name)
    return None


def _overpass_pick(elements: list, name: str) -> Optional[Tuple[float, float]]:
    """Choose the best in-box element: prefer an exact-name match, then an actual
    street (highway), over a generic named node/way — so a street name resolves to
    the road, not a same-named shop or point."""
    def rank(el) -> tuple:
        tags = el.get("tags", {}) or {}
        return (tags.get("name", "") == name, "highway" in tags)   # higher tuple = better

    best = None
    for el in sorted(elements, key=rank, reverse=True):
        c = el.get("center") or el                         # ways carry a computed center
        lat, lon = c.get("lat"), c.get("lon")
        if lat is not None and lon is not None and _in_beer_sheva(float(lat), float(lon)):
            best = (float(lat), float(lon))
            break
    return best


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
