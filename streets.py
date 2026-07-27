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


@lru_cache(maxsize=1)
def _geometry_index() -> dict:
    """{real OSM name -> [segment, …]} where a segment is [[lat, lon], …]."""
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict = {}
    for st in data.get("streets", []):
        if st.get("name"):
            out.setdefault(st["name"], []).extend(st.get("segments", []))
    return out


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
