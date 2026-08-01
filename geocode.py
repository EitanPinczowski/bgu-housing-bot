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
import statistics
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

import config
import streets

# An address is "precise" if it names a specific street or house number — as
# opposed to a bare neighborhood ("שכונה ג"), which covers a whole area and so
# can't be trusted as GREEN (see the amber cap in pipeline).
_STREET_WORDS = ("רחוב", "רח'", "רח׳", "שדרות", "שד'", "שד׳", "דרך", "סמטת",
                 "סמטה", "שביל", "רחבת", "כיכר", "משעול")


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


def is_bare_street(s: Optional[str]) -> bool:
    """A street/area with NO house number — a line, not a point ("אברהם אבינו",
    "רחוב הנדיב"). False for a numbered address ("אברהם אבינו 60") and for a bare
    neighborhood ("שכונה ג"). Used to cap an imprecise GREEN to AMBER."""
    if not s or is_bare_neighborhood(s):
        return False
    return not any(ch.isdigit() for ch in s)


# Which geocoders give a PRECISE point (a specific place / house number) vs a
# street-LEVEL point that only says "somewhere on this street". A street-level point
# can't be trusted as GREEN on a boundary-crossing street (see pipeline).
# "manual" = placed by hand on the dashboard map. It outranks everything: a person
# looked at the map and said "the flat is here", which is strictly better evidence
# than any tier below.
_PRECISE_SOURCES = {"static", "google", "osm_addr", "interpolated", "manual"}

# How much to trust a point, by the tier that produced it:
#   exact  — a specific pinned place or an OSM house node
#   high   — interpolated between known house numbers on the street
#   street — somewhere on the right street (a line, not a point)
#   area   — a whole neighborhood centroid
_CONFIDENCE = {"manual": "exact", "static": "exact", "google": "exact",
               "osm_addr": "exact", "interpolated": "high",
               # a projection past/around the known anchors: much better than the
               # street centroid it replaces, but deliberately absent from
               # _PRECISE_SOURCES so the boundary-street caution still applies
               "extrapolated": "high",
               "overpass": "street", "nominatim": "street"}


def confidence(source: Optional[str]) -> str:
    """'exact' | 'high' | 'street' | 'area' | 'none' for a geocode source."""
    if not source:
        return "none"
    return _CONFIDENCE.get(source, "street")


def is_precise_source(source: Optional[str]) -> bool:
    return source in _PRECISE_SOURCES


# --- boundary streets: streets whose OSM geometry crosses the in-range↔RED line, so a
# name-only (imprecise) placement on them can't be trusted GREEN. Built by
# load_boundary_streets.py; matched by name substring against the address text. -------
_boundary_streets: Optional[set] = None


def _load_boundary_streets() -> set:
    global _boundary_streets
    if _boundary_streets is None:
        try:
            data = json.loads((config.ROOT / "boundary_streets.json").read_text(encoding="utf-8"))
            _boundary_streets = {_normalize(s) for s in data.get("streets", []) if s}
        except Exception:
            _boundary_streets = set()
    return _boundary_streets


def is_boundary_street(address: Optional[str]) -> bool:
    """True if the address is on a known boundary-crossing street (its name appears in
    the address text). Empty set (no file) → False, so the feature is simply off."""
    if not address:
        return False
    norm = _normalize(address)
    return any(len(s) >= _MIN_REVERSE_MATCH and s in norm for s in _load_boundary_streets())

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
    # snapped onto the real OSM geometry 2026-07-30 — the old point was 520 m off the
    # street and answered every house number identically (audit_geocode.py catches this)
    "וינגייט": (31.255862, 34.804043),
    # "הבלוק" — student-building cluster, GREEN zone, ~8 min to שער סורוקה.
    # Both forms so it matches whether the model writes "הבלוק" or "בבלוק".
    "הבלוק": (31.259386, 34.796130),
    "בבלוק": (31.259386, 34.796130),
    # כיכר האבות — a known square at the south (campus) end of אברהם אבינו; GREEN
    # (inside the green zone, in ד). Pinned so a post that names it resolves HERE
    # instead of falling through to a coincidental match elsewhere in the address.
    "כיכר האבות": (31.26183, 34.79475),
    "כיכר אבות": (31.26183, 34.79475),
    # -------------------------------------------
}


# Minimum length of a location string for the REVERSE static-table match (the post
# text being a fragment of a longer table key). Below this, a stray token like "ג"
# would false-match a whole neighborhood — so short strings only match FORWARD.
_MIN_REVERSE_MATCH = 4


# Hebrew abbreviation marks. `_normalize` (below) DROPS them, which is right for
# comparing against our own tables — but the external geocoders never saw a normalized
# string, they got the raw address, and they don't understand these characters:
# measured 2026-07-30, `הכ״ג 5` resolved to nothing while `הכ"ג 5` resolved fine.
# Israeli street names are full of them (רד״ק, רמב״ם, הכ״ג, שד״ל), so fold them to
# their ASCII equivalents before anything leaves this module.
_QUOTE_FOLD = str.maketrans({"״": '"', "׳": "'", "“": '"', "”": '"', "‘": "'", "’": "'"})


def _fold_quotes(text: Optional[str]) -> str:
    """Hebrew gershayim/geresh (and curly quotes) -> ASCII, so an address written the
    normal Israeli way reaches Overpass/Nominatim in a form they can match."""
    return (text or "").translate(_QUOTE_FOLD)


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
# Bump whenever the resolution logic changes (new tokenizer, street index, …). A cached
# MISS from an older version is ignored, so an improvement takes effect immediately
# instead of waiting out the 7-day TTL on names it can now resolve.
GEOCODE_LOGIC_VERSION = 5      # 5: building-centroid anchors + 2D parity interpolation
_cache: Optional[dict] = None
misses = 0                    # geocode failures this process (a real name that didn't resolve) — for #41 run metrics


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


# --- user pins: coordinates you add by hand (or from Telegram /pin), merged into the
# static table so a recurring unmapped place resolves for good. -------------------
_USER_PINS_PATH = config.ROOT / "user_pins.json"
_user_pins: Optional[dict] = None


def _load_user_pins() -> dict:
    global _user_pins
    if _user_pins is None:
        try:
            raw = json.loads(_USER_PINS_PATH.read_text(encoding="utf-8"))
            _user_pins = {k: (v[0], v[1]) for k, v in raw.items()}
        except Exception:
            _user_pins = {}
    return _user_pins


def add_pin(name: str, lat: float, lon: float) -> str:
    """Add/replace a geocode pin (persisted to user_pins.json, merged into the static
    table with the same earliest-match logic). Returns the trimmed name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("empty pin name")
    pins = _load_user_pins()
    pins[name] = (float(lat), float(lon))
    _USER_PINS_PATH.write_text(json.dumps({k: [la, lo] for k, (la, lo) in pins.items()},
                                          ensure_ascii=False), encoding="utf-8")
    return name


def uncache(name: str) -> list:
    """Drop every cached entry whose key CONTAINS the given text (normalized), so a
    wrong pin (or a stale miss) can be cleared without hand-editing the JSON. The
    static table is unaffected and is re-checked first, so the name re-resolves on the
    next lookup. Returns the keys removed."""
    q = _normalize(name)
    if not q:
        return []
    cache = _load_cache()
    hit = [k for k in cache if q in k]
    for k in hit:
        del cache[k]
    if hit:
        _save_cache()
    return hit


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
            # A miss recorded by OLDER resolution logic is not trustworthy — retry it.
            if v.get("v", 1) < GEOCODE_LOGIC_VERSION:
                return "none", None, None
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


def geocode_cached(location_text: Optional[str]):
    """(lat, lon) from the static table / user pins / cache ONLY — never a network call.

    For read-only viewers (the dashboard, the maps) which run over hundreds of rows: a
    handful of unresolvable addresses would otherwise re-query the Overpass mirrors on
    every page build. Measured before this existed: 211 seconds to render 350 rows.
    A name that has never been resolved simply has no dot; the pipeline is what resolves
    names, not the viewer."""
    if not location_text:
        return None
    location_text = _fold_quotes(location_text)
    norm = _normalize(location_text)

    # Mirror geocode_detailed's precedence, minus the network tiers — including the
    # rules that keep it ACCURATE: a neighborhood centroid and a static street entry
    # both step aside for an address that carries a house number.
    precise = is_precise_address(location_text) and not is_bare_neighborhood(location_text)
    numbered = bool(_house_number(location_text))
    best_pos, best_coords, skipped_street = None, None, None
    for key, coords in list(STATIC_TABLE.items()) + list(_load_user_pins().items()):
        k = _normalize(key)
        if not k:
            continue
        if precise and (k.startswith("שכונה") or k.startswith("שכונת")):
            continue
        if numbered and streets.known(k) and k in norm:
            skipped_street = skipped_street or coords
            continue
        pos = norm.find(k)
        if pos != -1 and (best_pos is None or pos < best_pos):
            best_pos, best_coords = pos, coords
    if best_coords is not None:
        return best_coords

    hn = _house_number(location_text)
    if hn:                                    # local interpolation, no network
        for cand in _candidate_tokens(location_text)[:2]:
            real, _how = streets.canonical(cand)
            pt, _how = place_house(real or cand, hn)
            if pt:
                return pt

    entry = _load_cache().get(norm)
    if isinstance(entry, dict) and entry.get("c"):
        return tuple(entry["c"])
    if isinstance(entry, list) and len(entry) == 2:      # legacy bare [lat, lon]
        return tuple(entry)
    return skipped_street                     # street-level, better than no dot at all


def geocode_detailed(location_text: Optional[str]):
    """(coords, source) or (None, None). source ∈
    static/cache/google/overpass/nominatim — which tier resolved the name, so a
    lower-confidence hit (overpass/nominatim) can be flagged for a human check.
    Order: static table -> cache -> Google -> Overpass -> Nominatim."""
    if not location_text:
        return None, None
    # Fold Hebrew abbreviation marks once, here, so every tier below — the static
    # match, the street index, Overpass and Nominatim — sees the same ASCII form.
    location_text = _fold_quotes(location_text)
    norm = _normalize(location_text)

    # 1) static table: substring match. FORWARD (the table key appears inside the
    #    post text) is always safe — "רינגלבלום" in "גר ברינגלבלום ליד האוני'".
    #    REVERSE (the post text is a fragment of a longer key) is only trusted for a
    #    long-enough fragment, so a stray 1–2 char location ("ג", "ד") can't map onto
    #    a whole-neighborhood centroid and invent a wrong coordinate.
    #    When several keys match, prefer the one mentioned EARLIEST in the address (the
    #    primary location), so a trailing slang POI ("…כיכר האבות, הבלוק") can't override
    #    the real anchor. Reverse matches rank last.
    #    A whole-neighborhood centroid key ("שכונה ד") is SKIPPED when the address is a
    #    specific street ("רחוב האיסיים 5, שכונה ד") — the street must be geocoded to its
    #    real spot (Overpass), not to the neighborhood's green-zone centroid.
    #    A STREET key is likewise skipped once the address carries a HOUSE NUMBER:
    #    a street entry is one point for the whole street, so "וינגייט 74" and
    #    "וינגייט 16" were both answered with the same coordinate and interpolation
    #    never ran (measured 2026-07-30: that point was also 474 m off the real
    #    street). Slang/POI keys that aren't OSM street names still answer, and the
    #    skipped point is kept as `static_fallback` in case nothing better resolves,
    #    so this can only improve precision, never lose a placement.
    precise = is_precise_address(location_text) and not is_bare_neighborhood(location_text)
    numbered = bool(_house_number(location_text))
    best_pos, best_coords = None, None
    skipped_street_coords = None
    for key, coords in list(STATIC_TABLE.items()) + list(_load_user_pins().items()):
        k = _normalize(key)
        if not k:
            continue
        if precise and (k.startswith("שכונה") or k.startswith("שכונת")):
            continue                                        # don't let a nbhd centroid hijack a street
        if numbered and streets.known(k) and k in norm:
            skipped_street_coords = skipped_street_coords or coords
            continue                                        # let the house number win
        pos = norm.find(k)
        if pos != -1:                                       # forward: key inside the address
            if best_pos is None or pos < best_pos:
                best_pos, best_coords = pos, coords
        elif len(norm) >= _MIN_REVERSE_MATCH and norm in k and best_coords is None:
            best_pos, best_coords = 10 ** 6, coords         # reverse: lowest priority
    if best_coords is not None:
        return best_coords, "static"

    # 1b) house-number interpolation — local, free and more precise than any street-level
    #     hit: place the number between the known OSM address nodes on that street. Only
    #     fires for a numbered address on an anchored street, and never extrapolates.
    hn = _house_number(location_text)
    if hn:
        for cand in _candidate_tokens(location_text)[:2]:
            real, _how = streets.canonical(cand)
            pt, how = place_house(real or cand, hn)
            if pt:
                return pt, how

    # 2) cache of earlier lookups (success or a still-fresh miss)
    kind, coords, source = _cache_lookup(norm)
    if kind == "hit":
        return coords, source
    if kind == "miss":
        return None, None                                   # recent negative — don't re-query

    # 3) external geocoders, most accurate first
    coords = source = None
    authoritative = True          # only cache a MISS if we actually reached a geocoder,
                                  # so a network blackout doesn't suppress a good name
    if _google_enabled():
        coords, source = _google(location_text), "google"
    if coords is None and getattr(config, "USE_OVERPASS_FALLBACK", True):
        ocoords, osrc, responded = _overpass(location_text)
        authoritative = responded
        if ocoords and _plausible_external(location_text, ocoords, osrc):
            coords, source = ocoords, osrc          # 'osm_addr' (precise) or 'overpass'
    if coords is None and config.USE_NOMINATIM_FALLBACK:
        ncoords = _nominatim(location_text)
        # Retry on the CLEANED street + house number. Nominatim matches the whole
        # string, so trailing context defeats it exactly the way the city name did:
        # `הכ״ג 5` resolves but `רחוב הכ״ג 5, שכונה ג׳` did not, despite the tokenizer
        # already knowing the street. Only worth trying when it actually differs.
        if ncoords is None and hn:
            for cand in _candidate_tokens(location_text)[:2]:
                real, _how = streets.canonical(cand)
                probe = f"{real or cand} {hn}"
                if probe == location_text.strip():
                    continue
                ncoords = _nominatim(probe)
                if ncoords:
                    break
        if ncoords and _plausible_external(location_text, ncoords, "nominatim"):
            coords, source = ncoords, "nominatim"

    cache = _load_cache()
    if coords:
        cache[norm] = {"c": [coords[0], coords[1]], "s": source}
        _save_cache()
        return coords, source
    # Nothing more precise resolved, so fall back to the street-level static point we
    # skipped above. Street-level, not house-level, so it's reported as a LOW-precision
    # source and the boundary/edge rules stay cautious about it — but a listing that
    # used to be placed still gets placed.
    if skipped_street_coords is not None:
        return skipped_street_coords, "static_street"
    # Last resort: the post never gave a street, only a bearing off a landmark
    # ("ליד האוניברסיטה וסורוקה", "קרוב לאוניברסיטת בן גוריון"). Those are
    # campus-adjacent — i.e. in the zone — but were coming back UNKNOWN and dropped.
    # Runs LAST on purpose: as a static key, "האוניברסיטה" would hijack any address
    # merely mentioning the campus ("רגר 5, 5 דקות מהאוניברסיטה").
    lm = _descriptive_landmark(location_text)
    if lm:
        return lm, "landmark"                      # imprecise by design — see _CONFIDENCE
    global misses
    misses += 1                   # a real location string we couldn't map (for run metrics)
    if authoritative:             # a real not-found (a geocoder answered) — remember it
        cache[norm] = {"m": datetime.now().isoformat(timespec="seconds"),
                       "v": GEOCODE_LOGIC_VERSION}
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


# Strip house numbers (incl. a compound "13/6"), street-type words, and a trailing
# neighborhood ("…, שכונה ד") so the query matches the OSM `name` tag of the street itself
# ("רחוב רינגלבלום 5" -> "רינגלבלום", "רחוב האיסיים 5, שכונה ד" -> "האיסיים").
_OVERPASS_STRIP = re.compile(
    r"\d+(?:/\d+)?|שכונ[הת]\s*[א-י]?['׳]?|רחוב|רח['׳]|שדרות|שדרה|שד['׳]|דרך|סמטת|סמטה|שביל|רחבת|רחבה|כיכר|משעול")


def _overpass_name(location_text: str) -> str:
    s = _CITY_RE.sub(" ", location_text)                    # "…, באר שבע" pollutes the query
    s = _OVERPASS_STRIP.sub(" ", s)
    s = s.translate(str.maketrans("", "", '"\\/,'))         # keep the QL string safe; drop commas
    return re.sub(r"\s+", " ", s).strip()


# The city name in the address breaks the OSM name~ match: "רגר 179" resolves but
# "רחוב רגר 179, באר שבע" did not. Strip it (and the ב"ש abbreviations).
_CITY_RE = re.compile(r"באר\s*שבע|ב['\"׳״]ש\b|beer\s*sheva", re.IGNORECASE)
# An address often names several places: "שיפר, רינגבלום", "יוחנן הורקנוס/יטבתה",
# "ברגר פינת רינגלבלום". Split and try each part rather than gluing them into one token.
_SPLIT_RE = re.compile(r"[,/|]|\bפינת\b|\bפינה\b|\bמול\b|\bליד\b|\bבין\b(?=.*\bל)")


def _candidate_tokens(location_text: Optional[str]) -> list:
    """Ordered street-name candidates to try against OSM, best first. Splits a multi-part
    address, strips the city/house-number/street-words, and canonicalizes each part against
    the local street index (fixing ה/ב prefixes and misspellings). De-duped, order-stable."""
    if not location_text:
        return []
    base = _CITY_RE.sub(" ", location_text)
    out: list = []

    def add(tok):
        tok = (tok or "").strip()
        if tok and len(tok) >= 2 and tok not in out:
            out.append(tok)

    parts = [p for p in _SPLIT_RE.split(base) if p and p.strip()]
    cleaned = [_overpass_name(p) for p in parts] or [_overpass_name(base)]
    # canonical (real OSM) names first — they query exactly and fix typos/prefixes
    for c in cleaned:
        real, _how = streets.canonical(c)
        if real:
            add(real)
    for c in cleaned:                                       # then the cleaned raw tokens
        add(c)
    return out


# Landmarks a post can describe itself as being near, when it names no street at all.
# Only places whose surroundings are unambiguously in the search area belong here — a
# point is emitted for the LANDMARK, not the flat, so it must be a good approximation
# of "somewhere around here".
_LANDMARKS = (
    (("אוניברסיטת בן גוריון", "אוניברסיטה", "האוניברסיטה", "בן גוריון", "בן-גוריון"),
     (31.2622, 34.8015)),                                  # campus centre
    (("סורוקה", "בית החולים סורוקה", "המרכז הרפואי סורוקה"),
     (31.2585, 34.8005)),                                  # Soroka
    (("הבלוק", "בבלוק"), (31.259386, 34.796130)),
)
# "near / next to / opposite / close to" — the phrasings that make a landmark the
# subject of the address rather than a passing mention.
_NEAR_RE = re.compile(r"ליד|קרוב\s+ל|בסמוך|קרבת|צמוד\s+ל|מול\s+שער|במרחק")


def _descriptive_landmark(location_text: Optional[str]):
    """(lat, lon) for an address that only describes a position near a landmark, else
    None. Requires BOTH a proximity word and a known landmark, and is only consulted
    after every real geocoding tier has failed."""
    if not location_text:
        return None
    text = _fold_quotes(location_text)
    if not _NEAR_RE.search(text):
        return None
    for names, point in _LANDMARKS:
        if any(n in text for n in names):
            return point
    return None


def _house_number(location_text: Optional[str]) -> Optional[str]:
    """The house number in an address ('אברהם אבינו 38' -> '38', '13/6' -> '13'), else None."""
    m = re.search(r"\b(\d{1,4})\b", location_text or "")
    return m.group(1) if m else None


# --- house-number interpolation -------------------------------------------------
# A street is a LINE, so "אברהם אבינו 38" can't be answered by the street's midpoint —
# that's how a red-end address read as green. Using the OSM addr:housenumber nodes we
# do have (house_anchors.json, written by load_house_numbers.py), we place the number
# by its position ALONG the street: project the anchors onto the polyline, then read
# off where N falls between the two nearest known numbers.
_ANCHORS_PATH = config.ROOT / "house_anchors.json"
_anchors: Optional[dict] = None


def _load_anchors() -> dict:
    global _anchors
    if _anchors is None:
        try:
            _anchors = json.loads(_ANCHORS_PATH.read_text(encoding="utf-8"))
        except Exception:
            _anchors = {}
    return _anchors


def _street_axis(street: str):
    """(points, axis_index) — every point of the street from ALL its OSM segments, plus
    which coordinate (0=lat, 1=lon) runs along the street's length. House numbers increase
    monotonically along a street, so that coordinate is a natural, robust parametrization
    (a street is many disjoint ways, so walking a stitched polyline isn't reliable)."""
    pts = [tuple(p) for seg in streets.geometry(street) for p in seg]
    if len(pts) < 2:
        return [], 0
    import math
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    span_lat = (max(lats) - min(lats)) * 111000
    span_lon = (max(lons) - min(lons)) * 111000 * math.cos(math.radians(lats[0]))
    idx = 0 if span_lat >= span_lon else 1               # the longer extent = along the street
    return sorted(set(pts), key=lambda p: p[idx]), idx


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


# How far past the last known anchor we will project a house number. Measured against
# OSM ground truth, interpolation between anchors lands p50 53 m / p90 101 m out; the
# external street-level fallbacks it replaces land p90 595 m and as much as 3.5 km out
# (ההגנה 89 via Nominatim). So a bounded projection is far better than falling through —
# but only bounded: past ~150 m the numbering assumption stops being evidence.
MAX_EXTRAPOLATE_M = 150.0

_median_gap: Optional[float] = None


def _median_metres_per_number() -> float:
    """Typical metres between consecutive house numbers, across every street that has
    enough anchors to measure. Used only for a street with a SINGLE anchor, where there
    is no local gradient to read — the city's own typical spacing is a far better guess
    than the street's midpoint."""
    global _median_gap
    if _median_gap is not None:
        return _median_gap
    gaps = []
    for street, known in _load_anchors().items():
        pts, idx = _street_axis(street)
        if len(known) < 2 or len(pts) < 2:
            continue
        anchors = sorted((int(k), v[idx]) for k, v in known.items() if str(k).isdigit())
        for (n0, p0), (n1, p1) in zip(anchors, anchors[1:]):
            if n1 == n0:
                continue
            # the axis is a degree coordinate; convert to metres the same way the
            # projection does elsewhere
            metres = abs(p1 - p0) * 111320.0
            per = metres / (n1 - n0)
            if 0.5 <= per <= 40:                   # ignore absurd pairs (bad tagging)
                gaps.append(per)
    _median_gap = statistics.median(gaps) if gaps else 8.0
    return _median_gap


# --- the building layer ---------------------------------------------------------
# `buildings.json` (load_osm_buildings.py) holds the centre of all 19,110 footprints in
# the box. Only 3.7% of them carry a house number, so until now the pipeline could see
# the 700-odd addressed buildings and none of the rest.
_BUILDINGS_PATH = config.ROOT / "buildings.json"
_buildings: Optional[dict] = None

# How far a computed address may be moved to land on a real structure. Deliberately
# small: interpolation is good to a few tens of metres, so a wide radius would let a
# point jump to the NEXT building along, trading a small honest error for a confident
# wrong one. Tuned against the hold-out (geo_accuracy.py) — see SNAP_TUNING below.
SNAP_TO_BUILDING_M = 25.0


def _load_buildings() -> dict:
    global _buildings
    if _buildings is None:
        try:
            raw = json.loads(_BUILDINGS_PATH.read_text(encoding="utf-8"))
            _buildings = {"cell": raw.get("cell") or 0.002, "cells": raw.get("cells") or {}}
        except Exception:
            _buildings = {"cell": 0.002, "cells": {}}
    return _buildings


def nearest_building(lat: float, lon: float, max_m: float = SNAP_TO_BUILDING_M):
    """((lat, lon), metres) for the closest building centre, or None.

    The grid index makes this a scan of ~9 cells (a hundred-odd points) instead of
    19,110, which matters because it runs inside the geocoder's hot path."""
    import math
    data = _load_buildings()
    cells = data["cells"]
    if not cells:
        return None
    cell = data["cell"]
    ci, cj = int(lat / cell), int(lon / cell)
    scale = 111320.0 * math.cos(math.radians(lat))
    best, best_d = None, float("inf")
    for i in range(ci - 1, ci + 2):
        for j in range(cj - 1, cj + 2):
            for blat, blon in cells.get(f"{i}:{j}") or ():
                d = math.hypot((blat - lat) * 111320.0, (blon - lon) * scale)
                if d < best_d:
                    best, best_d = (blat, blon), d
    if best is None or best_d > max_m:
        return None
    return best, best_d


def snap_to_building(pt, max_m: float = SNAP_TO_BUILDING_M):
    """`pt` moved onto the nearest building centre if one is close enough, else `pt`.

    An interpolated house number lands wherever the arithmetic puts it — a garden, a
    car park, the middle of the road. A person looking for the flat is looking for a
    building, and so is the walking router."""
    if not pt:
        return pt
    got = nearest_building(pt[0], pt[1], max_m)
    return got[0] if got else pt


def _point_on_axis(pts, idx, target):
    """The point ON the street's polyline at `target` along its dominant axis.

    Snapping to the nearest VERTEX is what this replaces, and it was collapsing house
    numbers together: measured on אלכסנדר ינאי, eight different numbers all resolved to
    ONE point, because a street polyline has few vertices and every number between two
    of them rounded to the same one. That is the "too many listings on the same spot"
    complaint, generated by the geocoder rather than by the data. Interpolating between
    the bracketing vertices gives a continuous position instead."""
    ordered = sorted(pts, key=lambda p: p[idx])
    if target <= ordered[0][idx]:
        return ordered[0]
    if target >= ordered[-1][idx]:
        return ordered[-1]
    other = 1 - idx
    for a, b in zip(ordered, ordered[1:]):
        if a[idx] <= target <= b[idx]:
            span = b[idx] - a[idx]
            f = 0.0 if span == 0 else (target - a[idx]) / span
            out = [0.0, 0.0]
            out[idx] = target
            out[other] = a[other] + f * (b[other] - a[other])
            return (out[0], out[1])
    return ordered[-1]


def _axis_offset(pts, idx, anchor):
    """How far an anchor sits OFF the street's centreline, as a (dlat, dlon) pair.

    A building is not on the road. Measured across the anchor set, the median address
    sits 27.8 m from the centreline of the street it belongs to (p90 63.7 m) — and
    `_point_on_axis` used to discard all of it, returning a point on the tarmac. Keeping
    the offset puts the answer back on the building line, and on the correct SIDE of the
    road when the anchors used are the same parity.

    Defined against the same axis parametrization the interpolation uses, so feeding an
    anchor's own house number back through reproduces the anchor exactly."""
    base = _point_on_axis(pts, idx, anchor[idx])
    return (anchor[0] - base[0], anchor[1] - base[1])


def _anchors_for(known: dict, n: int) -> list:
    """[(number, (lat, lon))] to interpolate `n` between — same parity when possible.

    Odd and even numbers are on opposite sides of the street, so mixing them averages
    the two sides and lands back in the middle of the road. 45 of the 58 streets with
    ≥4 anchors carry both parities, which is enough for this to matter; when a street
    hasn't got two of the right parity bracketing `n`, all anchors are used and the
    result is simply the older, side-agnostic answer."""
    every = sorted((int(k), (v[0], v[1])) for k, v in known.items() if str(k).isdigit())
    same = [a for a in every if a[0] % 2 == n % 2]
    if len(same) >= 2 and same[0][0] <= n <= same[-1][0]:
        return same
    return every


def place_house(street: Optional[str], number: Optional[str]):
    """((lat, lon), source) for a house number on a street, or (None, None).

    Three cases, in descending order of evidence:
      • the number sits between two known anchors  -> "interpolated"
      • it sits past them, within MAX_EXTRAPOLATE_M -> "extrapolated"
      • the street has ONE anchor, and the number is within MAX_EXTRAPOLATE_M of it at
        the city's typical spacing                 -> "extrapolated"

    "extrapolated" is deliberately NOT in _PRECISE_SOURCES: it is a projection, not a
    survey, so `pipeline._classify` keeps applying its boundary-street and near-edge
    caution to it. It is graded `high` for display because it is still far better than
    the street centroid it replaces."""
    pt = interpolate_house(street, number)
    if pt:
        return pt, "interpolated"
    if not street or not number:
        return None, None
    try:
        n = int(number)
    except (TypeError, ValueError):
        return None, None

    known = _load_anchors().get(street) or {}
    pts, idx = _street_axis(street)
    anchors = sorted((int(k), v[idx]) for k, v in known.items() if str(k).isdigit())
    if not anchors or len(pts) < 2:
        return None, None

    if len(anchors) >= 2:
        # project past whichever end we fell off, using THIS street's own gradient
        lo, hi = anchors[0], anchors[-1]
        span_n, span_p = hi[0] - lo[0], hi[1] - lo[1]
        if span_n <= 0:
            return None, None
        per = span_p / span_n
        edge = hi if n > hi[0] else lo
        target = edge[1] + (n - edge[0]) * per
        overshoot = abs(target - edge[1]) * 111320.0
    else:
        one = anchors[0]
        # no local gradient with a single anchor — use the city's typical spacing, and
        # the street's own direction so we move ALONG it rather than across it
        per = _median_metres_per_number() / 111320.0
        lo_p, hi_p = min(p[idx] for p in pts), max(p[idx] for p in pts)
        direction = 1.0 if (one[1] - lo_p) < (hi_p - one[1]) else -1.0
        target = one[1] + direction * (n - one[0]) * per
        overshoot = abs(target - one[1]) * 111320.0

    if overshoot > MAX_EXTRAPOLATE_M:
        return None, None                          # too far to still be evidence
    # never leave the street: clamp to its real geometry
    return _point_on_axis(pts, idx, target), "extrapolated"


def interpolate_house(street: Optional[str], number: Optional[str]):
    """(lat, lon) for house `number` on `street`, interpolated between the nearest KNOWN
    OSM address nodes, or None.

    Deliberately conservative — returns None unless we can place the number credibly:
      • the street needs geometry and ≥2 address anchors, and
      • the number must lie WITHIN the anchored range (we never extrapolate — OSM often
        knows only 1..19 of a street that runs to 60, and guessing past the last anchor
        is exactly the false precision this is meant to remove).
    A None simply means "street-level only", which the caller treats as lower confidence.

    Position ALONG the street still follows the polyline, so a bend doesn't get cut off;
    the anchors' distance OFF the centreline is interpolated alongside it and added back
    (`_axis_offset`), so the answer lands on the building line rather than on the road.
    """
    if not street or not number:
        return None
    try:
        n = int(number)
    except (TypeError, ValueError):
        return None
    known = _load_anchors().get(street) or {}
    pts, idx = _street_axis(street)
    if len(known) < 2 or len(pts) < 2:
        return None
    anchors = _anchors_for(known, n)
    if len(anchors) < 2 or not (anchors[0][0] <= n <= anchors[-1][0]):
        return None                                        # outside known range -> no guess
    lo_n, lo_c = max((p for p in anchors if p[0] <= n), key=lambda p: p[0])
    hi_n, hi_c = min((p for p in anchors if p[0] >= n), key=lambda p: p[0])
    f = 0.0 if hi_n == lo_n else (n - lo_n) / (hi_n - lo_n)   # linear in house number
    target = lo_c[idx] + f * (hi_c[idx] - lo_c[idx])
    base = _point_on_axis(pts, idx, target)
    d_lo = _axis_offset(pts, idx, lo_c)
    d_hi = _axis_offset(pts, idx, hi_c)
    return (base[0] + d_lo[0] + f * (d_hi[0] - d_lo[0]),
            base[1] + d_lo[1] + f * (d_hi[1] - d_lo[1]))


MAX_OVERPASS_CANDIDATES = 3        # bound the paced queries per address


# Per-mirror circuit breaker. Most public Overpass mirrors are usually down; without
# this every lookup pays 15s × each dead mirror (measured: a single address could stall
# ~3 minutes, and the live scraper pays it too). Once a mirror fails in this process we
# stop calling it, so the cost of a dead mirror is paid once, not per address.
_dead_mirrors: set = set()


def _overpass_query(name: str, hn: Optional[str]):
    """(coords, source, responded) for ONE candidate street name."""
    import requests

    la0, lo0, la1, lo1 = _bs_bounds()
    bbox = f"{la0},{lo0},{la1},{lo1}"                       # Overpass: S,W,N,E
    # For a numbered address, ALSO ask for the exact OSM address node (street+number) —
    # a precise point. Plus named streets (highways) and any named node/way; we rank
    # client-side so the precise addr node > a real road > a same-named POI.
    addr = (f'node["addr:housenumber"="{hn}"]["addr:street"~"{name}"]({bbox});' if hn else "")
    q = (f'[out:json][timeout:25];'
         f'({addr}'
         f'way["highway"]["name"~"{name}"]({bbox});'
         f'way["name"~"{name}"]({bbox});'
         f'node["name"~"{name}"]({bbox}););'
         f'out center tags 25;')
    timeout = getattr(config, "OVERPASS_TIMEOUT_SEC", 15)
    live = [u for u in config.OVERPASS_URLS if u not in _dead_mirrors]
    if not live:                                           # all known-dead: retry them once
        _dead_mirrors.clear()
        live = list(config.OVERPASS_URLS)
    for url in live:                                        # first mirror that responds wins
        try:
            time.sleep(1.0)                                # be polite to the shared instance
            r = requests.post(url, data={"data": q},
                              headers={"User-Agent": config.NOMINATIM_USER_AGENT}, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception:
            _dead_mirrors.add(url)                         # don't pay this timeout again
            continue                                       # this mirror timed out — try the next
        # A valid response is authoritative (OSM data is identical across mirrors):
        # take the best-ranked in-box hit, or None — never keep hammering other mirrors.
        coords, source = _overpass_pick(data.get("elements", []), name, hn)
        return coords, source, True
    return None, None, False                               # every mirror failed — transient


def _overpass(location_text: str):
    """Resolve a Be'er Sheva street/place via the free public Overpass API, trying the
    ordered candidate names from _candidate_tokens (canonical street names first, so a
    ה/ב prefix or a misspelling still resolves). First hit wins; bounded to the BS box.
    Returns (coords, source, responded) — responded=False means every mirror failed
    (transient), so the caller must not cache it as a real 'not found'."""
    cands = [c for c in _candidate_tokens(location_text) if len(c) >= _MIN_REVERSE_MATCH]
    if not cands:
        return None, None, True                            # nothing to look up = a real miss
    hn = _house_number(location_text)
    any_response = False
    for name in cands[:MAX_OVERPASS_CANDIDATES]:
        coords, source, responded = _overpass_query(name, hn)
        any_response = any_response or responded
        if coords:
            return coords, source, True
    return None, None, any_response


# A point from an EXTERNAL geocoder that sits further than this from the street the
# address actually names is a blunder, not imprecision. audit_geocode.py measures the
# median offset across stored listings at 6 m, and a street-level hit legitimately sits
# anywhere ALONG its street — so this only ever catches "that isn't the right street".
# Measured examples it rejects: ההגנה 89 at 3,528 m (nominatim) and רחבת יבנה 29 at
# 2,964 m (overpass), the two worst errors in the whole hold-out.
MAX_EXTERNAL_OFFSET_M = 250.0

# Nominatim answers with whatever it found. We asked for somewhere to LIVE, so a
# railway station, a shop or a bus stop is a wrong answer even when the name overlaps —
# "ליד האוניברסיטה" matched the station named  …אוניברסיטה 783 m away and became a MATCH.
_NOMINATIM_OK_CLASSES = {"highway", "place", "building", "landuse", "boundary"}


def _off_claimed_street_m(location_text: str, lat: float, lon: float):
    """Metres from `(lat, lon)` to the geometry of the street the ADDRESS names, or
    None when we don't know that street and so can't judge."""
    for cand in _candidate_tokens(location_text)[:2]:
        real, _how = streets.canonical(cand)
        segs = streets.geometry(real) if real else []
        if not segs:
            continue
        return min(_haversine_m(lat, lon, p[0], p[1]) for seg in segs for p in seg)
    return None


def _plausible_external(location_text: str, coords, source: str) -> bool:
    """Is an external geocoder's answer consistent with the address we asked about?

    Rejecting returns the listing to NEEDS_DATA, where a human sees it — strictly better
    than placing it hundreds of metres away, where it silently gets a wrong tier, a wrong
    walk time and possibly a wrong MATCH or DROP."""
    if not coords:
        return False
    off = _off_claimed_street_m(location_text, coords[0], coords[1])
    if off is not None and off > MAX_EXTERNAL_OFFSET_M:
        print(f"[geocode] rejected {source} for {location_text!r}: {off:.0f} m from the "
              f"street it names")
        return False
    return True


def _overpass_pick(elements: list, name: str, housenumber: Optional[str] = None):
    """(coords, source) for the best in-box element, or (None, None). Prefers an exact
    ADDRESS NODE (street+number → precise, source 'osm_addr'), then an exact-name street
    (highway), over a generic named node/way (source 'overpass', a street-level point)."""
    def is_addr(el) -> bool:
        t = el.get("tags", {}) or {}
        return bool(housenumber and t.get("addr:housenumber") == housenumber
                    and name in (t.get("addr:street") or ""))

    def rank(el) -> tuple:
        t = el.get("tags", {}) or {}
        return (is_addr(el), t.get("name", "") == name, "highway" in t)   # higher = better

    for el in sorted(elements, key=rank, reverse=True):
        c = el.get("center") or el                         # ways carry a computed center
        lat, lon = c.get("lat"), c.get("lon")
        if lat is not None and lon is not None and _in_beer_sheva(float(lat), float(lon)):
            return (float(lat), float(lon)), ("osm_addr" if is_addr(el) else "overpass")
    return None, None


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
            hit = data[0]
            # We asked for somewhere to LIVE. Nominatim answers with whatever matched,
            # so a railway station / shop / bus stop is a wrong answer even when the
            # name overlaps — that is how "ליד האוניברסיטה" became a MATCH 783 m away.
            cls = (hit.get("class") or "").lower()
            if cls and cls not in _NOMINATIM_OK_CLASSES:
                print(f"[geocode] rejected nominatim for {location_text!r}: "
                      f"it is a {cls}/{hit.get('type')}, not a place to live")
                return None
            return float(hit["lat"]), float(hit["lon"])
    except Exception:
        pass
    return None


if __name__ == "__main__":       # small CLI:  python geocode.py uncache <location text>
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "uncache":
        removed = uncache(" ".join(sys.argv[2:]))
        print(f"uncached {len(removed)} entr(y/ies): {removed}" if removed else "nothing matched")
    else:
        print("usage: python geocode.py uncache <location text>")
