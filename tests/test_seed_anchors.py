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

import pytest

import geocode
import seed_anchors

STREET = "רחוב הבדיקה"
BASE = (31.2500, 34.7900)


@pytest.fixture(autouse=True)
def _pin_median_gap(monkeypatch):
    """Pin the city's median metres-per-house-number, for two reasons.

    DETERMINISM: `seed_conflict`'s allowance is a multiple of it, so without pinning these
    tests move whenever the anchor data is rebuilt.

    AND, MORE IMPORTANTLY, TO STOP THIS FILE POISONING THE REST OF THE SUITE.
    `_median_metres_per_number` caches into the module global `geocode._median_gap` on
    first call. Several tests here fake `_street_axis`, so if that first call happened
    while a fake was installed, the garbage value was cached for the WHOLE session and
    surfaced as failures in unrelated tests — `test_a_landmark_mentioned_in_passing_never_
    hijacks_a_real_address` among them. It moved with `pytest-randomly`'s ordering and
    passed whenever run alone. Setting the global makes the getter return before it can
    compute anything from a fake.
    """
    monkeypatch.setattr(geocode, "_median_gap", 11.2)


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


def _one_street(monkeypatch, name: str, numbers: list):
    monkeypatch.setattr(geocode, "_load_anchors",
                        lambda: {name: {str(n): [31.25, 34.79] for n in numbers}})
    monkeypatch.setattr(seed_anchors.streets, "aliases", lambda s: set())
    monkeypatch.setattr(seed_anchors, "in_zone", lambda s: True)


def test_outward_probes_below_the_lowest_and_above_the_highest(monkeypatch):
    """`interpolate_house` refuses anything outside the anchored range, so an address past
    either end cannot be placed at all however dense the middle is."""
    _one_street(monkeypatch, "רחוב הבדיקה", [35, 37, 39])
    got = {tuple(nums[:3]) for _st, nums in seed_anchors.outward_runs()}
    assert (33, 31, 29) in got                     # downward, same parity, toward 1
    assert (41, 43, 45) in got                     # upward, same parity


def test_a_downward_run_stops_at_one(monkeypatch):
    """House numbers start at 1; walking past it wastes requests on nothing."""
    _one_street(monkeypatch, "רחוב הבדיקה", [5, 7])
    down = [nums for _st, nums in seed_anchors.outward_runs() if nums[0] < 5][0]
    assert down == [3, 1]


def test_a_street_with_no_numbered_anchor_has_no_outward_run(monkeypatch):
    """Nothing to walk outward FROM — there is no range to be outside of."""
    _one_street(monkeypatch, "רחוב הבדיקה", [])
    assert seed_anchors.outward_runs() == []


def test_pooled_spellings_are_probed_once(monkeypatch):
    """`דרך מצדה` and `מצדה` are one road; probing both doubles the request count for
    nothing, which is how the earlier gap estimate read nearly twice its real size."""
    monkeypatch.setattr(geocode, "_load_anchors", lambda: {
        "דרך מצדה": {"10": [31.25, 34.79]}, "מצדה": {"10": [31.25, 34.79]}})
    monkeypatch.setattr(seed_anchors.streets, "aliases",
                        lambda s: {"דרך מצדה", "מצדה"})
    monkeypatch.setattr(seed_anchors, "in_zone", lambda s: True)
    assert len({st for st, _nums in seed_anchors.outward_runs()}) == 1


def test_seed_outward_accepts_a_real_answer(monkeypatch, tmp_path):
    """`_accept` is handed govmap's OWN answer text, never the number we asked for.

    It parses the street and number out of that text, because govmap renames as it answers
    and the whole point is to check that what came back is the address we meant. Passing
    the bare number made `_ADDR_RE` fail on every result, so every answer was rejected and
    the run read as "no street has numbers past its end" — 710 requests, 0 anchors, on
    2026-08-11. Nothing about that failure looked like a bug from the outside.
    """
    monkeypatch.setattr(seed_anchors, "OUT_PATH", tmp_path / "seeds.json")
    monkeypatch.setattr(seed_anchors.streets, "canonical", lambda s: (STREET, ""))
    monkeypatch.setattr(geocode, "_off_street_m", lambda *a, **k: 0.0)
    monkeypatch.setattr(seed_anchors.zones, "no_housing_here", lambda *a, **k: False)
    monkeypatch.setattr(seed_anchors, "seed_conflict", lambda *a, **k: None)
    monkeypatch.setattr(seed_anchors.govmap, "calls", 0)
    monkeypatch.setattr(seed_anchors.govmap, "throttled", False)
    monkeypatch.setattr(seed_anchors.govmap, "consecutive_errors", 0)
    monkeypatch.setattr(seed_anchors.govmap, "search",
                        lambda q: [("address", f"{STREET} 20 באר שבע", (31.25, 34.79))])

    existing: dict = {}
    assert seed_anchors.seed_outward([(STREET, [20])], existing) == 1
    assert existing[STREET]["20"] == [31.25, 34.79]


def _axis(monkeypatch, idx=1):
    """A straight east-west street, so the axis coordinate is longitude."""
    monkeypatch.setattr(geocode, "_street_axis",
                        lambda street: ([(31.25, 34.79), (31.25, 34.80)], idx))


def _row(numbers_to_lon: dict) -> dict:
    return {str(n): [31.25, lon] for n, lon in numbers_to_lon.items()}


def test_longest_monotonic_keeps_the_run_and_names_the_stray():
    assert seed_anchors._longest_monotonic([1.0, 2.0, 3.0, 4.0]) == {0, 1, 2, 3}
    keep = seed_anchors._longest_monotonic([1.0, 2.0, 99.0, 3.0, 4.0])
    assert 2 not in keep and len(keep) == 4


def test_a_street_numbered_backwards_is_not_all_outliers():
    """Numbers may run either way along the axis — which end OSM's geometry starts at is
    an accident. Only DISAGREEMENT with the street's own direction is evidence."""
    assert len(seed_anchors._longest_monotonic([4.0, 3.0, 2.0, 1.0])) == 4


def test_an_anchor_out_of_order_along_the_street_is_flagged(monkeypatch):
    """The `הכנסת 18` shape: 16 sits where it should and the bracket around 18 does not."""
    _axis(monkeypatch)
    anchors = _row({2: 34.790, 4: 34.791, 6: 34.792, 8: 34.850, 10: 34.794})
    assert set(seed_anchors.order_outliers("רחוב הבדיקה", anchors)) == {"8"}


def test_too_few_anchors_to_establish_an_order_flags_nothing(monkeypatch):
    """With three anchors, 'two agree and one does not' is not evidence of anything."""
    _axis(monkeypatch)
    assert seed_anchors.order_outliers("רחוב הבדיקה",
                                       _row({2: 34.790, 4: 34.900, 6: 34.792})) == {}


def test_without_a_consensus_nothing_is_dropped(monkeypatch):
    """A street whose numbering genuinely jumps around must not be gutted — if the
    surviving run is not a real majority, the test itself is what is unreliable."""
    _axis(monkeypatch)
    scattered = _row({2: 34.795, 4: 34.791, 6: 34.799, 8: 34.792, 10: 34.797, 12: 34.790})
    assert seed_anchors.order_outliers("רחוב הבדיקה", scattered) == {}


def test_a_street_with_no_geometry_is_never_judged(monkeypatch):
    """No polyline, no axis, no opinion — rather than an opinion from nothing."""
    monkeypatch.setattr(geocode, "_street_axis", lambda street: ([], 0))
    assert seed_anchors.order_outliers("רחוב הבדיקה",
                                       _row({2: 34.79, 4: 34.90, 6: 34.79, 8: 34.79})) == {}


def test_odd_and_even_are_judged_separately(monkeypatch):
    """They run up opposite sides, so mixing them adds noise the consensus has to fight.
    Four clean evens and four clean odds interleave into one badly-behaved sequence."""
    _axis(monkeypatch)
    both = _row({1: 34.7901, 2: 34.7900, 3: 34.7911, 4: 34.7910,
                 5: 34.7921, 6: 34.7920, 7: 34.7931, 8: 34.7930})
    assert seed_anchors.order_outliers("רחוב הבדיקה", both) == {}


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
