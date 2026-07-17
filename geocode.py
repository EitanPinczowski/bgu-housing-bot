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
import time
from typing import Optional, Tuple

import config

# name (as it tends to appear in posts) -> (lat, lon)
# Seed values below are ILLUSTRATIVE placeholders near BGU — replace/extend
# with your real green-area list. Keys are matched by normalized substring,
# so "רסקו" will match a post that says "גר ברסקו ליד האוניברסיטה".
STATIC_TABLE: dict[str, Tuple[float, float]] = {
    # Keys are BARE tokens (no "רחוב", no house number) so a post saying
    # "רינגלבלום 5" or "גר ברינגלבלום" still matches. Coordinates are a point
    # INSIDE that area — replace/extend with your own.
    "רינגלבלום": (31.2601, 34.7980),
    "שכונה ג": (31.2610, 34.7960),
    "שכונה ד": (31.2635, 34.7975),
    "שכונה ב": (31.2585, 34.7950),
    "שכונה ו": (31.2625, 34.7990),
    "וינגייט": (31.2600, 34.8015),
    # -------------------------------------------
}


def _normalize(text: str) -> str:
    return (text or "").replace("״", "").replace("׳", "").strip().lower()


def geocode(location_text: Optional[str]) -> Optional[Tuple[float, float]]:
    """Return (lat, lon) or None. Static table first; optional Nominatim last."""
    if not location_text:
        return None
    norm = _normalize(location_text)

    # 1) static table: substring match in either direction
    for key, coords in STATIC_TABLE.items():
        k = _normalize(key)
        if k and (k in norm or norm in k):
            return coords

    # 2) optional Nominatim fallback (off by default; poor on Be'er Sheva)
    if config.USE_NOMINATIM_FALLBACK:
        return _nominatim(location_text)

    return None


def _nominatim(location_text: str) -> Optional[Tuple[float, float]]:
    import requests

    try:
        time.sleep(1.1)  # policy: max ~1 req/sec
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{location_text}, באר שבע", "format": "json", "limit": 1},
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
