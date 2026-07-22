"""map_listings: the lat/lon->SVG projection stays in-canvas and inverts latitude,
and build() writes a self-contained SVG page (listings mocked; real zone/gates)."""
import map_listings


def test_projector_in_canvas_and_inverts_latitude():
    pts = [(31.25, 34.79), (31.27, 34.81)]
    xy = map_listings._projector(pts)
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
                        lambda: ([(31.26, 34.80, "GREEN", 90, "רגר 1", 1400, 8)], 3))
    page = map_listings.build()
    assert out.exists()
    assert "<svg" in page and "http" not in page.split("xmlns")[0]   # no external CDN/tiles
    assert map_listings._TIER_COLOR["GREEN"] in page                 # dot colored by tier
    assert "1 placed, 3 unmapped" in page
