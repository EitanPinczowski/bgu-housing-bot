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
    assert "<circle" not in base                    # dots are the browser's job
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
    assert "רחוב רחוק" not in [n for _pos, n, _m in labels]
    assert "יצחק רגר" in [n for _pos, n, _m in labels]


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
    names = [n for _pos, n, _m in labels]
    assert "יצחק רגר" in names                    # long artery
    assert "סמטה קטנה" not in names               # a few metres of side street


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
