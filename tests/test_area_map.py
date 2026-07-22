"""area_map: the projection stays in-canvas, and build() writes a self-contained
SVG with the real tier field (GREEN/AMBER over a RED base)."""
import area_map


def test_projector_in_canvas():
    xy, bounds = area_map._projector([(31.25, 34.79), (31.27, 34.81)])
    x, y = xy(31.26, 34.80)
    assert 0 <= x <= area_map._W and 0 <= y <= area_map._H


def test_build_writes_tier_field(monkeypatch, tmp_path):
    out = tmp_path / "area.html"
    monkeypatch.setattr(area_map, "OUT", out)
    # keep the grid small so the test is quick
    monkeypatch.setattr(area_map, "_NX", 30)
    monkeypatch.setattr(area_map, "_NY", 25)
    page = area_map.build()
    assert out.exists()
    assert "<svg" in page and "http" not in page.split("xmlns")[0]   # self-contained
    assert area_map._TIER_FILL["RED"] in page      # the out-of-range base wash
    assert area_map._TIER_FILL["GREEN"] in page    # in-range cells over it
