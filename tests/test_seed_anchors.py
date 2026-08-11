"""`seed_conflict` — refusing a govmap answer that contradicts the surveyed numbering.

The failure it exists for, measured 2026-08-11: `_accept` checked only that a point was
NEAR the street, so govmap answering `שדרות יצחק רגר 163` with a point 409 m along רגר was
written as an anchor — it IS on רגר, it simply is not where 163 is. 1,050 seeded anchors
then made the hold-out WORSE at the tail (max 436 m -> 715 m, coverage 249 -> 248), because
a bad anchor does double damage: `geocode._contradicts_anchors` rejects the correct
interpolation for disagreeing with it, and then the bad anchor becomes the fallback answer.

Numbers are monkeypatched rather than read from the real survey, so these stay true when
`house_anchors.json` is rebuilt.
"""
from __future__ import annotations

import geocode
import seed_anchors

STREET = "רחוב הבדיקה"
BASE = (31.2500, 34.7900)


def _survey(monkeypatch, **nums):
    monkeypatch.setattr(seed_anchors, "_osm_anchors_cache", {STREET: dict(nums)})


def _north_of(base, metres):
    """A point `metres` due north — 1 degree of latitude is ~111,320 m."""
    return (base[0] + metres / 111_320.0, base[1])


def test_a_seed_beside_its_surveyed_neighbour_is_accepted(monkeypatch):
    _survey(monkeypatch, **{"11": list(BASE)})
    assert seed_anchors.seed_conflict(STREET, "12", _north_of(BASE, 40)) is None


def test_a_seed_far_from_its_surveyed_neighbour_is_refused(monkeypatch):
    """The רגר 163 shape: still on the street, nowhere near the number."""
    _survey(monkeypatch, **{"11": list(BASE)})
    why = seed_anchors.seed_conflict(STREET, "12", _north_of(BASE, 400))
    assert why and "surveyed anchor 11" in why


def test_the_allowance_grows_with_the_house_number_distance(monkeypatch):
    """One point, two claims. 333 m from number 1 is absurd for number 2 and unremarkable
    for number 21 — the rule is about metres PER HOUSE NUMBER, not raw distance."""
    _survey(monkeypatch, **{"1": list(BASE)})
    far = _north_of(BASE, 333)
    assert seed_anchors.seed_conflict(STREET, "2", far)
    assert seed_anchors.seed_conflict(STREET, "21", far) is None


def test_a_street_with_no_surveyed_numbers_is_never_refused(monkeypatch):
    """Only judges a seed when there is something to judge it against, so a street OSM has
    never numbered is seeded exactly as it was before this rule existed."""
    monkeypatch.setattr(seed_anchors, "_osm_anchors_cache", {})
    assert seed_anchors.seed_conflict(STREET, "12", _north_of(BASE, 5000)) is None


def test_the_seed_being_judged_is_not_its_own_evidence(monkeypatch):
    """A number already surveyed must not compare against ITSELF and trivially pass — that
    would let govmap overwrite a survey point with anything it liked."""
    _survey(monkeypatch, **{"11": list(BASE), "12": list(BASE)})
    assert seed_anchors.seed_conflict(STREET, "12", _north_of(BASE, 400))


def test_a_non_numeric_number_is_left_alone(monkeypatch):
    _survey(monkeypatch, **{"11": list(BASE)})
    assert seed_anchors.seed_conflict(STREET, "12א", _north_of(BASE, 400)) is None


def test_seeds_are_graded_against_the_survey_not_against_other_seeds(monkeypatch):
    """`_osm_anchors` reads house_anchors.json alone, NOT `geocode._load_anchors()`, which
    merges govmap in. Otherwise one bad seed becomes the evidence admitting the next."""
    import json

    monkeypatch.setattr(seed_anchors, "_osm_anchors_cache", None)
    surveyed = seed_anchors._osm_anchors()
    seeds = json.loads(geocode._GOVMAP_ANCHORS_PATH.read_text(encoding="utf-8"))

    # every (street, number) that only govmap knows must be absent from what grades it
    only_seeded = [(st, num) for st, nums in seeds.items() for num in nums
                   if num not in surveyed.get(st, {})]
    assert only_seeded, "no govmap-only anchors left to prove the point with"
    for street, num in only_seeded[:50]:
        assert num not in surveyed.get(street, {})
