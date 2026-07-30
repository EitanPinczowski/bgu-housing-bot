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
