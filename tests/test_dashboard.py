"""dashboard.py — the offline browse-by-hand page.

The contract is that it works forever with no network: everything inline, no CDN,
no tile server. These tests pin that, plus the details that are easy to break
silently (HTML escaping, the sort keys, and read-only access to the DB).
"""
import re

import pytest

import config
import dashboard
import storage
from models import ListingExtract, PipelineResult, Status


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The map geocodes every address; left live, this file spent 98 s hitting
    Overpass. The dashboard's own logic is what's under test here."""
    monkeypatch.setattr(dashboard.map_listings, "build_svg",
                        lambda: ("<svg><!--map--></svg>", [], 0))


def _save(key, addr, price=1500, rooms=2, score=80, status=Status.MATCH, walk=8.0, **kw):
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=addr,
                       price_per_room_ils=price, available_rooms_count=rooms, **kw)
    storage.save_listing(PipelineResult(status=status, dedup_key=key, score=score,
                                        location_tier="GREEN", walk_minutes=walk, extract=e))


def test_page_is_self_contained(temp_db, monkeypatch, tmp_path):
    """No CDN, no external stylesheet, no remote script — the whole point of the
    inline-everything approach used by the other generators here."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "<script>" in page and "<style>" in page
    # nothing may be fetched from anywhere
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', page)
    assert "cdn" not in page.lower()


def test_rows_and_filters_are_present(temp_db, monkeypatch, tmp_path):
    """Rows are rendered in the BROWSER now, from window.__BOOT__, so the assertion is
    on the payload rather than on server-built <tr>s."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5", price=1400)
    _save("k2", "קדש 3", price=1900, status=Status.NEEDS_DATA)
    page = dashboard.build()
    assert "רגר 5" in page and "קדש 3" in page
    for control in ('id="q"', 'id="st"', 'id="tier"', 'id="maxp"', 'id="minr"',
                    'id="stale"', 'id="nobroker"', 'id="saved"', 'id="onlynew"',
                    'id="bycolor"'):
        assert control in page
    boot = _boot(page)
    assert {r["dedup_key"] for r in boot} == {"k1", "k2"}
    assert {r["price_per_room"] for r in boot} == {1400, 1900}
    # each row carries what the filters and the map dots need
    for r in boot:
        assert set(r) >= {"status", "location_tier", "eff_score", "lat", "lon",
                          "photos", "breakdown", "post_text", "note", "stale", "broker"}


def test_untrusted_text_cannot_break_out_of_the_script_block(temp_db, monkeypatch, tmp_path):
    """Addresses and post bodies come straight from Facebook. They now travel inside a
    <script> block as JSON, and json.dumps does NOT escape '<' — so "</script>" in an
    address would close the tag and everything after it would parse as HTML."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "</script><img src=x onerror=alert(1)> רגר")
    page = dashboard.build()
    assert "</script><img" not in page
    assert "\u003c/script\u003e" in page or "\u003c" in page
    # exactly two script tags: the boot data and the app
    assert page.count("<script>") == 2 and page.count("</script>") == 2
    # and it survives a JSON round-trip unchanged
    assert _boot(page)[0]["address"].startswith("</script>")


def test_sorting_and_unknown_values_are_handled_in_the_payload(temp_db, monkeypatch, tmp_path):
    """Sorting moved to the browser; nulls stay null in the payload and the JS sinks
    them (see visible()). The server no longer emits sentinels."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5", price=None, walk=None)
    _save("k2", "קדש 3", price=1400, walk=6.0)
    boot = {r["dedup_key"]: r for r in _boot(dashboard.build())}
    assert boot["k1"]["price_per_room"] is None and boot["k1"]["walk_minutes"] is None
    assert boot["k2"]["price_per_room"] == 1400 and boot["k2"]["walk_minutes"] == 6.0


def test_broker_and_vote_flags_show(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    storage.set_mark("k1", "u1", "saved")
    monkeypatch.setattr(storage, "phone_listing_count",
                        lambda p: config.BROKER_MIN_LISTINGS + 1)
    row = _boot(dashboard.build())[0]
    assert row["saved"] is True
    assert row["broker"] == config.BROKER_MIN_LISTINGS + 1


def test_live_page_fetches_instead_of_inlining(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    live = dashboard.render_live()
    assert "/api/listings.json" in live and "window.__LIVE__ = true" in live
    assert _boot(live) == []                       # nothing inlined; it fetches
    static = dashboard.render(live=False)
    assert "window.__LIVE__ = false" in static and len(_boot(static)) == 1


def test_empty_db_still_builds(temp_db, monkeypatch, tmp_path):
    """A fresh install has no listings; the page must render rather than crash."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    page = dashboard.build()
    assert "<table" in page and "0 רשומות" in page


def test_build_does_not_modify_the_database(temp_db, monkeypatch, tmp_path):
    """It's a viewer. Reading must never write."""
    import sqlite3
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    before = sqlite3.connect(temp_db).execute(
        "SELECT dedup_key, status, score, address FROM listings ORDER BY dedup_key").fetchall()
    dashboard.build()
    after = sqlite3.connect(temp_db).execute(
        "SELECT dedup_key, status, score, address FROM listings ORDER BY dedup_key").fetchall()
    assert before == after


def _boot(page):
    """The JSON the page hands the browser (window.__BOOT__).

    The backslash-u003c escapes are ordinary JSON, so json.loads decodes them
    itself. (An earlier version ran unicode_escape here, which would have mangled
    the Hebrew — it only passed because no test asserted on Hebrew content.)"""
    import json
    marker = "window.__BOOT__ = "
    start = page.index(marker) + len(marker)
    end = page.index(chr(59) + chr(10), start)
    return json.loads(page[start:end])


def test_hebrew_survives_the_payload_round_trip(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "שדרות יצחק רגר 164, שכונה ג׳")
    assert _boot(dashboard.build())[0]["address"] == "שדרות יצחק רגר 164, שכונה ג׳"


# --- map-first layout -------------------------------------------------------------
def test_the_map_comes_before_the_list(temp_db, monkeypatch, tmp_path):
    """Where a flat is, is the first question — the map leads and the table follows."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert page.index('class="map"') < page.index('details class="list"')


def test_the_list_starts_collapsed(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    details = page[page.index('<details class="list"'):]
    opening_tag = details[:details.index(">") + 1]
    assert " open" not in opening_tag          # closed until asked for
    assert "<summary" in details and page.index("<table id=") > page.index("<details")


def test_street_styling_is_in_the_page(temp_db, monkeypatch, tmp_path):
    """Without STREET_CSS the paths default to fill:black and the map renders as
    filled blobs instead of roads."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert ".st{" in page and "fill:none" in page
    assert ".st-art{" in page and ".st-min{" in page


def test_map_furniture_is_present(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    for bit in ('id="card"', 'id="reset"', 'class="maphint"', 'id="listsum"'):
        assert bit in page, bit
    # the dot click must not navigate away — directions live in the card
    assert "google.com/maps/dir" in page          # present, but inside cardHtml
    assert "ctrlKey" in page                      # plain wheel scrolls the page


def test_map_interrogation_controls_are_wired(temp_db, monkeypatch, tmp_path):
    """Fit / box-select / cluster behaviour is interaction logic, verified in a real
    browser. What a unit test can hold is that the controls exist and are connected —
    a renamed id silently dropping a listener is the failure that would slip through."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    for bit in ('id="fitbtn"', 'id="boxbtn"', 'id="boxchip"', 'id="boxclear"'):
        assert bit in page, bit
    for handler in ("$('fitbtn').addEventListener", "$('boxbtn').addEventListener",
                    "$('boxclear').addEventListener"):
        assert handler in page, handler
    assert "if (!inBox(r)) return false;" in page      # the box is a real filter
    assert "grp.__rows = b.rows" in page               # not a comma-joined key list
    # colour is not a filter, so switching it must not move the map
    assert "$('bycolor').addEventListener('change', render);" in page


def test_legend_explains_the_map_without_javascript(temp_db, monkeypatch, tmp_path):
    """A partner opens the shared file cold; the key to the symbols has to be in the
    markup, not assembled by a script that a downloaded file may never run."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    legend = page.split('<details id="legend"')[1].split("</details>")[0]
    assert "GREEN" in legend and "AMBER" in legend and "RED" in legend
    assert str(config.MAX_WALK_MINUTES) in legend          # the rule behind AMBER
    assert "ב/ג/ד" in legend                               # which outlines matter
    for layer in ("streets", "nbhd", "amen", "rings"):
        assert f'data-layer="{layer}"' in legend, layer
    # rings are the only switch off by default — they clutter until you want them
    off = [line for line in legend.splitlines()
           if "data-layer=" in line and "checked" not in line]
    assert len(off) == 1 and 'data-layer="rings"' in off[0], off
    assert "initLayers();" in page
