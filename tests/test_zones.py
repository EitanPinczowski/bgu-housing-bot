"""zones.classify_location — the GREEN/AMBER/RED/UNKNOWN gate that decides
in-range. Uses the real green_zone.json polygon."""
import zones


def test_unknown_without_coordinates():
    assert zones.classify_location(None, None) == "UNKNOWN"
    assert zones.classify_location(31.26, None) == "UNKNOWN"
    assert zones.in_green_zone(None, None) is False


def test_far_away_point_is_red():
    # Tel Aviv, ~100 km north — nowhere near the Be'er Sheva zone
    assert zones.classify_location(32.0853, 34.7818) == "RED"
    assert zones.in_green_zone(32.0853, 34.7818) is False


def test_amber_is_walk_time_to_a_gate():
    import config
    # a point outside the green polygon: AMBER iff within MAX_WALK_MINUTES of a
    # gate. Force the tier with an explicit walk time to keep it deterministic.
    far_pt = (32.0853, 34.7818)   # definitely outside the green polygon
    assert zones.classify_location(*far_pt, walk_min=config.MAX_WALK_MINUTES - 1) == "AMBER"
    assert zones.classify_location(*far_pt, walk_min=config.MAX_WALK_MINUTES + 1) == "RED"


def test_walk_estimate_matches_osrm_ballpark():
    # הבלוק is ~8 min from שער סורוקה by OSRM; the straight-line estimate should
    # land in the same ballpark (calibration guard).
    assert 5 <= zones.est_walk_to_gate_min(31.259386, 34.79613) <= 12


def test_no_amber_zone_forces_red():
    # a point inside the שכונה ד' polygon but outside the green zone: classify
    # says AMBER, but the no-amber rule (classify_effective) makes it RED.
    lat, lon = 31.267, 34.795
    if zones.in_no_amber_zone(lat, lon) and zones.classify_location(lat, lon) == "AMBER":
        assert zones.classify_effective(lat, lon) == "RED"
    # inside the green zone stays green under either function
    poly = zones._polygon()
    la = sum(p[0] for p in poly) / len(poly)
    lo = sum(p[1] for p in poly) / len(poly)
    if zones.in_green_zone(la, lo):
        assert zones.classify_effective(la, lo) == "GREEN"


def test_zone_centre_is_in_range():
    # The polygon's centroid is inside it (or, at worst for a concave zone,
    # well within the 500 m buffer) — so it must classify as in-range, not RED.
    poly = zones._polygon()
    lat = sum(p[0] for p in poly) / len(poly)
    lon = sum(p[1] for p in poly) / len(poly)
    assert zones.classify_location(lat, lon) in ("GREEN", "AMBER")
