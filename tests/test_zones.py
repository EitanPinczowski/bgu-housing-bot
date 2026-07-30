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


def test_neighborhood_of_resolves_imported_polygons():
    # each imported ב/ג/ד polygon: a point inside it (its centroid) reports its letter
    polys = zones._neighborhood_polys()
    assert polys, "neighborhoods.json should be imported (run load_neighborhoods.py)"
    for letter, poly in polys:
        lat = sum(p[0] for p in poly) / len(poly)
        lon = sum(p[1] for p in poly) / len(poly)
        assert zones.neighborhood_of(lat, lon) == letter
    # far away / no coordinate -> None (never a droppable 'other' neighborhood)
    assert zones.neighborhood_of(32.0853, 34.7818) is None
    assert zones.neighborhood_of(None, None) is None


def test_in_allowed_neighborhood_and_fail_open(monkeypatch):
    polys = zones._neighborhood_polys()
    letter, poly = polys[0]
    la = sum(p[0] for p in poly) / len(poly)
    lo = sum(p[1] for p in poly) / len(poly)
    assert zones.in_allowed_neighborhood(la, lo) is True        # inside ב/ג/ד
    assert zones.in_allowed_neighborhood(32.0853, 34.7818) is False   # Tel Aviv
    # FAIL-OPEN: if no polygons are loaded, everything is allowed (never red-out)
    monkeypatch.setattr(zones, "_neighborhood_polys", lambda: [])
    assert zones.in_allowed_neighborhood(32.0853, 34.7818) is True


def test_classify_effective_reds_outside_bgd():
    import config
    # a point within walk of a gate but OUTSIDE ב/ג/ד: classify_location says AMBER,
    # the ב/ג/ד-only rule makes classify_effective RED.
    far = (32.0853, 34.7818)   # Tel Aviv — outside the polygons
    assert zones.classify_location(*far, walk_min=config.MAX_WALK_MINUTES - 1) == "AMBER"
    assert zones.classify_effective(*far, walk_min=config.MAX_WALK_MINUTES - 1) == "RED"


def test_zone_centre_is_in_range():
    # The polygon's centroid is inside it (or, at worst for a concave zone,
    # well within the 500 m buffer) — so it must classify as in-range, not RED.
    poly = zones._polygon()
    lat = sum(p[0] for p in poly) / len(poly)
    lon = sum(p[1] for p in poly) / len(poly)
    assert zones.classify_location(lat, lon) in ("GREEN", "AMBER")


# --- display-only neighborhoods must never affect classification -------------------
def test_display_neighborhoods_cannot_widen_the_allowed_set(tmp_path, monkeypatch):
    """map_neighborhoods.json exists so the dashboard can label שכונה א/ה/ו for
    orientation. zones.in_allowed_neighborhood returns True for ANY polygon in
    neighborhoods.json, so if those display areas ever leaked into that file, listings
    in rejected neighborhoods would silently start passing the ב/ג/ד gate. This is the
    guard for that."""
    import json
    import config
    import map_listings
    import zones

    # a display file that contains a rejected neighborhood
    box = [[31.30, 34.70], [31.30, 34.71], [31.31, 34.71], [31.31, 34.70]]
    disp = tmp_path / "map_neighborhoods.json"
    disp.write_text(json.dumps({"neighborhoods": [{"name": "שכונה ו",
                                                   "polygon_latlon": box}]},
                               ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(map_listings, "_MAP_NBHD_PATH", disp)

    # …and a classification file that holds only ב
    allowed = tmp_path / "neighborhoods.json"
    allowed.write_text(json.dumps({"neighborhoods": [
        {"letter": "ב", "name": "שכונה ב",
         "polygon_latlon": [[31.25, 34.79], [31.25, 34.80], [31.26, 34.80], [31.26, 34.79]]}]},
        ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "NEIGHBORHOODS_PATH", allowed)
    zones._neighborhood_polys.cache_clear()
    try:
        inside_vav = (31.305, 34.705)
        assert zones.in_allowed_neighborhood(*inside_vav) is False
        assert zones.neighborhood_of(*inside_vav) is None
        # the real ב polygon still works
        assert zones.in_allowed_neighborhood(31.255, 34.795) is True
        assert zones.neighborhood_of(31.255, 34.795) == "ב"
        # and the display layer DOES draw the area zones refuses to accept
        drawn = "".join(map_listings.display_neighborhoods_svg(
            lambda la, lo: ((lo - 34.6) * 1000, (31.4 - la) * 1000),
            (31.0, 31.5, 34.6, 34.9)))
        assert "שכונה ו" in drawn and 'class="nbhd"' in drawn
    finally:
        zones._neighborhood_polys.cache_clear()


def test_display_neighborhoods_absent_is_not_fatal(tmp_path, monkeypatch):
    import map_listings
    monkeypatch.setattr(map_listings, "_MAP_NBHD_PATH", tmp_path / "nope.json")
    assert map_listings.display_neighborhoods_svg(lambda a, b: (0, 0),
                                                  (31.0, 31.5, 34.6, 34.9)) == []
