"""
Local index of every named Be'er Sheva street (from `area_features.json`, written by
load_area_features.py) — used to turn a messy Hebrew address token into a REAL OSM
street name before querying, and to hand back a street's geometry.

Why: posts write streets loosely — "ברגר 155" (a ב prefix on רגר), "הסנהדרין 69" (a ה
prefix), "רינגנבלום"/"רינגבלום" (misspellings of רינגלבלום). Resolving these locally
against the real name list recovers a lot of otherwise-unmappable listings, with no
network call.

SAFETY — the resolution order matters. `canonical` tries, in strict order:
    1. exact match
    2. exact match after stripping a leading ה/ב/ל/מ/ו/ש prefix
    3. fuzzy match, high cutoff only
Fuzzy is LAST and strict on purpose: "ברגר" fuzzy-matches "ברנר" — a real but DIFFERENT
street — while the prefix rule correctly yields "רגר". Cheap, safe tiers must win, or we
trade missed apartments for confidently-wrong ones.
"""
from __future__ import annotations
import difflib
import json
import re
from functools import lru_cache
from typing import Optional, Tuple

import config

_PATH = config.ROOT / "area_features.json"
# Only accept a fuzzy hit this close — below it, prefer "unknown" over a wrong street.
FUZZY_CUTOFF = 0.82
# Hebrew one-letter proclitics that get glued onto a street name in prose
# ("ברגר" = "in Reger", "הסנהדרין" = "the Sanhedrin").
_PREFIXES = "הבלמוש"


def _norm(s: str) -> str:
    """Comparison form: drop gershayim/quotes/punctuation, collapse spaces."""
    s = (s or "").translate(str.maketrans("", "", "\"'״׳`,.-"))
    return re.sub(r"\s+", " ", s).strip().lower()


@lru_cache(maxsize=1)
def _index() -> dict:
    """{normalized name -> real OSM name} for every named street, PLUS an alias for each
    name's prefix-stripped form so a post writing 'כ"ג' still finds OSM's 'הכ"ג'."""
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for st in data.get("streets", []):
        name = st.get("name")
        if name:
            out.setdefault(_norm(name), name)
    for key in list(out):                          # aliases never overwrite a real name
        bare = _strip_prefix(key)
        if bare:
            out.setdefault(bare, out[key])
    return out


@lru_cache(maxsize=1)
def _words_index() -> dict:
    """{word-subsequence -> [real names]} so a bare token can match a street whose OSM
    name carries extra words — 'רגר' -> 'שדרות יצחק רגר'. Only used when UNIQUE."""
    out: dict = {}
    for key, real in _index().items():
        parts = key.split()
        for size in range(1, min(len(parts), 3) + 1):        # 1..3-word runs
            for i in range(len(parts) - size + 1):
                out.setdefault(" ".join(parts[i:i + size]), set()).add(real)
    return {k: sorted(v) for k, v in out.items()}


# How close two same-words-different-order entries must come before we treat them as one
# road. ביאליק's two halves touch at 0 m; a coincidence of word order between genuinely
# separate roads would be hundreds of metres apart.
_MERGE_TOUCH_M = 50.0

# Road-type words OSM writes on some ways of a road and leaves off others, so the same
# street lands in two index entries: `דרך מצדה` holds 5 points and `מצדה` holds 225, and
# `דרך מצדה 69` then interpolated off a single anchor on a 5-point stub and came out
# 585 m from the street it names. Measured on this bbox, 10 pairs split this way; the two
# that are NOT the same road (`כיכר האבות`/`האבות`, `כיכר המדע`/`המדע`) sit 2.5 km apart
# and are rejected by the same touch test that catches a word-order coincidence.
_ROAD_TYPES = frozenset(("דרך", "רחוב", "שדרות", "שדרת", "סמטת", "סמטה", "שביל",
                         "כביש", "כיכר", "ככר"))


def _pool_key(name: str) -> str:
    """The bag of words that identifies a ROAD, ignoring word order and any leading
    road-type word. Two entries sharing this key are candidates to be pooled — the
    touch test below decides whether they actually are."""
    parts = _norm(name).split()
    while len(parts) > 1 and parts[0] in _ROAD_TYPES:
        parts = parts[1:]
    return " ".join(sorted(parts))


def _nearest_m(a: list, b: list) -> float:
    """Metres between the closest pair of vertices in two segment lists."""
    import math
    pa = [p for s in a for p in s]
    pb = [p for s in b for p in s]
    if not pa or not pb:
        return float("inf")
    best = float("inf")
    for la, lo in pa:
        scale = 111320.0 * math.cos(math.radians(la))
        for lb, ob in pb:
            d = math.hypot((lb - la) * 111320.0, (ob - lo) * scale)
            if d < best:
                best = d
                if best == 0.0:
                    return 0.0
    return best


@lru_cache(maxsize=1)
def _raw_geometry() -> dict:
    """{real OSM name -> [segment, …]} exactly as OSM labelled it, before pooling."""
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict = {}
    for st in data.get("streets", []):
        if st.get("name"):
            out.setdefault(st["name"], []).extend(st.get("segments", []))
    return out


@lru_cache(maxsize=1)
def _pools() -> dict:
    """{real OSM name -> every name that is the SAME ROAD, itself included}.

    OSM writes one road's name in more than one form, and each spelling then keeps its
    own fragment: `ביאליק חיים נחמן` held 135 m while `חיים נחמן ביאליק` held 2,849 m of
    the same street, their nearest vertices 0 m apart. A 135 m stub then fails every
    distance check for a house at number 122, which is how six ביאליק listings ended up
    sharing one dot. Two things split a road this way — word ORDER (12 name-sets) and a
    leading road-TYPE word (10 pairs, see _ROAD_TYPES).

    Pooling happens only when the fragments actually TOUCH. Welding two unrelated roads
    together because they share a word bag is precisely the class of error the 200 m
    guard exists to catch, and it must not be introduced here to satisfy that guard
    elsewhere: `כיכר האבות` and `האבות` share a bag and lie 2.5 km apart."""
    geo = _raw_geometry()
    by_words: dict = {}
    for name in geo:
        by_words.setdefault(_pool_key(name), []).append(name)
    out: dict = {}
    for names in by_words.values():
        if len(names) < 2:
            continue
        # pool everything that touches the largest fragment; leave the rest alone
        names.sort(key=lambda n: -sum(len(s) for s in geo[n]))
        main = names[0]
        joined = [main] + [o for o in names[1:]
                           if _nearest_m(geo[main], geo[o]) <= _MERGE_TOUCH_M]
        if len(joined) > 1:
            for n in joined:
                out[n] = joined
    return out


def pools() -> list:
    """Every pooled road as a tuple of its OSM names, largest fragment first, each pool
    listed once. `geocode._load_anchors` walks this to union the house numbers."""
    return sorted({tuple(p) for p in _pools().values()})


def aliases(street: Optional[str]) -> list:
    """Every OSM name for the same road as `street`, itself first.

    Pooling the GEOMETRY is only half the repair: anchors, pins and caches are all keyed
    by street name, so a road split across two spellings also has its house numbers split
    across two anchor sets. Callers use this to gather all of them under one road."""
    if not street:
        return []
    pool = _pools().get(street)
    return [street] + [n for n in pool if n != street] if pool else [street]


@lru_cache(maxsize=1)
def _geometry_index() -> dict:
    """{real OSM name -> [segment, …]}, with every spelling of one road seeing the
    whole road (see _pools)."""
    geo = dict(_raw_geometry())
    for name, pool in _pools().items():
        geo[name] = [s for n in pool for s in _raw_geometry().get(n, [])]
    return geo


def _strip_prefix(n: str) -> Optional[str]:
    """'ברגר' -> 'רגר', 'הכג' -> 'כג'. None if there's nothing sensible to strip.
    The result is only ever used for an EXACT lookup, so a short remainder is safe."""
    if len(n) >= 3 and n[0] in _PREFIXES:
        return n[1:]
    return None


@lru_cache(maxsize=512)
def canonical(name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(real_street_name, how) for a messy token, or (None, None) if we can't place it
    safely. `how` ∈ exact | prefix | fuzzy — the caller can weigh the confidence."""
    n = _norm(name)
    if not n or len(n) < 2:
        return None, None
    idx = _index()
    if n in idx:                                   # 1) exact (incl. the prefix aliases)
        return idx[n], "exact"
    bare = _strip_prefix(n)                        # 2) prefix-stripped exact (beats fuzzy!)
    if bare and bare in idx:
        return idx[bare], "prefix"
    words = _words_index()                         # 3) UNIQUE word match ('רגר' -> 'שדרות יצחק רגר')
    for cand in filter(None, (n, bare)):
        hits = words.get(cand)
        if hits and len(hits) == 1 and len(cand) >= 3:
            return hits[0], "word"
    for cand in filter(None, (n, bare)):           # 4) fuzzy, strict cutoff, last resort
        m = difflib.get_close_matches(cand, list(idx), n=1, cutoff=FUZZY_CUTOFF)
        if m:
            return idx[m[0]], "fuzzy"
    return None, None


def geometry(street: Optional[str]) -> list:
    """The street's polyline segments ([[lat, lon], …] each), or []."""
    return _geometry_index().get(street or "", [])


def known(street: Optional[str]) -> bool:
    return bool(street) and _norm(street) in _index()
