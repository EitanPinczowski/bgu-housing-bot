"""map_listings: the lat/lon->SVG projection stays in-canvas and inverts latitude,
and build() writes a self-contained SVG page (listings mocked; real zone/gates)."""
import map_listings


def test_projector_in_canvas_and_inverts_latitude():
    pts = [(31.25, 34.79), (31.27, 34.81)]
    xy, _params = map_listings._projector(pts)
    for la, lo in pts:
        x, y = xy(la, lo)
        assert map_listings._PAD - 1 <= x <= map_listings._W - map_listings._PAD + 1
        assert map_listings._PAD - 1 <= y <= map_listings._H - map_listings._PAD + 1
    # a more-northern point (higher lat) maps to a smaller y (SVG y grows downward)
    assert xy(31.27, 34.80)[1] < xy(31.25, 34.80)[1]


def test_build_writes_self_contained_svg(monkeypatch, tmp_path):
    out = tmp_path / "map.html"
    monkeypatch.setattr(map_listings, "OUT", out)
    monkeypatch.setattr(map_listings, "_load_listings",
                        lambda: ([(31.26, 34.80, "GREEN", 90, "רגר 1", 1400, 8, "k1")], 3))
    page = map_listings.build()
    assert out.exists()
    assert "<svg" in page and "http" not in page.split("xmlns")[0]   # no external CDN/tiles
    assert map_listings._TIER_COLOR["GREEN"] in page                 # dot colored by tier
    assert "1 placed, 3 unmapped" in page


def test_projection_params_reproduce_the_projector_exactly():
    """The dashboard draws the backdrop server-side but the dots in the browser, from
    these params. If they don't reproduce xy() the dots drift off the map — and because
    build_svg() also goes through xy_from, the standalone map would break first."""
    pts = [(31.24, 34.78), (31.28, 34.82)]
    xy, params = map_listings._projector(pts)
    same = map_listings.xy_from(params)
    for la, lo in [(31.24, 34.78), (31.26, 34.80), (31.28, 34.82), (31.255, 34.795)]:
        a, b = xy(la, lo)
        c, d = same(la, lo)
        assert abs(a - c) < 1e-9 and abs(b - d) < 1e-9
    assert set(params) == {"min_lon", "max_lat", "kx", "scale", "pad", "w", "h"}


def test_base_svg_has_the_backdrop_but_no_dots(monkeypatch):
    monkeypatch.setattr(map_listings, "_amenity_pins", lambda: [])
    base, projection = map_listings.build_base_svg(
        [(31.26, 34.80, "GREEN", 90, "רגר 1", 1400, 8, "k1")])
    assert "<polygon" in base                       # the green zone
    assert "★" in base                              # the gates
    assert 'class="dot"' not in base                # dots are the browser's job
    assert "רגר 1" not in base                      # nor any listing's identity
    assert not base.endswith("</svg>")              # left open for callers to extend
    assert projection["scale"] > 0


def test_amenity_pins_skip_a_target_that_matches_half_the_city(tmp_path, monkeypatch):
    """'a stop with a bus to the train station' legitimately matches 428 stops; pinning
    them all would bury the map while saying nothing."""
    import json
    import config
    data = {"targets": {
        "gym": {"icon": "🏋️", "points": [{"name": "קניון", "lat": 31.24, "lon": 34.79}]},
        "bus669": {"icon": "🚌", "stops": [{"name": "רגר", "lat": 31.27, "lon": 34.80}]},
        "train": {"icon": "🚆", "stops": [{"name": f"s{i}", "lat": 31.25, "lon": 34.80}
                                          for i in range(map_listings._MAX_PINS_PER_TARGET + 1)]},
    }}
    p = tmp_path / "amenities.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "AMENITIES_PATH", p)
    icons = {icon for _la, _lo, icon, _n in map_listings._amenity_pins()}
    assert icons == {"🏋️", "🚌"}                    # the 428-stop target is skipped


def test_missing_amenities_file_is_not_fatal(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "AMENITIES_PATH", tmp_path / "nope.json")
    assert map_listings._amenity_pins() == []


# --- the street layer (shared with area_map) --------------------------------------
_FEATS = {"landmarks": [{"kind": "university", "name": "BGU",
                         "polygon_latlon": [[31.262, 34.800], [31.263, 34.802],
                                            [31.261, 34.802]]}],
          "streets": [
              {"name": "יצחק רגר", "main": True,
               "segments": [[[31.250, 34.798], [31.270, 34.799]]]},      # long, in view
              {"name": "סמטה קטנה", "main": False,
               "segments": [[[31.2600, 34.7990], [31.2601, 34.7991]]]},  # tiny, in view
              {"name": "רחוב רחוק", "main": True,
               "segments": [[[32.900, 35.900], [32.901, 35.901]]]},      # far outside
          ]}
_BOUNDS = (31.24, 31.28, 34.77, 34.82)


def _xy(la, lo):
    return ((lo - 34.77) * 20000, (31.28 - la) * 20000)


def test_streets_are_culled_to_the_viewport():
    """Only ~8,900 of the 30,300 street points fall inside the listings map's view;
    drawing the rest would trade ~400 KB for pixels nobody can see."""
    paths, labels = map_listings.streets_svg(_xy, _BOUNDS, _FEATS)
    artery = next(p for p in paths if 'class="st st-art"' in p)
    # two arteries exist but one is 180 km away, so the combined path has ONE subpath
    assert artery.count("M") == 1, artery
    # and the far one contributes neither geometry nor a label
    assert "רחוב רחוק" not in [n for _pos, n, _main, _mz in labels]
    assert "יצחק רגר" in [n for _pos, n, _main, _mz in labels]


def test_arteries_and_minor_streets_are_drawn_differently():
    paths, _ = map_listings.streets_svg(_xy, _BOUNDS, _FEATS)
    d = "".join(paths)
    assert 'class="st st-cas st-cas-art"' in d and 'class="st st-art"' in d
    assert 'class="st st-cas st-cas-min"' in d and 'class="st st-min"' in d
    # the widths live in CSS, once, rather than on every path
    assert ".st-art{" in map_listings.STREET_CSS and ".st-min{" in map_listings.STREET_CSS
    assert "non-scaling-stroke" in map_listings.STREET_CSS


def test_geometry_is_four_paths_not_thousands_of_polylines():
    """620 KB and 2,804 DOM nodes before this; the node count is what keeps zoom smooth."""
    paths, _ = map_listings.streets_svg(_xy, _BOUNDS, _FEATS)
    assert len(paths) == 4                        # casing+line for arteries and minors
    assert all(p.startswith("<path") for p in paths)


def test_only_long_segments_get_a_name():
    """1,174 streets would be a wall of text; a name needs a long enough run in view."""
    _paths, labels = map_listings.streets_svg(_xy, _BOUNDS, _FEATS)
    names = [n for _pos, n, _main, _mz in labels]
    assert "יצחק רגר" in names                    # long artery
    assert "סמטה קטנה" not in names               # a few metres of side street


def test_labels_are_revealed_by_zoom_not_all_at_once():
    """250 names at full extent is a wall of text. Each label carries the zoom at
    which its segment is long enough to read; the dashboard's applyView() hides it
    below that, so arteries show at 1x and side streets only once you're in among
    them."""
    _paths, labels = map_listings.streets_svg(_xy, _BOUNDS, _FEATS)
    mz = {n: m for _pos, n, _main, m in labels}
    assert mz["יצחק רגר"] == 1                       # a long artery: legible at 1x
    short = [{"name": "רחוב בינוני", "main": False,
              "segments": [[[31.2600, 34.7990], [31.2620, 34.7990]]]}]
    _p, lb = map_listings.streets_svg(_xy, _BOUNDS,
                                      {"landmarks": [], "streets": short})
    assert lb and lb[0][3] > 1                       # shorter run: needs zooming in
    out = "".join(map_listings.street_labels_svg(lb))
    assert 'data-minzoom="' in out and 'class="slabel st-l"' in out


def test_prominence_is_the_polyline_not_the_distance_between_the_ends():
    """`חיבת ציון` measured 13.8 px end-to-end against 84.5 px of real geometry, so it
    was never named at any zoom. A street that doubles back covers ground even when it
    finishes near where it started."""
    zig = [[31.2600, 34.7900], [31.2600, 34.7910], [31.2602, 34.7910],
           [31.2602, 34.7900], [31.2604, 34.7900], [31.2604, 34.7910],
           [31.2606, 34.7910], [31.2606, 34.7900], [31.2608, 34.7900]]
    straight = [[31.2600, 34.7900], [31.2608, 34.7900]]     # 96 px of line vs 16, both
    #                                                         ending 16 px from the start
    _p, labels = map_listings.streets_svg(_xy, _BOUNDS, {"landmarks": [], "streets": [
        {"name": "מפותל", "main": False, "segments": [zig]},
        {"name": "ישר", "main": False, "segments": [straight]}]})
    mz = {n: m for _pos, n, _main, m in labels}
    assert mz["מפותל"] == 1.0        # 92 px of street: legible at 1x
    assert mz["ישר"] > 5             # genuinely 12 px: only once you're in among it


def test_a_roundabout_is_named_from_its_segments_together():
    """`כיכר האבות` is six pieces, none longer than 4.9 px but ~20 px in total, and it
    is the densest listing cluster on the map. Judging a street by its longest single
    piece left it anonymous."""
    ring = [[[31.2600 + i * 3e-4, 34.7900], [31.2602 + i * 3e-4, 34.7900]]
            for i in range(6)]                               # 6 x 4 px = 24 px total
    _p, labels = map_listings.streets_svg(_xy, _BOUNDS, {"landmarks": [], "streets": [
        {"name": "כיכר", "main": False, "segments": ring},
        {"name": "בודד", "main": False, "segments": [ring[0]]}]})
    names = [n for _pos, n, _main, _mz in labels]
    assert "כיכר" in names           # 24 px of street, in six pieces
    assert "בודד" not in names       # one 4 px piece is genuinely too little


def test_no_label_asks_for_more_zoom_than_the_map_has():
    """dashboard.applyView clamps at 12x, so a minzoom above that is the same as no
    label at all. This binds the label rule to that ceiling: change the floor or the
    target and this fails rather than silently emitting labels nobody can reach."""
    assert map_listings._MAX_LABEL_ZOOM <= 12
    # …and the constants must not be able to produce one on their own either
    worst = map_listings._LABEL_TARGET_PX / map_listings._LABEL_MIN_PX
    assert worst <= map_listings._MAX_LABEL_ZOOM
    tiny = [{"name": f"רחוב {i}", "main": False,
             "segments": [[[31.2600 + i * 1e-4, 34.7900],
                           [31.2607 + i * 1e-4, 34.7900]]]} for i in range(40)]
    _p, labels = map_listings.streets_svg(_xy, _BOUNDS,
                                          {"landmarks": [], "streets": tiny})
    assert labels and all(mz <= map_listings._MAX_LABEL_ZOOM
                          for _pos, _n, _main, mz in labels)


def test_a_street_in_the_zone_outranks_a_longer_one_outside_it(monkeypatch):
    """The cap used to be spent on long roads out in the desert that no flat is on.
    The green/amber area is the part of the map anyone reads, so it is named first and
    length only breaks ties."""
    monkeypatch.setattr(map_listings, "_LABEL_CAP", 2)
    monkeypatch.setattr(map_listings, "_in_zone", lambda seg: seg[0][1] < 34.795)
    streets = [{"name": "מדברי ארוך", "main": True,          # far the longest, out of zone
                "segments": [[[31.2500, 34.8000], [31.2700, 34.8000]]]},
               {"name": "מדברי שני", "main": True,
                "segments": [[[31.2500, 34.8100], [31.2690, 34.8100]]]},
               {"name": "בתוך האזור", "main": False,          # short, but where the flats are
                "segments": [[[31.2600, 34.7900], [31.2610, 34.7900]]]}]
    _p, labels = map_listings.streets_svg(_xy, _BOUNDS,
                                          {"landmarks": [], "streets": streets})
    names = [n for _pos, n, _main, _mz in labels]
    assert names[0] == "בתוך האזור"          # in-zone first, whatever its length
    assert "מדברי שני" not in names          # the cap falls on the out-of-zone tail


def test_label_count_is_capped():
    # the step has to keep every one of them INSIDE _BOUNDS, or culling caps the list
    # before the cap does and the test passes for the wrong reason
    many = [{"name": f"רחוב {i}", "main": False,
             "segments": [[[31.250 + i * 5e-5, 34.790], [31.250 + i * 5e-5, 34.800]]]}
            for i in range(map_listings._LABEL_CAP + 60)]
    _p, labels = map_listings.streets_svg(_xy, _BOUNDS,
                                          {"landmarks": [], "streets": many})
    assert len(labels) == map_listings._LABEL_CAP


def test_walk_rings_match_the_20_minute_rule_they_illustrate():
    """The rings exist to make MAX_WALK_MINUTES visible, so their radii have to be
    the same arithmetic zones.est_walk_to_gate_min uses — otherwise the picture
    contradicts the classifier."""
    import re
    import config
    import geocode
    xy, params = map_listings._projector([(31.24, 34.77), (31.29, 34.83)])
    out = "".join(map_listings.walk_rings_svg(xy))
    rings = re.findall(r'class="ring(?: ring-max)?" cx="([\d.]+)" cy="([\d.]+)" '
                       r'r="([\d.]+)"', out)
    assert len(rings) == 4 * len(config.GATES)
    assert out.count("ring-max") == len(config.GATES)          # the boundary ring
    assert 'id="rings"' in out and "display:none" in out       # off until switched on

    gate = next(iter(config.GATES.values()))
    cx, _cy = xy(gate["lat"], gate["lon"])
    px_per_deg = abs(xy(gate["lat"], gate["lon"] + 0.01)[0] - cx) / 0.01
    mine = sorted(float(r) for gx, _gy, r in rings if abs(float(gx) - cx) < 0.5)
    for want, r in zip((5, 10, 15, config.MAX_WALK_MINUTES), mine):
        metres = geocode._haversine_m(gate["lat"], gate["lon"],
                                      gate["lat"], gate["lon"] + r / px_per_deg)
        got = metres * config.WALK_DETOUR_FACTOR / config.WALK_SPEED_M_PER_MIN
        assert abs(got - want) < 0.2, f"{want}-min ring reads as {got:.1f} min"


def test_each_layer_has_its_own_switch_class():
    """The legend's checkboxes toggle one class on the <svg>; every hideable thing
    needs a marker class of its own. A :not() chain caught the campus labels once."""
    css = map_listings.STREET_CSS
    for rule in (".no-streets .st-l", ".no-nbhd .nbhd-abc", ".no-amen .amen"):
        assert rule in css, rule
    lm = "".join(map_listings.landmarks_svg(_xy, _FEATS))
    assert 'class="slabel"' in lm and "st-l" not in lm    # campus label is not a street


def test_landmarks_render_with_their_labels():
    out = "".join(map_listings.landmarks_svg(_xy, _FEATS))
    assert "<polygon" in out and "#3949ab" in out          # BGU blue
    assert "אוניברסיטת בן גוריון" in out


def test_base_svg_now_includes_streets_and_landmarks(monkeypatch):
    monkeypatch.setattr(map_listings, "features", lambda: _FEATS)
    monkeypatch.setattr(map_listings, "_amenity_pins", lambda: [])
    base, _proj = map_listings.build_base_svg(
        [(31.26, 34.80, "GREEN", 90, "רגר 1", 1400, 8, "k1")])
    assert 'class="st ' in base                            # street paths
    assert "#3949ab" in base                               # the campus footprint
    assert base.index('class="st ') < base.index("<polygon points")  # streets underneath


def test_missing_features_file_degrades_quietly(tmp_path, monkeypatch):
    monkeypatch.setattr(map_listings, "_FEATURES_PATH", tmp_path / "nope.json")
    assert map_listings.features() == {"landmarks": [], "streets": []}
    assert map_listings.streets_svg(_xy, _BOUNDS) == ([], [])
    assert map_listings.landmarks_svg(_xy) == []


def test_the_surveyed_landmarks_are_drawn():
    """They steered geocoding while being invisible: a dot inside `הבלוק` looked placed
    from nowhere. Drawn at their MEASURED extent, so the shape shows how precisely a
    listing there is known."""
    import geocode
    svg = "".join(map_listings.landmarks_svg(_xy))
    for name in geocode.landmarks():
        assert name in svg, name
    assert svg.count('class="lmk"') == len(geocode.landmarks())


def test_the_landmark_layer_can_be_switched_off():
    """CLAUDE.md's rule after the gates shipped as the only untoggleable thing on the
    map: every layer needs its OWN marker class."""
    css = map_listings.STREET_CSS
    assert ".no-lmk .lmk" in css and ".lmk-l" in css
