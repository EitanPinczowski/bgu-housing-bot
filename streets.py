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


# Named ways in area_features.json that are NOT streets and must never answer an
# address. `תחנת רכבת צפון - אוניברסיטה` is the railway station, and because
# `_words_index` matches a unique word run, `האוניברסיטה` canonicalised straight to it —
# so `ליד האוניברסיטה` resolved to a platform 783 m from anywhere you could live, and
# came back as an AMBER MATCH. Measured 2026-08-02: exactly 1 of 1,174 entries.
#
# An explicit name, not a pattern on `תחנת`: one bad row is a fact, a heuristic is a
# guess. The durable fix is for load_area_features.py to stop importing railway
# features, which needs an Overpass run to regenerate area_features.json.
_NOT_STREETS = frozenset(("תחנת רכבת צפון - אוניברסיטה",))


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
        if name and name not in _NOT_STREETS:
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
# Honorifics OSM carries on some spellings of a road and not others, exactly like the
# road-type words above: `צבי קלישר` and `קלישר הרב צבי` are the SAME road — measured
# 0.0 m apart — but the `הרב` kept their word bags different, so they never pooled and
# `קלישר` stayed ambiguous and unresolvable.
_TITLES = frozenset(("הרב", "רב", "ד\"ר", "דר", "פרופ", "פרופסור", "הרבי"))


def _pool_key(name: str) -> str:
    """The bag of words that identifies a ROAD, ignoring word order, any leading
    road-type word, and any honorific. Two entries sharing this key are candidates to be
    pooled — the touch test below decides whether they actually are."""
    parts = _norm(name).split()
    while len(parts) > 1 and parts[0] in _ROAD_TYPES:
        parts = parts[1:]
    parts = [p for p in parts if p not in _TITLES] or parts
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
        if st.get("name") and st["name"] not in _NOT_STREETS:
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


def _strip_road_type(n: str) -> Optional[str]:
    """'רחוב בזל' -> 'בזל'. None when there is no leading road-type WORD to drop.

    `_pool_key` has always ignored these words when deciding whether two OSM entries are
    one road; the QUERY side did not. A post that writes the street out in full therefore
    reached only the word-run tier — and that tier requires a 4-character run, so a street
    whose name is shorter resolved to NOTHING at all. Measured: `בזל` alone is `exact`,
    `רחוב בזל` was `(None, None)`, while `רחוב רמב"ם` and `רחוב בעלי התוספות` worked and
    hid it. `בזל` is one of 194 single-word street names of four characters or fewer.

    Note this strips a WORD, not the single letter `_strip_prefix` handles — the two are
    different failures and both occur. The result only ever feeds an EXACT index lookup,
    so it cannot invent a fuzzy match."""
    parts = n.split()
    out = list(parts)
    while len(out) > 1 and out[0] in _ROAD_TYPES:
        out = out[1:]
    return " ".join(out) if out != parts else None


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
    # 2b) a leading road-type WORD dropped: 'רחוב בזל' -> 'בזל'. EXACT lookups only, and
    # the hit must be that street's OWN name — `_index` also holds the single-letter
    # prefix aliases, so a plain `road in idx` answers `כיכר אבות` with `אבות` -> `האבות`,
    # a street **2,506 m** away. That is the same trap tier 3b guards with this same test,
    # and it fired the moment this tier was added (`test_every_static_entry_sits_on_the
    # _street_it_names`). Deliberately NOT offered to the fuzzy tiers below for that
    # reason: rescuing a short name must not widen what a road-type word can match.
    road = _strip_road_type(n)
    if road and _norm(idx.get(road, "")) == road:
        return idx[road], "prefix"
    words = _words_index()                         # 3) UNIQUE word match ('רגר' -> 'שדרות יצחק רגר')
    for cand in filter(None, (n, bare)):
        hits = words.get(cand)
        if not hits or len(cand) < 3:
            continue
        if len(hits) == 1:
            return hits[0], "word"
        # SEVERAL HITS THAT ARE ONE ROAD IS NOT AMBIGUITY. `קלישר` matched both
        # `צבי קלישר` and `קלישר הרב צבי` — two OSM spellings of a single street whose
        # geometries touch at 0.0 m — and the uniqueness rule threw it away. When every
        # hit belongs to the same pool, answer with the pool's primary name.
        # `זבוטינסקי` still refuses: `ז'בוטינסקי` and `יוהנה זבוטינסקי` are 2,652 m
        # apart, so they never pooled, and refusing is the right answer there.
        pool = _pools().get(hits[0])
        if pool and all(h in pool for h in hits):
            return pool[0], "word"
    # 3b) A WORD RUN INSIDE the candidate. The street is often buried in a longer phrase
    # the tokenizer cannot know to cut: `הרב יוסף סוסו הכהן 15` and
    # `רחוב יצחק רגר בנייני היוקרה 5` both name a street the index holds, wrapped in an
    # honorific or a building name. `_words_index` already maps every 1-3 word run of
    # every real street name, so ask it about the candidate's own runs, longest first.
    #
    # A ONE-WORD RUN MUST BE A WHOLE STREET NAME; a longer run may be part of one.
    # Exact-index matching does not by itself prevent the `ברגר` -> `ברנר` class of
    # error: `מרכז הנגב 2` (a shopping mall) matched the run `מרכז` and resolved to the
    # street `מרכז אורן`, a different place entirely — a common noun picking a street.
    # Caught by auditing every address this tier newly resolved, which is the only way
    # to see it. `מרכז` appears in the index only INSIDE `מרכז אורן`, while `רמב"ם` is a
    # street in its own right, and that is exactly the line between the two.
    for cand in filter(None, (n, bare)):
        parts = cand.split()
        if len(parts) < 2:
            continue
        for size in range(min(len(parts), 3), 0, -1):
            if size == len(parts):
                continue                   # the whole token — tiers 1-3 already tried it
            for i in range(len(parts) - size + 1):
                run = " ".join(parts[i:i + size])
                if len(run) < 4:
                    continue
                if size == 1:
                    # …and it must be the street's OWN name, not one of the prefix
                    # aliases `_index` adds. `כיכר אבות` dissected to `אבות`, which is an
                    # alias for `האבות` — a street 2,506 m away. `רמב"ם` passes because
                    # it normalises to itself.
                    hit = idx.get(run)
                    if hit and _norm(hit) == run:
                        return hit, "word"
                    continue
                hits = words.get(run)
                if not hits:
                    continue
                if len(hits) == 1:
                    return hits[0], "word"
                pool = _pools().get(hits[0])
                if pool and all(h in pool for h in hits):
                    return pool[0], "word"

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
