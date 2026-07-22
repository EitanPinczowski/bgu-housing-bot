"""Golden-set regression test: a handful of representative listings with frozen
extracts + geocode/walk, run through the REAL pipeline._classify (zones, ב/ג/ד mask,
boundary-street rule, blacklist, fit score). Guards against a future threshold/zone/
rule change silently flipping a real listing's outcome. Deterministic & offline —
only the network boundaries (geocode, OSRM) are stubbed per case.
"""
import pipeline
from models import ListingExtract

# (label, extract kwargs, geocode (lat,lon,source) or None, walk_min, expected verdict, expected tier)
CASES = [
    ("הבלוק — student cluster, in ד, precise POI",
     dict(street_address_or_neighborhood="הבלוק", available_rooms_count=2,
          total_roommates_in_apt=3, price_per_room_ils=1500),
     (31.259386, 34.796130, "static"), 8.0, "MATCH", "GREEN"),
    ("אברהם אבינו 38 — boundary street, name-only placement",
     dict(street_address_or_neighborhood="אברהם אבינו 38", available_rooms_count=2,
          total_roommates_in_apt=3, price_per_room_ils=1500),
     (31.262, 34.795, "overpass"), 6.0, "DROP", "RED"),
    ("נאות לון — blacklisted neighborhood (pre-geocode drop)",
     dict(street_address_or_neighborhood="נאות לון", available_rooms_count=2,
          total_roommates_in_apt=3, price_per_room_ils=1500),
     None, None, "DROP", None),
    ("שכונה ה' — non-ב/ג/ד neighborhood letter",
     dict(street_address_or_neighborhood="שכונה ה', רחוב חוגלה", available_rooms_count=2,
          total_roommates_in_apt=3, price_per_room_ils=1500),
     None, None, "DROP", None),
    ("over-price — hard drop before routing",
     dict(street_address_or_neighborhood="הבלוק", available_rooms_count=2,
          total_roommates_in_apt=3, price_per_room_ils=3200),
     (31.259386, 34.796130, "static"), 8.0, "DROP", None),
    ("bare neighborhood — not a real address → NEEDS_DATA",
     dict(street_address_or_neighborhood="שכונה ד", available_rooms_count=2,
          total_roommates_in_apt=3, price_per_room_ils=1500),
     (31.2635, 34.7975, "static"), 12.0, "NEEDS_DATA", None),
]


def _run(monkeypatch, geo, walk, extract):
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed",
                        lambda a: ((geo[0], geo[1]), geo[2]) if geo else (None, None))
    monkeypatch.setattr(pipeline.osrm, "walk_to_nearest", lambda lat, lon: (walk, "gate"))
    e = ListingExtract(is_apartment_ad=True, **extract)
    return pipeline._classify(e, "", None, None, [], None, commit=False)


def test_golden_cases(monkeypatch):
    failures = []
    for label, extract, geo, walk, want_status, want_tier in CASES:
        res = _run(monkeypatch, geo, walk, extract)
        if res.status.value != want_status or (want_tier and res.location_tier != want_tier):
            failures.append(f"{label!r}: got {res.status.value}/{res.location_tier} "
                            f"want {want_status}/{want_tier} — {res.reason}")
    assert not failures, "golden regressions:\n" + "\n".join(failures)
