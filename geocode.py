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


# Slang keys with no `שכונה` prefix that were ASSUMED to cover a whole quarter. Kept only
# as the answer for a key we have not surveyed: once `landmarks.json` holds a drawn
# outline, its measured extent decides (see `_landmark_grade`), because a guess here is
# wrong in both directions. `הבלוק` lived in this set described as "the whole student
# quarter, several streets across"; surveyed it is 85 x 96 m — a 123 m diagonal, TIGHTER
# than a typical street centroid — so it was being thrown away as "not a location".
_AREA_KEYS = {"הבלוק", "בבלוק"}

# How big a drawn landmark may be and still count as a precise point. 150 m is the size of
# a building plot or a small cluster — `מגדלי דוד` 115 m, `הבלוק` 123 m, `מרכז הנגב` 135 m.
# Past that it is a district you still have to walk across: `אביסרור` measures 299 m, so it
# answers at street level, which is honestly what a 300 m shape knows.
_LANDMARK_PRECISE_M = 150.0
_LANDMARK_STREET_M = 400.0

_landmarks_cache = None


def landmarks() -> dict:
    """Hand-surveyed landmark outlines from `landmarks.json` (see
    load_landmarks_from_kmz.py). Absent file -> {} and every caller falls back to the
    behaviour it had before the survey existed."""
    global _landmarks_cache
    if _landmarks_cache is None:
        try:
            import json
            from pathlib import Path
            p = Path(__file__).with_name("landmarks.json")
            _landmarks_cache = {_normalize(k): v
                                for k, v in json.loads(p.read_text(encoding="utf-8")).items()}
        except Exception:
            _landmarks_cache = {}
    return _landmarks_cache


def _landmark_grade(extent_m: float) -> str:
    """What a shape of this size can honestly claim. THE POLYGON IS THE UNCERTAINTY."""
    if extent_m <= _LANDMARK_PRECISE_M:
        return "static"
    if extent_m <= _LANDMARK_STREET_M:
        return "static_street"
    return "static_area"


def _static_source(key: Optional[str]) -> str:
    """'static' for a real place, 'static_area' for a whole neighbourhood or quarter.

    The distinction is about the KEY, not the address: whatever the post said, if the
    thing we matched is an area then the coordinate is an area centroid and every listing
    that matches it lands on the identical point.

    A SURVEYED landmark is graded from its drawn size instead of from this guess — that
    is the only way `הבלוק` (123 m, a real place) and `שכונה ד` (2,375 m, not one) stop
    being treated alike."""
    n = _normalize(key or "")
    lm = landmarks().get(n)
    if lm and "extent_m" in lm:
        return _landmark_grade(lm["extent_m"])
    if n.startswith("שכונה") or n.startswith("שכונת") or n in _AREA_KEYS:
        return "static_area"
    return "static"


# How close a FUZZY street match must be to count as "this address names a street".
# Set from measurement, not taste — the three cases that matter sit either side of it:
#   יוסף בן מתתיהו -> יוסף בן מתיתיהו   0.966   a one-letter spelling variant: WANT
#   האוני          -> הגאונים           0.833   a short-string coincidence:    REFUSE
#   בן מתתיהו      -> יוסף בן מתיתיהו   0.750   already recorded unresolvable: REFUSE
_STREET_FUZZY_MIN = 0.90


def _names_a_street(cand: str, norm_text: str) -> bool:
    """Does this candidate token name a real street confidently enough to act on?

    The gate this feeds DISCARDS a working placement (a neighbourhood centroid) in favour
    of the street, so a wrong yes is expensive.

    TWO CONDITIONS, AND BOTH ARE LOAD-BEARING — each was learned by breaking the other:

    1. The token must appear VERBATIM in the address. `_candidate_tokens` also emits its
       own corrected spellings, and a correction is a fuzzy step this function cannot
       see: for `ליד האוני` it offers `הגאונים`, which `streets.canonical` then answers
       `exact`. Dropping this check made "near the university" look like a street address
       and lose the only key that could place it.
    2. A FUZZY match must additionally be a close one. Requiring non-fuzzy was too strict:
       `יוסף בן מתתיהו` is verbatim but resolves fuzzy (one letter from OSM's
       `יוסף בן מתיתיהו`), while its corrected twin resolves exact but is not verbatim —
       each failed a different half, so a street we know perfectly well was reported as
       no street at all and the listing drew on שכונה ד.
    """
    if _normalize(cand) not in norm_text:
        return False
    real, how = streets.canonical(cand)
    if not real:
        return False
    if how != "fuzzy":
        return True
    import difflib
    return difflib.SequenceMatcher(None, _normalize(cand),
                                   _normalize(real)).ratio() >= _STREET_FUZZY_MIN


def _near_governs(norm_text: str, norm_key: str) -> bool:
    """Does a proximity word sit immediately before this landmark in the address?

    Position matters, not mere presence: `רגר 5, ליד הבלוק` names a real street and is a
    real address that happens to mention a bearing, while `ליד הבלוק` alone is only the
    bearing. Looking just before the matched key is what tells them apart.

    THE LOOKBACK STOPS AT A SEPARATOR, because a post names several places at once and a
    bearing belongs to exactly one of them. `ליד הבלוק, מגדלי דוד` is NEAR הבלוק and AT
    מגדלי דוד; a plain 14-character window reached back over the comma, found הבלוק's
    `ליד`, and marked מגדלי דוד as a bearing too — so the address the post actually gave
    was discarded and the flat drew on the landmark it was merely near."""
    pos = norm_text.find(norm_key)
    if pos <= 0:
        return False
    window = norm_text[max(0, pos - 14):pos]
    cut = max(window.rfind(sep) for sep in (",", ".", "|", "/", "،"))
    if cut != -1:
        window = window[cut + 1:]
    return bool(_NEAR_RE.search(window))


_NBHD_PHRASE_RE = re.compile(r"שכונ[הת]\s*[א-ת]['׳\"״]?")


def names_only_a_neighbourhood(location_text: Optional[str]) -> bool:
    """Is this address a neighbourhood NAME AND NOTHING ELSE — `שכונה ד`, `שכונה ג'`?

    The user's rule (2026-08-04): keep a placeless listing only if it names a known
    location like `הבלוק`; a bare quarter is not one. שכונה ד is 2,375 m across, so its
    centroid is a dot in the middle of thousands of flats.

    A TEXT TEST, and deliberately NOT `is_bare_neighborhood`, which answers True for
    `אלעזר בן יאיר שכונה ד` — an address that names a street. That predicate was written
    for a different question (may this be capped to amber?) and using it here would
    delete the flats the user's own "a street is okay" rule protects.

    Anything left over after the quarter is removed counts, EVEN IF WE CANNOT PLACE IT:
    `אנדלה אמבלו, שכונה ד` names a street missing from OSM, and failing to geocode a
    street is our limitation, not the post's."""
    text = _fold_quotes(location_text or "").strip()
    if not text:
        return False                       # an empty address is a different problem
    if not _NBHD_PHRASE_RE.search(text):
        return False
    return not _NBHD_PHRASE_RE.sub(" ", text).strip(" ,־-–'\"׳״\t")


def landmark_point(key: Optional[str]):
    """A surveyed landmark's centroid, else None. Preferred over its STATIC_TABLE point:
    a drawn outline's centre beats a single dropped pin (`הבלוק` moved 67 m, `אביסרור`
    89 m)."""
    lm = landmarks().get(_normalize(key or ""))
    if lm and lm.get("centroid"):
        return tuple(lm["centroid"])
    return None


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
               # a projection past the known anchors, along the street's OWN measured
               # gradient: much better than the street centroid it replaces, but
               # deliberately absent from _PRECISE_SOURCES so the boundary-street
               # caution still applies
               "extrapolated": "high",
               # a projection from a SINGLE anchor. That anchor fixes where one number
               # is, not which way the numbers run — the direction is a guess (they are
               # assumed to run toward the longer half of the street). Same coordinate
               # as "extrapolated" but an honest label, because the evidence is
               # genuinely weaker and the map should say so.
               "projected": "street",
               # a static-table hit on a whole AREA rather than a place: "שכונה ד".
               # One coordinate stands for a neighbourhood, so 19 flats landed on it
               # drawn as solid, precise dots — the single biggest pile on the map, and
               # a lie. Its siblings already grade honestly (static_street -> street,
               # landmark -> street); this joins them.
               # NB `הבלוק` used to be graded here on the ASSUMPTION that it was a whole
               # quarter. Surveyed, it is 123 m across and now grades `static`.
               "static_area": "area",
               # the middle of a street we hold the POLYLINE for, for an address that
               # names that street and no house number. A street is a line, so this is
               # street-level by construction — the same grade `static_street` and the
               # external tiers get, and deliberately never precise.
               "street_geom": "street",
               # a NEIGHBOURING house's surveyed point, used only when nothing else could
               # place a numbered address. Real evidence, on the right street, but it is
               # not this house — grading it `high` would claim a precision we do not have.
               "anchor_neighbour": "street",
               "overpass": "street", "nominatim": "street"}


def confidence(source: Optional[str]) -> str:
    """'exact' | 'high' | 'street' | 'area' | 'none' for a geocode source."""
    if not source:
        return "none"
    return _CONFIDENCE.get(source, "street")


def is_precise_source(source: Optional[str]) -> bool:
    return source in _PRECISE_SOURCES


# The tiers that count as KNOWING WHERE THE FLAT IS. The user's rule, 2026-08-03:
# a street is an address, a neighbourhood on its own is not, and an institution is not.
# That is exactly the exact/high/street line, so this reuses `confidence` rather than
# adding a fifth notion of address quality to the four the codebase already has.
_LOCATED = frozenset(("exact", "high", "street"))


def has_location(source: Optional[str]) -> bool:
    """Do we know where this flat is, even roughly, on a STREET?

      exact/high  a house number          -> yes
      street      a named street          -> yes  ("street is okay")
      area        only a neighbourhood    -> NO   ("only neighbourhood is not an address")
      none        institution, blurb, ""  -> NO   ("that's not an address")

    Hand-pinned landmarks grade `exact` via the static table, so `מגדלי דוד` counts as
    located — which is why this is keyed on the geocode SOURCE and not on whether the
    address text happens to name a street."""
    return confidence(source) in _LOCATED


def names_only_a_landmark(location_text: Optional[str]) -> bool:
    """Is this text a BEARING off a landmark rather than an address — `ליד האוניברסיטה`,
    `מול שער האוניברסיטה`, `אזור האוניברסיטה וסורוקה`?

    The user's rule, 2026-08-03: remove these however well they score. It is narrower
    than the score gate beside it and deliberately so — a bare neighbourhood like
    `שכונה ד` is NOT this, and stays subject to the score rule as decided earlier.

    CALL THIS ONLY WHEN `has_location` IS ALREADY FALSE. It answers True for
    `מגדלי דוד, סורוקה` too, and that is a real building the user pinned by hand: in the
    live path the static table answers it before `_is_bare_proximity` is ever consulted,
    so the text alone cannot tell the two apart. The geocoder's verdict can — `מגדלי דוד`
    comes back `exact`, so pairing the two predicates keeps it and drops the bearings."""
    return _is_bare_proximity(location_text)


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
    # Two student blocks the posts name constantly and no free source resolves.
    # Hand-supplied by the user, 2026-08-03. `מגדלי דוד` was actively WRONG before this:
    # "מגדלי דוד, סורוקה" fell through to Overpass and landed on the hospital/campus
    # point, which is not where the towers are. Bare keys, so the substring match also
    # catches "מגדלי גראנד אביסרור", "אביסרורים הגבוהים" and "מגדלי דוד, סורוקה".
    "מגדלי דוד": (31.255349, 34.803121),
    "אביסרור": (31.254823, 34.798264),
    # There ARE flats at מרכז הנגב (user, 2026-08-03). Before this it was worse than
    # unplaced: the sub-run tier matched the bare word `מרכז` and answered with the
    # STREET `מרכז אורן`, a different place. That match is refused now, and this gives
    # the right answer instead of no answer.
    "מרכז הנגב": (31.259132, 34.795781),
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
GEOCODE_LOGIC_VERSION = 11     # 11: tail strip, sub-run streets, מרכז הנגב pinned
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
    """Persist the cache — but never let a nearly-empty one overwrite a full one.

    Twice in one day the on-disk cache went from ~300 entries to 1. The mechanism is
    always the same: something sets `_cache` to a small dict (a hold-out harness using it
    as scratch, a long-lived process whose copy predates a rebuild), then one successful
    or missed lookup calls this and the small dict lands on disk. Recovering costs a
    35-minute re-geocode of every listing, and until someone notices, the map silently
    loses two thirds of its dots.

    The cache is a pure accelerator, so refusing a suspicious write can only cost time,
    never correctness — whereas allowing one costs real data. A legitimate shrink (an
    `uncache`, or entries ageing out) moves a handful of keys, not almost all of them."""
    try:
        payload = json.dumps(_cache, ensure_ascii=False, indent=0)
    except Exception:
        return
    try:
        on_disk = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        on_disk = {}
    if len(on_disk) > 20 and len(_cache) < len(on_disk) * 0.5:
        print(f"[geocode] refusing to shrink the cache {len(on_disk)} -> {len(_cache)} "
              f"entries — something is holding a stale copy")
        return
    try:
        _CACHE_PATH.write_text(payload, encoding="utf-8")
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
    # rules that keep it ACCURATE: a neighborhood centroid, a static street entry AND A
    # SURVEYED LANDMARK all step aside for an address that carries a house number.
    #
    # THIS DOCSTRING'S CLAIM OF PARITY IS WHAT HID A REAL DIVERGENCE FOR MONTHS. The
    # `or k in landmarks()` clause below was added to the twin loop in `_resolve_detailed`
    # and never to this one, so the pipeline placed `רגר 137, הבלוק` at house 137 while
    # THE MAP drew it on the הבלוק pin — 624 m away, under a confidence badge reading
    # `exact`, because `dashboard.py` grades the STORED source but fetches the coordinate
    # from here. Measured 2026-08-12: 15 listings, p90 222 m, max 626 m, all on one pin.
    # When you change precedence in either loop, change both and extend the agreement
    # test — `tests/test_geocode.py::test_the_two_lookup_loops_agree_*`.
    precise = is_precise_address(location_text) and not is_bare_neighborhood(location_text)
    numbered = bool(_house_number(location_text))
    best_pos, best_coords, best_key, skipped_street = None, None, None, None
    # A SURVEYED LANDMARK IS A KEY IN ITS OWN RIGHT. Without this line `landmarks.json`
    # could only re-grade and re-centre a name STATIC_TABLE already knew, so importing a
    # KMZ for a new place (`מגדל הספורט`, 90 m) placed nothing at all — the loop never
    # matched it. Importing a survey is now enough on its own; no code edit per landmark.
    surveyed = {k: tuple(v["centroid"]) for k, v in landmarks().items() if v.get("centroid")}
    # A USER PIN IS A SUBSTRING RULE, NOT AN ENTRY — and it returns below WITHOUT passing
    # `_not_on_campus`, which is deliberate for a hand-placed point and catastrophic for a
    # pin named after a landmark. Measured 2026-08-12: a single pin on `אוניברסיטה` moves
    # 6 of 9 university-ish names onto the campus point, `ליד האוניברסיטה` and
    # `שער האוניברסיטה` among them — and `רגר 5, ליד האוניברסיטה` too, because this loop
    # runs BEFORE the house-number interpolation below, so the pin beats a real address.
    # That is why a "pin these" report must never offer a bearing: `pinnable_unknowns`.
    for key, coords in (list(STATIC_TABLE.items()) + list(_load_user_pins().items())
                        + list(surveyed.items())):
        k = _normalize(key)
        if not k:
            continue
        if precise and (k.startswith("שכונה") or k.startswith("שכונת")):
            continue
        # `or k in landmarks()` mirrors `_resolve_detailed`, which explains it: a house
        # number is ~13 m and the tightest landmark here is 115 m, so the number wins over
        # ANY of them. It must test membership and not the `static_area` GRADE, because
        # once `הבלוק` was surveyed it began grading `static` (123 m) and would fall out.
        if numbered and (streets.known(k) or _static_source(key) == "static_area"
                         or k in landmarks()) \
                and k in norm:
            skipped_street = skipped_street or coords
            continue
        pos = norm.find(k)
        if pos != -1 and (best_pos is None or pos < best_pos):
            best_pos, best_coords, best_key = pos, coords, key
    if best_coords is not None:
        # A DRAWN OUTLINE'S CENTRE BEATS A DROPPED PIN — the twin does this at the
        # equivalent return. Without it the map used the hand-placed STATIC_TABLE pin even
        # when the landmark was the right answer: `הבלוק` 67.1 m off its surveyed
        # centroid, `מגדלי דוד` 7.7 m, `מרכז הנגב` 5.2 m.
        return landmark_point(best_key) or best_coords

    hn = _house_number(location_text)
    if hn:                                    # local interpolation, no network
        for cand in _candidate_tokens(location_text)[:2]:
            real, _how = streets.canonical(cand)
            pt, how = place_house(real or cand, hn)
            if pt and _plausible_external(location_text, pt, how):
                return _not_on_campus(pt)

    # The cache long outlives the rules that filled it: an entry written before the
    # no-housing mask existed still pointed at Soroka, and this is the function every
    # map dot goes through, so it has to apply the mask too — not just geocode_detailed.
    entry = _load_cache().get(norm)
    if isinstance(entry, dict) and entry.get("c"):
        return _not_on_campus(tuple(entry["c"]))
    if isinstance(entry, list) and len(entry) == 2:      # legacy bare [lat, lon]
        return _not_on_campus(tuple(entry))
    # The street's own polyline — local, so it belongs on the no-network path too. Without
    # it the pipeline would place a bare street name and the dashboard, which renders every
    # dot through this function, would draw nothing for the same listing.
    bare = _bare_street_point(location_text)
    if bare:
        return _not_on_campus(bare[0])
    return skipped_street                     # street-level, better than no dot at all


def still_unplaceable(location_text: Optional[str]) -> bool:
    """True when nothing we hold LOCALLY can put this name on the map.

    For the "pin these" reports built on `storage.unknown_locations`, which is a log of
    what failed ONCE — nothing ever re-checks it, so a name sits on that list forever
    after a single miss, however many static entries, 📍 pins, anchors and street
    polylines have landed since. Measured 2026-08-12: **98 of the 182 logged names
    resolve today**, the top entry among them (`שכונת הפארק`, 5 hits, answered by the
    static table), so the head of the list was recommending work already done.

    Deliberately `geocode_cached` and NOT `geocode_detailed`: a report may not depend on
    an Overpass mirror. Those mirrors disagree with each other — reading a measurement
    off them is what made the accuracy harness a coin flip — and stats.py is documented
    as making no network calls. The whole re-check runs in 0.33 s for 182 names.
    That errs in the safe direction: a name only Overpass could place stays LISTED, so
    the failure mode is a redundant suggestion, never a hidden one.

    Note this is NOT `has_location`, which answers a different question about a saved
    listing (keyed on the geocode SOURCE, and `area` does not count). Here an area
    centroid means the name already HAS a table entry — nothing left to pin."""
    return geocode_cached(location_text) is None


def pinnable_unknowns(rows):
    """Split a `storage.unknown_locations` log into the names actually worth pinning.

    Returns `(pinnable, resolved, by_design)` — the surviving rows, then the COUNTS of
    the two groups removed. FOUR reports were building a "pin these" section straight off
    that log with no re-check; this is the one filter they share, so it cannot drift
    apart between them. Rows are `(location, count, last_seen)`, and only `[0]` is read.

    Both removals are counted rather than dropped, because the callers must be able to
    print them: a filter you cannot audit hides a real gap the day it goes wrong.

    The two groups are removed for OPPOSITE reasons, and conflating them would be the
    easy mistake. `resolved` is work already DONE — the geocoder answers the name today,
    so there is nothing left to pin. `by_design` is work that must NEVER be done: a
    bearing off a landmark (`ליד האוניברסיטה`) has no address to pin, and pinning one is
    actively destructive — see the warning on `_load_user_pins` in `geocode_cached`."""
    import contextlib
    import io
    # The geocoder narrates its own rejections ("[geocode] rejected extrapolated for …")
    # and loads its anchors on first use. Useful when placing a listing, pure noise in
    # the middle of a report — and on the Telegram path it is not even visible.
    with contextlib.redirect_stdout(io.StringIO()):
        still = [r for r in rows if still_unplaceable(r[0])]
        pin = [r for r in still if not names_only_a_landmark(r[0])]
    return pin, len(rows) - len(still), len(still) - len(pin)


def _not_on_campus(pt):
    """`pt`, unless it is on land nobody rents — then None. Hand-placed points never
    reach here; the static-table and user-pin returns above bypass it deliberately."""
    if not pt:
        return None
    import zones
    return None if zones.no_housing_here(pt[0], pt[1]) else pt


# A hand-curated point is a decision, not a guess: the static table is maintained by
# hand and `user_pins.json` is written by the 📍 button, so if either says a flat is on
# campus, someone meant it. Everything else is computed or fetched and gets masked.
_NO_MASK_SOURCES = {"static", "static_area", "static_street", "manual"}


def geocode_detailed(location_text: Optional[str]):
    """(coords, source) or (None, None). source ∈
    static/cache/google/overpass/nominatim — which tier resolved the name, so a
    lower-confidence hit (overpass/nominatim) can be flagged for a human check.
    Order: static table -> cache -> Google -> Overpass -> Nominatim.

    Whatever tier answers, the point must be somewhere a flat can exist. See
    `_reject_no_housing`."""
    coords, source = _resolve_detailed(location_text)
    return _reject_no_housing(location_text, coords, source)


def _reject_no_housing(location_text, coords, source):
    """Drop a computed point that landed on a campus or in a hospital.

    Nobody rents in the middle of Ben-Gurion University, so such a point is a data error
    every time — and they were reaching the map from several directions at once: the old
    `אוניברסיטה` landmark WAS the campus centre, 13 house-number anchors sat on
    institutional buildings, and an external geocoder can always answer with a lecture
    hall. Rejecting sends the address to NEEDS_DATA, where a person sees it; that is the
    same trade `_plausible_external` already makes for a point 250 m off its street."""
    if not coords or source in _NO_MASK_SOURCES:
        return coords, source
    import zones
    where = zones.no_housing_here(coords[0], coords[1])
    if where:
        print(f"[geocode] rejected {source} for {location_text!r}: inside {where} — "
              f"not a place anyone rents")
        return None, None
    return coords, source


def _named_street(location_text: Optional[str], norm: Optional[str] = None) -> Optional[str]:
    """The real OSM name of a street this address actually NAMES, or None.

    ONE PREDICATE, TWO CALLERS, DELIBERATELY: the rule that decides whether a named street
    may THROW AWAY a neighbourhood centroid has to be the same rule that decides whether
    we may place a listing on that street's own geometry. So the test itself lives in
    `_names_a_street` and this only reports WHICH street passed it.

    That mattered on the rebase. This function was first written against the older,
    stricter rule (`how != "fuzzy"`), and `_names_a_street` has since relaxed it: a fuzzy
    match is allowed when it is a close one, because requiring non-fuzzy lost
    `יוסף בן מתתיהו` — verbatim but one letter from OSM's spelling, while its corrected
    twin resolves exact and is not verbatim. Re-stating the condition here would have
    silently reverted that fix for every caller of this path."""
    norm = _normalize(_fold_quotes(location_text)) if norm is None else norm
    for c in _candidate_tokens(location_text or ""):
        if _names_a_street(c, norm):
            real, _how = streets.canonical(c)
            if real:
                return real
    return None


def _resolve_detailed(location_text: Optional[str]):
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
    # DOES THE ADDRESS NAME A STREET AT ALL — with or without a house number? The user's
    # rule is "a street is an address"; `is_precise_address` only sees a digit or a
    # `רחוב`-style word, so `אלעזר בן יאיר שכונה ד` looked like a bare neighbourhood
    # (`is_bare_neighborhood` even returns True for it) and the שכונה centroid won. That
    # put 13 listings 364-1,070 m from the street their own post names — `שלמה המלך,
    # שכונה ג` was the worst at 1,070 m.
    names_street = _named_street(location_text, norm) is not None
    best_pos, best_coords, best_key = None, None, None
    near_pos, near_coords, near_key = None, None, None   # keys the flat is only NEAR
    # Last-resort coords from a key we deliberately stepped over, WITH the grade that key
    # honestly deserves. It used to be a bare coordinate labelled `static_street`, which
    # was fine while only street keys were skipped — now neighbourhood centroids are
    # skipped too, and calling one of those "street" would relabel a 2 km area as a
    # street-level fix. `שלמה המלך, שכונה ג` came back `static_street` 1,070 m from
    # שלמה המלך: the right point was never found, and the wrong one claimed to be good.
    skipped_street_coords = skipped_grade = skipped_key = None
    # Surveyed landmarks are keys in their own right — see the twin loop above. THERE
    # ARE TWO of these loops; patching only one placed nothing, because the live path
    # runs this one.
    surveyed = {k: tuple(v["centroid"]) for k, v in landmarks().items() if v.get("centroid")}
    for key, coords in (list(STATIC_TABLE.items()) + list(_load_user_pins().items())
                        + list(surveyed.items())):
        k = _normalize(key)
        if not k:
            continue
        # NO FALLBACK IS RECORDED HERE, deliberately. If the named street cannot be
        # resolved the honest answer is NEEDS_DATA, not the neighbourhood centroid we
        # just rejected — `שלמה המלך, שכונה ג` would quietly return to being 1,070 m
        # wrong, and a nonsense address like `רחוב שלא נמצא 12345` would answer with
        # whichever שכונה happened to be first in the table.
        if (precise or names_street) and (k.startswith("שכונה") or k.startswith("שכונת")):
            continue                                        # don't let a nbhd centroid hijack a street
        # An AREA key must stand aside for a named street too — but ONLY an area key.
        # A surveyed landmark is not one: `הבלוק` is 123 m across, TIGHTER than a street
        # centroid, so `רבי טרפון, הבלוק` is better answered by הבלוק than by the middle
        # of רבי טרפון. Yielding is for keys that know LESS than a street, not more.
        if names_street and _static_source(key) == "static_area" and k in norm:
            continue                                        # same rule, slang area keys
        # An AREA OR LANDMARK key must stand aside for a house number just as a street key
        # does. `רגר 137, הבלוק` was resolving to the slang quarter instead of house 137,
        # even though רגר is anchored 53-191 and would have placed it exactly — the
        # trailing area name simply won the static match. ~7 listings.
        # This tests `k in landmarks()` and not just the `static_area` grade: once הבלוק
        # was surveyed it stopped grading `static_area` and silently fell out of this
        # rule, re-breaking `רגר 137, הבלוק`. A house number is ~13 m; the tightest
        # landmark here is 115 m, so the number wins over ANY of them.
        if numbered and (streets.known(k) or _static_source(key) == "static_area"
                         or k in landmarks()) \
                and k in norm:
            if skipped_street_coords is None:
                # the KEY too, not just its grade: the fallback below has to tell a
                # surveyed landmark from a street, and the grade alone cannot
                skipped_street_coords, skipped_grade = coords, _static_source(key)
                skipped_key = key
            continue                                        # let the house number win
        # A KEY THE FLAT IS ONLY *NEAR* LOSES TO ONE IT IS *AT*, whatever the word order.
        # A post often names both — `ליד הבלוק, מגדלי דוד` is AT מגדלי דוד and NEAR
        # הבלוק. Ranking purely by position answered with the landmark it is near, and
        # then graded the whole thing `area`, so the address it actually gave was thrown
        # away. Governed keys are collected separately and only used if nothing ungoverned
        # matched at all.
        governed = _near_governs(norm, k)
        pos = norm.find(k)
        if pos != -1:                                       # forward: key inside the address
            if governed:
                if near_pos is None or pos < near_pos:
                    near_pos, near_coords, near_key = pos, coords, key
            elif best_pos is None or pos < best_pos:
                best_pos, best_coords, best_key = pos, coords, key
        elif (len(norm) >= _MIN_REVERSE_MATCH and norm in k
              and best_coords is None and not governed):
            best_pos, best_coords, best_key = 10 ** 6, coords, key   # reverse: lowest
    # An AT-key if we have one; otherwise the best NEAR-key, downgraded.
    # "NEAR X" IS NOT "AT X", and it is easy to miss because the static table answers
    # several tiers before `_is_bare_proximity` would ever be consulted: `ליד מגדלי דוד`
    # returned the building's own point graded `static`, claiming the flat IS there.
    # Nothing in the current 321 listings says "near", so this fires on nothing today —
    # it is here because grading `הבלוק` precise turns tomorrow's `ליד הבלוק` from a
    # vague blob into a confident wrong dot. `מגדלי דוד, סורוקה` has no proximity word
    # and keeps its precise grade.
    near_only = best_coords is None and near_coords is not None
    if near_only:
        best_coords, best_key = near_coords, near_key
    if best_coords is not None:
        # a whole-neighbourhood key is an AREA centroid, not a place — see _static_source
        src = "static_area" if near_only else _static_source(best_key)
        # A BUILDING WITH NO HOUSE NUMBER IS NOT AN EXACT LOCATION — UNLESS IT IS A
        # LANDMARK (user's rule, 2026-08-12). `_static_source` grades the KEY, and a
        # street sitting in STATIC_TABLE grades `static` exactly like a pinned place does,
        # so `רינגלבלום` with no number came back `exact`: 14 listings on one point, each
        # claiming a specific building. The exception is real and narrow — a SURVEYED
        # landmark is a place with a drawn outline, so `הבלוק` on its own genuinely is
        # exact, and `_landmark_grade` has already sized it.
        #
        # This is not cosmetic. `static` is in `_PRECISE_SOURCES`, so it also bought
        # `edge_grace` (AMBER->GREEN within 40 m) and skipped the boundary-street caution
        # in `pipeline._classify` — the very checks that exist for street-level points.
        # Downgrading routes these through the same caution as `overpass`/`nominatim`.
        if src == "static" and not numbered and best_key not in landmarks():
            src = "static_street"
        # A DRAWN OUTLINE'S CENTRE BEATS A DROPPED PIN. `הבלוק` moved 67 m and `אביסרור`
        # 89 m onto their surveyed centroids.
        best_coords = landmark_point(best_key) or best_coords
        return best_coords, src

    # 1b) house-number interpolation — local, free and more precise than any street-level
    #     hit: place the number between the known OSM address nodes on that street. Only
    #     fires for a numbered address on an anchored street, and never extrapolates.
    hn = _house_number(location_text)
    if hn:
        for cand in _candidate_tokens(location_text)[:2]:
            real, _how = streets.canonical(cand)
            pt, how = place_house(real or cand, hn)
            # The SECOND candidate can be a different real street with a similar name,
            # and placing there is worse than not placing at all: `דרך מצדה 69` fell
            # through to `מצדה` and landed 585 m away, looking fully confident. Hold an
            # internal placement to the same distance rule the external tiers obey.
            if pt and _plausible_external(location_text, pt, how):
                return pt, how

    # 2) cache of earlier lookups (success or a still-fresh miss)
    kind, coords, source = _cache_lookup(norm)
    if kind == "hit":
        return coords, source
    if kind == "miss":
        # A CACHED MISS IS ABOUT THE GEOCODERS, NOT ABOUT US. The entry records that
        # Overpass and Nominatim had no answer for this string; it says nothing about the
        # polyline sitting in area_features.json. Returning None here is what kept
        # `רחוב רמב"ם` unplaced even with the street tier below in the file — the miss is
        # cached the moment the externals fail, so from the second lookup onward the tier
        # was never reached. Anything that can answer WITHOUT a network call has to be
        # consulted on this path too.
        #
        # `_nearest_anchor_point` is here for the SAME reason, and was caught by the same
        # trap: `שדרות יצחק רגר 134` placed correctly in isolation and came back unplaced
        # in the hold-out, because that harness resolves an address twice — the first pass
        # cached the miss and the second returned from here, above the tier. A local
        # answer must never be gated on whether a geocoder once failed.
        return (_bare_street_point(location_text)
                or _nearest_anchor_point(location_text)
                or (None, None))

    # 2b) "NEAR X" IS NOT AN ADDRESS, so no external geocoder may answer it.
    #
    # Removing the university from _LANDMARKS was only half of the 2026-08-01 decision:
    # the phrase still fell through to Overpass, which answered `ליד האוניברסיטה` with a
    # point
    # outside the campus polygon — so the no-housing mask did not catch it either — and
    # two listings came back as AMBER MATCHes in the next replay. `_plausible_external`
    # cannot help: it ABSTAINS when there is no street to measure against, which is
    # exactly this case.
    #
    # A pure proximity phrase names a RELATIONSHIP, not a place. If there is no street
    # and no house number, the only honest answer is nothing — the listing goes to
    # NEEDS_DATA where a person sees it. `_descriptive_landmark` below still runs, so
    # `ליד הבלוק` (a real residential quarter we hold a point for) keeps working.
    if _is_bare_proximity(location_text):
        lm = _descriptive_landmark(location_text)
        return (lm, "landmark") if lm else (None, None)

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
        # …AND THAT COMMENT WAS NOT TRUE UNTIL 2026-08-13. `skipped_grade` is
        # `_static_source(key)` of the key we stepped over, and that grades the KEY: a
        # street or a surveyed landmark sitting in STATIC_TABLE both come back `static`,
        # i.e. `exact` AND `is_precise_source`. So this fallback — reached only when the
        # house number could NOT be placed — handed back a street point labelled as a
        # specific building, which then bought `edge_grace` and skipped the
        # boundary-street caution meant for exactly this case.
        #
        # Same rule as the one that fixed the other branch, and stricter here: we KNOW a
        # number was given and we KNOW we failed to place it, so no landmark exception
        # applies. Only an already-WORSE grade survives — `static_area` stays an area.
        if skipped_key is not None and _normalize(skipped_key) in landmarks():
            grade = skipped_grade          # a surveyed landmark keeps its own grade
        elif skipped_grade == "static_area":
            grade = "static_area"          # already worse; leave it
        else:
            grade = "static_street"
        return skipped_street_coords, grade
    # 4) THE STREET'S OWN POLYLINE. We hold the geometry of all 1,174 named streets in the
    # box, so an address naming one of them is never really unknown — `רחוב רמב"ם` was the
    # last of 322 listings that named a resolvable street and still had no location, and
    # under the 2026-08-03 drop rule an unlocated listing can be deleted outright. The
    # externals could not match the gershayim spelling; they did not need to.
    # Cannot collide with the fallback above: `skipped_street_coords` is only ever
    # recorded for an address that carries a house number, and this tier refuses one.
    bare = _bare_street_point(location_text)
    if bare:
        if authoritative:
            _remember_miss(norm)   # the geocoders really did fail — don't re-ask every run
        return bare
    # 5) THE NEAREST NUMBERED ANCHOR ON THE SAME STREET. Runs AFTER the externals, never
    # before — that ordering is the whole point. Placing house 3 at anchor 4 up front
    # pre-empts `place_house`'s extrapolation tier, which is better tuned than this and
    # was measured beating it 12 cases to 4. Down here it only catches what nothing else
    # could answer, which after `_contradicts_anchors` includes the externals we now
    # refuse: `שדרות בנ״צ כרמל 3` was left unplaced by that refusal, and anchor 4 is 125 m
    # from the truth against nominatim's 2,090 m.
    near = _nearest_anchor_point(location_text)
    if near:
        if authoritative:
            _remember_miss(norm)
        return near
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
        _remember_miss(norm)
    return None, None


def _remember_miss(norm: str) -> None:
    """Record that the external geocoders had no answer for this string, so it isn't
    re-queried on every run (a miss costs ~1s per Overpass mirror).

    A negative entry, NOT a verdict on the address: `_cache_lookup`'s miss branch answers
    from the street geometry anyway, which is why the street tier can record one and still
    return a point."""
    _load_cache()[norm] = {"m": datetime.now().isoformat(timespec="seconds"),
                           "v": GEOCODE_LOGIC_VERSION}
    _save_cache()


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

# TAILS THAT ARE NOT PART OF A STREET NAME. A post often ends the address with what the
# flat is like rather than where it is — a floor, an entrance, a parenthetical aside —
# and `_SPLIT_RE` only splits on punctuation, so the whole lot was glued into one token:
# `וינגייט 74 שכונה ג קומה שניה` produced the candidate `וינגייט קומה שניה`, which
# matches no street, even though `canonical("וינגייט")` is exact. Measured over the
# listings table, this and the parenthetical below are the two cheapest recall wins left.
_TAIL_STRIP = re.compile(
    r"\([^)]*\)"                                   # "(כיכר האבות)", "( ו הישנה)"
    r"|\bקומ[הת]\s*\S*"                            # קומה שניה / קומה 2 / קומת קרקע
    r"|\bכניס[הת]\s*\S*"                           # כניסה ב
    r"|\bדיר[הת]\s+\d+"                            # דירה 4
    r"|\bמעל\s+\S+"                                # מעל הסופר
)


def _overpass_name(location_text: str) -> str:
    s = _CITY_RE.sub(" ", location_text)                    # "…, באר שבע" pollutes the query
    s = _TAIL_STRIP.sub(" ", s)                             # "קומה שניה", "(ו' הישנה)"
    s = _OVERPASS_STRIP.sub(" ", s)
    s = s.translate(str.maketrans("", "", '"\\/,'))         # keep the QL string safe; drop commas
    return re.sub(r"\s+", " ", s).strip()


# Same, but KEEPING the street-type word. `דרך`/`שדרות`/`סמטת` are usually noise, and
# stripping them is what makes `רחוב רינגלבלום 5` match OSM's `רינגלבלום`. Sometimes
# they are part of the name, and then stripping them names a DIFFERENT REAL STREET:
# `דרך מצדה 69` was placed on `מצדה`, 585 m from דרך מצדה — caught by audit_geocode.
_KEEP_TYPE_STRIP = re.compile(r"\d+(?:/\d+)?|שכונ[הת]\s*[א-י]?['׳]?")


def _typed_name(location_text: str) -> str:
    s = _CITY_RE.sub(" ", location_text)
    s = _TAIL_STRIP.sub(" ", s)
    s = _KEEP_TYPE_STRIP.sub(" ", s)
    s = s.translate(str.maketrans("", "", '"\\/,'))
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
    # Strip the descriptive tail BEFORE splitting: `_SPLIT_RE` breaks on `מול`, so
    # `מצדה 17 (מול הפארק)` was torn into `מצדה (` and `הפארק)` and neither resolved.
    base = _TAIL_STRIP.sub(" ", _CITY_RE.sub(" ", location_text))
    out: list = []

    def add(tok):
        tok = (tok or "").strip()
        if tok and len(tok) >= 2 and tok not in out:
            out.append(tok)

    parts = [p for p in _SPLIT_RE.split(base) if p and p.strip()]
    # A street whose name INCLUDES its type word wins outright, but only on an EXACT
    # index match — a fuzzy one would let "רחוב רגר" invent a street. See _typed_name.
    for p in parts:
        real, how = streets.canonical(_typed_name(p))
        if real and how == "exact":
            add(real)
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
# The university and Soroka USED to be here, pointing at the campus centre and the
# hospital. Both points test INSIDE their own no-housing polygon, so every listing whose
# address was only `ליד האוניברסיטה` got a dot in the middle of a campus nobody can rent
# in — 8 of them. "Near the university" is not a location; those now resolve to nothing
# and land in NEEDS_DATA, where a human sees them (user's decision, 2026-08-01).
# `הבלוק` stays: it is a real residential quarter, and it is graded `static_area` so it
# already draws as the area it is rather than as a building.
_LANDMARKS = (
    (("הבלוק", "בבלוק"), (31.259386, 34.796130)),
)
# "near / next to / opposite / close to" — the phrasings that make a landmark the
# subject of the address rather than a passing mention.
_NEAR_RE = re.compile(r"ליד|קרוב\s+ל|בסמוך|קרבת|צמוד\s+ל|מול\s+שער|במרחק")


# Places nobody rents a room in. An address that names one of these and nothing else is
# not a housing address at all — the poster is telling you what they are near.
#
# Matched as a PHRASE, and removed before the "does it name a street" test below. The
# institution's own words look like a street to the index — `בן גוריון` resolves to the
# boulevard — so testing the raw text let `אוניברסיטת בן גוריון` claim to name a street
# and slip past this guard entirely.
_INSTITUTION_RE = re.compile(
    r"אוניברסיט\S*(?:\s+בן\s+גוריון)?|בן\s+גוריון\s+אוניברסיט\S*"
    r"|סורוקה|בית\s+חולים|ביה[\"״']?ח|הקריה\s+הרפואית|קמפוס|שער\s+האוניברסיטה")


def _is_bare_proximity(location_text: Optional[str]) -> bool:
    """Is this string something other than an address — a bearing off a landmark, or an
    institution named on its own — with no street and no house number of its own?

    Two shapes, one rule. "near the university" is a relationship, not a place; and
    `אוניברסיטת בן גוריון` as a whole address is a place NOBODY LIVES (user's decision,
    2026-08-03) — it resolved through Overpass to a real campus coordinate, which is a
    dot on a lawn. Both must resolve to nothing so the listing lands in NEEDS_DATA where
    a person sees it.

    Requires no house number AND no street we can name, so `רגר 5, ליד האוניברסיטה` and
    `ליד הבלוק ברינגלבלום` are untouched — and so is `מגדלי דוד, סורוקה`, because the
    static table answers a named building before this ever runs."""
    text = _fold_quotes(location_text or "")
    if not text:
        return False
    if not (_NEAR_RE.search(text) or _INSTITUTION_RE.search(text)):
        return False
    if _house_number(text):
        return False
    # Ask whether anything OTHER than the institution names a street:
    # `רינגלבלום ליד האוניברסיטה` keeps רינגלבלום and resolves, while
    # `אוניברסיטת בן גוריון` has nothing left once the institution is removed.
    rest = _INSTITUTION_RE.sub(" ", text)
    return not any(streets.canonical(c)[0] for c in _candidate_tokens(rest))


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

# Anchors the USER placed by hand, kept in their own file so a sloppy pin is auditable
# and deletable, and so rebuilding from the PBF (load_osm_addresses.py) can never wipe
# them. They win over OSM: someone looked at the map and said "number 140 is here".
_USER_ANCHORS_PATH = config.ROOT / "user_anchors.json"

# Anchors seeded once from govmap (seed_anchors.py) for the 199 relevant streets where
# OSM has fewer than the two house numbers interpolation needs. Its own file so a bad
# batch can be deleted wholesale, and so a PBF rebuild cannot wipe it.
_GOVMAP_ANCHORS_PATH = config.ROOT / "govmap_anchors.json"

# A hand-placed anchor still has to be near the street it claims — the same rule
# load_osm_addresses applies to OSM's own data, for the same reason. Generous, because
# a street's OSM geometry can be a partial fragment: this rejects "you tapped a
# different street", not "you were a few metres off".
MAX_ANCHOR_OFFSET_M = 200.0

_anchors: Optional[dict] = None


def _load_anchors() -> dict:
    """Every known house-number anchor, in ascending order of authority.

    OSM is a survey, so it is the base. govmap only FILLS GAPS — it never replaces an
    OSM point, so a one-off bad seed cannot degrade a street that already worked. A user
    pin overrides both: a person looked at the map and said where the flat is."""
    global _anchors
    if _anchors is None:
        try:
            _anchors = json.loads(_ANCHORS_PATH.read_text(encoding="utf-8"))
        except Exception:
            _anchors = {}
        try:
            for street, nums in json.loads(
                    _GOVMAP_ANCHORS_PATH.read_text(encoding="utf-8")).items():
                have = _anchors.setdefault(street, {})
                for num, pt in nums.items():
                    have.setdefault(num, pt)           # fill only, never overwrite
        except Exception:
            pass
        try:
            for street, nums in json.loads(
                    _USER_ANCHORS_PATH.read_text(encoding="utf-8")).items():
                _anchors.setdefault(street, {}).update(nums)
        except Exception:
            pass
        # An anchor on institutional land is wrong wherever it came from, and it poisons
        # its neighbours: `יוסף בן מתיתיהו 97` sits inside the campus, and interpolating
        # between it and 77 put number 90 on the university lawn. 13 anchors across six
        # streets were like this.
        import zones
        dropped = 0
        for street, nums in list(_anchors.items()):
            for num, pt in list(nums.items()):
                if zones.no_housing_here(pt[0], pt[1]):
                    del nums[num]
                    dropped += 1
        if dropped:
            print(f"[geocode] dropped {dropped} anchor(s) inside a campus/hospital")
        # A road OSM split across two spellings also has its house numbers split across
        # two anchor sets, and pooling the geometry alone does not fix that: `דרך מצדה`
        # held one anchor while `מצדה` held 21, so `דרך מצדה 69` projected off a single
        # point and landed 585 m away. Every spelling now sees the whole road's numbers.
        # The pool is the touching-geometry one from streets.py, so this cannot merge
        # two different streets that happen to share a word bag.
        own = {s: dict(n) for s, n in _anchors.items()}
        for pool in streets.pools():
            union: dict = {}
            for alias in pool:
                for num, pt in (own.get(alias) or {}).items():
                    union.setdefault(num, pt)
            if not union:
                continue
            for alias in pool:                     # every spelling, even one with none
                have = _anchors.setdefault(alias, {})
                for num, pt in union.items():
                    have.setdefault(num, pt)       # the spelling's own numbers win
    return _anchors


def _off_street_m(street: str, lat: float, lon: float) -> Optional[float]:
    """Metres from (lat, lon) to the nearest point of `street`'s geometry, or None if
    we don't know where the street is."""
    segs = streets.geometry(street) if street else []
    if not segs:
        return None
    return min(_haversine_m(lat, lon, p[0], p[1]) for seg in segs for p in seg)


def add_anchor(street: str, number: str, lat: float, lon: float) -> bool:
    """Teach the geocoder that `street number` is at (lat, lon). True if accepted.

    A pin fixes one listing; an anchor fixes the STREET — every other flat between it
    and the next known number, and every future one. That is the only mechanism that
    can ever place a house on the streets OSM has no addresses for at all, because the
    numbering origin of a street cannot be derived from free data (measured: "low
    numbers nearer the centre" holds for 64% of streets, so guessing it is a coin flip
    that lands at the wrong END).

    Refused when the point is more than MAX_ANCHOR_OFFSET_M from the street's geometry,
    so one mis-tap cannot poison every address on a street. The listing's own manual
    location is stored separately and still applies."""
    street = (street or "").strip()
    number = str(number or "").strip()
    if not street or not number.isdigit():
        return False
    off = _off_street_m(street, lat, lon)
    if off is not None and off > MAX_ANCHOR_OFFSET_M:
        print(f"[geocode] refused anchor {street} {number}: {off:.0f} m off that street")
        return False
    try:
        data = json.loads(_USER_ANCHORS_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault(street, {})[number] = [round(lat, 6), round(lon, 6)]
    _USER_ANCHORS_PATH.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                             indent=1), encoding="utf-8")
    global _anchors, _median_gap
    _anchors = None                        # both are derived from the anchor set
    _median_gap = None
    return True


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
# small: interpolation is good to a few tens of metres, so a wide radius lets a point
# jump to the NEXT building along, trading a small honest error for a confident wrong
# one. Swept against the 705-address hold-out (p50 / p90, snapping place_house):
#
#     off  18.7 / 97.9     25m  16.0 / 97.2     40m  15.9 / 102.5
#     15m  18.7 / 99.3     30m  15.6 / 102.5    60m  16.0 / 100.1
#
# 25 m is the only setting that improves BOTH; past it the median keeps creeping down
# while the tail gets worse, which is exactly the wrong trade for a map you act on.
SNAP_TO_BUILDING_M = 25.0

# Counting the buildings between two anchors (number 14 is the second building after
# number 10, not 40% of the way to number 20) was built and measured, and it does not
# work: p50 19.0 m against 18.4 m for plain interpolation when the building count has
# to match the house-number gap exactly, and steadily worse as that tolerance is
# loosened (20.0 m at ±1, 20.2 m at ±3). Sheds, stairwells and garages are footprints
# too, so "the buildings between these anchors" is not the list of addresses between
# them. Not implemented — recorded here so it is not rediscovered as a good idea.


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


def _same_parity_neighbour(known: dict, number: Optional[str]):
    """(lat, lon) of a same-parity anchor one or two numbers away — but ONLY when the
    same-parity anchors cannot bracket `number` themselves.

    That condition is the whole point, and it is narrow deliberately. It fires exactly when
    `_anchors_for` would give up on the correct side of the street and fall back to ALL
    anchors, which is where the damage is: on `שמעון בר גיורא` the odd numbers sit ~200 m
    from the even ones, number **26** has no even anchor above 24, and mixing bracketed it
    between odd 25 and 27 on the far arm — 200 m out, turning a RED flat GREEN. Even 24 was
    two numbers away and 6 m from the truth.

    Extrapolation cannot be left to catch this either: `place_house` projects from
    `anchors[0]`/`anchors[-1]` across BOTH parities, so with odds running to 39 it would
    project number 26 from anchor 1.

    Graded `anchor_neighbour` -> "street", not "high": this is the house next door, not the
    house asked for, and the confidence should say so. Bounded by `NEIGHBOUR_MAX_NUMBERS`
    (2) for the reason that constant already gives — beyond next-door it is a different
    part of the road.

    An earlier attempt tried to DETECT the split-parity streets instead, by comparing the
    two sides' centroids. It is recorded in `dead-ends`: measured outright it flagged 75 of
    347 streets, measured perpendicular to the axis it flagged 39 and missed the one street
    it was written for. This needs no such notion — it asks only whether the right side of
    the street can answer without help.
    """
    try:
        n = int(str(number or "").strip())
    except (TypeError, ValueError):
        return None
    same = sorted(int(k) for k in known
                  if str(k).isdigit() and int(k) % 2 == n % 2)
    if not same or same[0] <= n <= same[-1]:
        return None                     # nothing to offer, or interpolation can do it right
    best = min(same, key=lambda a: abs(a - n))
    if abs(best - n) > NEIGHBOUR_MAX_NUMBERS:
        return None
    pt = known[str(best)]
    return (pt[0], pt[1])


def place_house(street: Optional[str], number: Optional[str]):
    """((lat, lon), source) for a house number on a street, or (None, None).

    Three cases, in descending order of evidence:
      • the number sits between two known anchors  -> "interpolated"
      • it sits past them, within MAX_EXTRAPOLATE_M -> "extrapolated"
      • the street has ONE anchor, and the number is within MAX_EXTRAPOLATE_M of it at
        the city's typical spacing                 -> "projected"

    "extrapolated" is deliberately NOT in _PRECISE_SOURCES: it is a projection, not a
    survey, so `pipeline._classify` keeps applying its boundary-street and near-edge
    caution to it. It is graded `high` for display because it is still far better than
    the street centroid it replaces.

    Both answers are finally nudged onto the nearest real building (`snap_to_building`)
    when one is within 25 m — an address is a structure, and arithmetic on its own can
    land in a garden or a car park. A number we have an anchor FOR is exempt: that point
    is evidence, not arithmetic, and snapping it would answer a hand-placed pin with a
    coordinate 20 m from where the person put it."""
    known = _load_anchors().get(street) or {}
    hit = known.get(str(number or "").strip())
    if hit:
        return (hit[0], hit[1]), "osm_addr"
    near = _same_parity_neighbour(known, number)
    if near:
        return near, "anchor_neighbour"
    pt = interpolate_house(street, number)
    if pt:
        return snap_to_building(pt), "interpolated"
    if not street or not number:
        return None, None
    try:
        n = int(number)
    except (TypeError, ValueError):
        return None, None

    pts, idx = _street_axis(street)
    anchors = sorted((int(k), v[idx]) for k, v in known.items() if str(k).isdigit())
    if not anchors or len(pts) < 2:
        return None, None

    how = "extrapolated"
    if len(anchors) >= 2:
        # project past whichever end we fell off, using THIS street's own gradient
        lo, hi = anchors[0], anchors[-1]
        span_n, span_p = hi[0] - lo[0], hi[1] - lo[1]
        if span_n <= 0:
            return None, None
        per = span_p / span_n
        # A gradient wildly unlike the city's measured 11.2 m per number means the
        # anchors are not laid out along this street the way house numbers are. On
        # אלכסנדר ינאי both anchors (8 and 14) sit PAST the end of the street's own
        # polyline, 16 m apart across 6 numbers, giving 2.7 m per number — and every
        # number from 17 to 32 then projected to the same clamped endpoint. Fall back to
        # the city's typical spacing, which is what the single-anchor branch already
        # trusts, rather than believing a degenerate pair.
        typical = _median_metres_per_number() / 111320.0
        if not (0.25 * typical <= abs(per) <= 4.0 * typical):
            per = typical if per >= 0 else -typical
        edge = hi if n > hi[0] else lo
        target = edge[1] + (n - edge[0]) * per
        overshoot = abs(target - edge[1]) * 111320.0
    else:
        how = "projected"                      # one anchor: the DIRECTION is a guess
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
    # Past the end of the street's own polyline there is nothing left to project ONTO.
    # `_point_on_axis` clamps to the last vertex, which silently answered seven different
    # אלכסנדר ינאי numbers (17, 19, 21, 23, 28, 30, 32) with one identical point, graded
    # `high` and drawn as a confident dot. Refuse instead: the address falls through to
    # the tiers that can answer, and a hollow street-level dot beats a precise wrong one.
    lo_p, hi_p = min(p[idx] for p in pts), max(p[idx] for p in pts)
    if not (lo_p <= target <= hi_p):
        return None, None
    return snap_to_building(_point_on_axis(pts, idx, target)), how


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


def street_point(street: Optional[str]):
    """(lat, lon) in the MIDDLE of a street we hold the polyline for, or None.

    The degenerate case of `place_house`: with no house number there is no position along
    the street to solve for, so the honest answer is the centre of the line and a
    street-level grade to say so. Uses the same `_point_on_axis` machinery, at the
    midpoint of the street's own extent along its dominant axis — a target strictly
    BETWEEN the first and last vertex, so it can never be the clamped endpoint that
    answered seven אלכסנדר ינאי numbers with one point.

    Refused when the result lands more than MAX_ANCHOR_OFFSET_M from the street's
    geometry. That is not a formality: a name in the index can cover two roads that are
    not one road — the axis midpoint of `לימונית` sits 4.9 km from any לימונית vertex,
    because the halves are in different neighbourhoods and the midpoint is in the desert
    between them. Measured over all 1,172 named streets with geometry, the midpoint is a
    median 11 m from the nearest vertex and 33 fail this check — the same multi-kilometre
    error class the 200 m guard was written for, so they get no point at all."""
    pts, idx = _street_axis(street)
    if len(pts) < 2:
        return None
    mid = (pts[0][idx] + pts[-1][idx]) / 2.0            # _street_axis sorts along the axis
    pt = _point_on_axis(pts, idx, mid)
    off = _off_street_m(street, pt[0], pt[1])
    if off is None or off > MAX_ANCHOR_OFFSET_M:
        return None
    return pt


def _bare_street_point(location_text: Optional[str]):
    """((lat, lon), "street_geom") for an address that NAMES a street we hold the geometry
    for and gives no house number, else None.

    A NUMBER IS NEVER ANSWERED HERE. "אברהם אבינו 38" can't be the street's midpoint —
    that is how a red-end address read as green, and it is the whole reason house-number
    interpolation exists. A number that `place_house` cannot place keeps falling through
    to the tiers that might really know.

    The proximity guard is load-bearing, not belt-and-braces: `_named_street` reads the
    RAW text, and an institution's own words look like a street to the index —
    `אוניברסיטת בן גוריון` yields the boulevard `שדרות בן גוריון`. Without this, a place
    nobody lives would get a confident street-level dot, undoing the 2026-08-01 decision
    that "near the university" is not a location."""
    if not location_text or _house_number(location_text):
        return None
    if _is_bare_proximity(location_text):
        return None
    street = _named_street(location_text)
    if not street:
        return None
    pt = street_point(street)
    return (pt, "street_geom") if pt else None


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
        off = _off_street_m(real, lat, lon) if real else None
        if off is not None:
            return off
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
    bad = _contradicts_anchors(location_text, coords)
    if bad:
        print(f"[geocode] rejected {source} for {location_text!r}: {bad}")
        return False
    return True


# How far an external answer may sit from the nearest NUMBERED anchor on the same street,
# per house number of difference, before we call it a contradiction. The city's own median
# is ~11 m per number (`_median_metres_per_number`); 4x that plus a floor is loose enough
# for irregular numbering and still catches a kilometre-scale mistake.
ANCHOR_CONSISTENCY_SLACK = 4.0
ANCHOR_CONSISTENCY_FLOOR_M = 250.0


# How far apart two house numbers can be and still be "next door". Two, because odd and
# even run up opposite sides of the street: 3 and 5 are neighbours, 3 and 4 face each
# other. Anything beyond that is a different part of the road, and the `אלכסנדר ינאי`
# disaster (numbers 17-32 all answered by one clamped point) is what a loose bound buys.
NEIGHBOUR_MAX_NUMBERS = 2


def _anchors_on_claimed_street(location_text: str):
    """(house_number, [(number, coords), …]) for the street this address NAMES, or None.

    Shared by the consistency check and the last-resort placement so they can never
    disagree about which street and which anchors are in play."""
    n = _house_number(location_text)
    if not n:
        return None
    try:
        want = int(n)
    except (TypeError, ValueError):
        return None
    street = _named_street(location_text)
    if not street:
        return None
    known = _load_anchors().get(street) or {}
    numbered = sorted((int(k), v) for k, v in known.items() if str(k).isdigit())
    return (want, numbered) if numbered else None


def _nearest_anchor_point(location_text: str):
    """((lat, lon), "anchor_neighbour") — a NEIGHBOURING house's surveyed position.

    The honest last resort for a numbered address nothing else could place. It is a real
    surveyed point on the right street, which is far better than nothing, and it is NOT
    this house — so it is graded `street`, never `high`. Claiming precision here would be
    the exact lie `_CONFIDENCE` exists to prevent.

    BOUNDED BY HOUSE NUMBER, NOT ONLY BY DISTANCE. A metres-only bound let this answer
    `אלכסנדר ינאי 32` from anchor 14 — eighteen numbers away — because the city's median
    spacing put that inside MAX_EXTRAPOLATE_M. That street is the reason the projection
    guard exists at all: its anchors 8 and 14 sit past the end of its own polyline, and
    every number from 17 to 32 once resolved to one identical clamped point, graded `high`
    and drawn as a confident dot. Eighteen numbers is not next door.

    Both bounds apply: at most NEIGHBOUR_MAX_NUMBERS away in numbering, and no further
    than MAX_EXTRAPOLATE_M on the ground at the city's median spacing.
    """
    got = _anchors_on_claimed_street(location_text)
    if not got:
        return None
    want, numbered = got
    near_n, near_c = min(numbered, key=lambda a: abs(a[0] - want))
    delta = abs(near_n - want)
    if delta > NEIGHBOUR_MAX_NUMBERS:
        return None
    if delta * _median_metres_per_number() > MAX_EXTRAPOLATE_M:
        return None
    return (near_c[0], near_c[1]), "anchor_neighbour"


def _contradicts_anchors(location_text: str, coords) -> Optional[str]:
    """Why this external answer disagrees with the street's own house numbers, or None.

    BEING ON THE RIGHT STREET IS NOT ENOUGH. `שדרות בנ״צ כרמל 3` came back from nominatim
    2,090 m from the truth and 124 m off the street — inside the 250 m off-street
    threshold above, so that guard passed it — while our own anchors put houses 4 and 5
    just over 100 m from the truth. A point for number 3 that sits two kilometres from
    number 4 is not a placement, whatever street it is near.

    This is the third independent reason the address was mishandled, and the only one
    that was OUR bug: `interpolate_house` refused correctly (3 is outside the anchored
    range) and `place_house`'s extrapolation refused correctly (3 lies past the end of
    OSM's polyline for the street, where `_point_on_axis` would clamp and answer several
    numbers with one identical point). Both refusals were right. Accepting what filled
    the gap was not.

    Only fires when the street HAS numbered anchors, so it can never reject an answer for
    a street we know nothing about — those still fall through to the existing tiers.
    """
    got = _anchors_on_claimed_street(location_text)
    if not got:
        return None
    want, numbered = got
    near_n, near_c = min(numbered, key=lambda a: abs(a[0] - want))
    d = _haversine_m(near_c[0], near_c[1], coords[0], coords[1])
    allowed = max(ANCHOR_CONSISTENCY_FLOOR_M,
                  ANCHOR_CONSISTENCY_SLACK * _median_metres_per_number()
                  * max(1, abs(near_n - want)))
    if d <= allowed:
        return None
    return (f"{d:.0f} m from anchor {near_n} on the same street "
            f"(allowed {allowed:.0f} m for {abs(near_n - want)} house number(s))")


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
