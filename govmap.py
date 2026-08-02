"""
Address lookup against govmap.gov.il — free, keyless, Israeli government data.

    import govmap; govmap.address("בני אור 50")

WHY THIS EXISTS
---------------
199 of the 237 streets that matter to this bot have fewer than two OSM house-number
anchors, so `geocode.interpolate_house` cannot run on them at all and every flat there
falls onto one street centroid. Free OSM-derived geocoders (Nominatim, Photon, Geoapify,
LocationIQ, Pelias) all serve the same data we already hold in the PBF, so none of them
can help. Measured 2026-08-01: Nominatim answers `place_rank=26, addresstype=road` for
every numbered Be'er Sheva address.

govmap is the national mapping portal, and its own front end calls this endpoint
anonymously. Measured against 8 addresses OSM has surveyed exactly: **median error 5.4 m**
(range 4.1–12.6 m), which is better than this project's whole-pipeline p50 of 14 m.

THIS IS NOT A RUNTIME DEPENDENCY, BY DESIGN
-------------------------------------------
It is the app's own internal endpoint, not a documented API: it can change without
notice. So it is used ONCE, by `seed_anchors.py`, to write permanent anchors to disk —
after which local interpolation serves every future listing forever with no network at
all. `main.py` / `pipeline.py` / `replay.py` never call this module, and a test enforces
that.

IT SUBSTITUTES SILENTLY — VALIDATE EVERYTHING
---------------------------------------------
Measured, and the reason `verify=` below is not optional:
  • `בני אור 999`            -> returns `בני אור 13`, a DIFFERENT house number, no error
  • `רחוב שאיננו קיים 5 באר שבע` -> returns an address in **רמלה**, a different city
  • it normalises names: `ביאליק חיים נחמן` -> `ביאליק`, `סמטת קדש` -> `קדש`
An unvalidated anchor set would repeat the five `ההגנה` anchors that were once this
project's entire multi-kilometre error tail.
"""
from __future__ import annotations
import json
import math
import re
import time
import urllib.error
import urllib.request

URL = "https://www.govmap.gov.il/api/search-service/autocomplete"

# Be a good citizen on someone else's public service: identify ourselves, and never
# burst. The whole job is ~600 requests, once — this is not a crawl.
USER_AGENT = "bgu-housing-bot/1.0 (personal apartment search; one-time anchor seed)"
MIN_INTERVAL_SEC = 1.0
TIMEOUT_SEC = 20

# WKT points come back in Web Mercator. A CRS mistake would show up as kilometres, and
# the 4–12 m errors measured against surveyed anchors confirm this is right.
_MERC_R = 20037508.34
_POINT_RE = re.compile(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)")

_last_call = 0.0
calls = 0                       # so the seeder can report and cap actual network use


def _merc_to_wgs(x: float, y: float):
    lon = x / _MERC_R * 180.0
    lat = math.degrees(2 * math.atan(math.exp(y * math.pi / _MERC_R)) - math.pi / 2)
    return lat, lon


def _pace():
    global _last_call
    wait = MIN_INTERVAL_SEC - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def search(text: str) -> list:
    """Raw results for a query: [(type, text, (lat, lon))]. [] on any failure —
    a seeding run must not die because one lookup timed out."""
    global calls
    _pace()
    body = json.dumps({"searchText": text}).encode("utf-8")
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": USER_AGENT})
    try:
        calls += 1
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"[govmap] {text!r}: {exc}")
        return []
    out = []
    for res in data.get("results") or []:
        m = _POINT_RE.match(res.get("shape") or "")
        if not m:
            continue                       # MULTIPOINT (bus routes) and friends
        out.append((res.get("type"), res.get("text") or "",
                    _merc_to_wgs(float(m.group(1)), float(m.group(2)))))
    return out


_CITY_OK = ("באר שבע", "באר-שבע")


def _has_number(text: str, number: str) -> bool:
    """Is `number` present in the result as a standalone house number?

    A plain `in` test would accept `13` inside `130`, and accept the `5` of a
    neighbourhood suffix. The number must stand alone (optionally with a Hebrew entrance
    letter, `14א`)."""
    return re.search(rf"(?<!\d){re.escape(number)}[א-ת]?(?!\d)", text) is not None


def address_detail(street: str, number: str, verify=None):
    """(govmap's own answer text, (lat, lon)) for `street number` in Be'er Sheva,
    or (None, None).

    `verify(street_text) -> bool` is called with the street part of govmap's own answer
    so the caller can canonicalise it — govmap renames (`ביאליק חיים נחמן` -> `ביאליק`),
    so a raw string comparison would throw away good data, and no comparison at all would
    accept a different street.

    The TEXT comes back as well as the point because a confirm step has to show what was
    actually answered, not what was asked. The guards below reject the substitutions we
    have measured, but a person can only judge an answer they can read."""
    number = str(number).strip()
    for kind, text, pt in search(f"{street} {number} באר שבע"):
        if kind != "address":
            continue                                   # street / poi / transportation
        if not any(c in text for c in _CITY_OK):
            continue                                   # the רמלה case
        if not _has_number(text, number):
            continue                                   # the `999 -> 13` case
        if verify is not None:
            # everything before the house number is the street govmap thinks it answered
            head = re.split(rf"(?<!\d){re.escape(number)}[א-ת]?(?!\d)", text)[0]
            if not verify(head.strip()):
                continue
        return text, pt
    return None, None


def address(street: str, number: str, verify=None):
    """(lat, lon) for `street number` in Be'er Sheva, or None."""
    return address_detail(street, number, verify)[1]


def place(name: str):
    """(lat, lon) for a named building or POI in Be'er Sheva, or None. Used for the
    listings whose whole address is a landmark (`מגדלי קרן`, `אגם תבור`)."""
    for kind, text, pt in search(f"{name} באר שבע"):
        if kind in ("poi", "institutes", "address") and any(c in text for c in _CITY_OK):
            return pt
    return None
