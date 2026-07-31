"""dashboard.py — the offline browse-by-hand page.

The contract is that it works forever with no network: everything inline, no CDN,
no tile server. These tests pin that, plus the details that are easy to break
silently (HTML escaping, the sort keys, and read-only access to the DB).
"""
import datetime as dt
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


def test_payload_carries_how_much_to_trust_the_dot(temp_db, monkeypatch, tmp_path):
    """138 of 338 listings resolve only to a street centroid, which is why so many
    stack on one point. The page can't say so unless the confidence travels with the
    row — geocode_source was in the table but never in the query."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    row = _boot(dashboard.build())[0]
    assert "geocode_source" in row and "geo_confidence" in row
    assert row["geo_confidence"] in ("exact", "high", "street", "area", "none")


def test_approximate_dots_are_drawn_hollow_and_filterable(temp_db, monkeypatch,
                                                          tmp_path):
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "function isApprox(r)" in page
    assert "'dot' + (approx ? ' approx' : '')" in page
    assert 'id="approx"' in page                       # the needs-a-location filter
    assert "$('approx').checked && !isApprox(r)" in page
    assert "עיגול חלול" in page                        # explained in the legend


def test_a_stack_fans_out_because_zoom_can_never_split_it(temp_db, monkeypatch,
                                                          tmp_path):
    """Dots sharing one coordinate are the "clusters don't open up" report. Zooming a
    stack does nothing by definition, so a stack fans instead — and the code has to
    tell the two cases apart to pick the right one."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "b.sameSpot = b.rows.every(" in page         # the discriminator
    assert "if (grp.__sameSpot) setSpider(" in page     # stack -> fan
    assert "else fitTo(grp.__rows);" in page            # spread -> zoom
    assert "function drawSpider(" in page and "spider-leg" in page
    assert "באותה נקודה בדיוק" in page                 # the badge says which it is


def test_touch_action_is_set_on_the_svg_not_only_the_container(temp_db, monkeypatch,
                                                               tmp_path):
    """touch-action does NOT inherit, and the pointer handlers live on the <svg>.
    With `pan-y` on .map only, the svg computed to `auto`: the browser scrolled the
    page while our handler panned the map, so one finger did both — the "glitchy on
    phone" report."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert ".map,.map svg{touch-action:none}" in page
    assert "touch-action:pan-y" not in page          # the bug, gone
    # finger-sized targets wherever the pointer is coarse
    assert "@media (pointer:coarse){" in page
    assert "min-height:40px" in page


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


# --- the shared snapshot -----------------------------------------------------------
def test_snapshot_removes_write_controls_and_dates_itself(temp_db, monkeypatch, tmp_path):
    """A file someone was SENT has no server behind it. Buttons that could only alert
    'unavailable' are removed, not disabled — and the banner is what stops a
    three-day-old copy being read as current."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _save("k1", "רגר 5")
    path = dashboard.build_share()
    page = path.read_text(encoding="utf-8")

    assert path.name == f"dashboard-{dt.date.today().isoformat()}.html"
    assert "window.__SNAPSHOT__ = true;" in page
    assert "צילום מצב" in page and dt.date.today().strftime("%d/%m/%Y") in page
    # the flag is what every write control is gated on
    assert "const SNAP = !!window.__SNAPSHOT__;" in page
    # Correcting a location writes to the DB, so it is gated on __LIVE__ (which is
    # false here) rather than on SNAP — the template string still ships, but the
    # button is never rendered. Assert the gate, not the absence of the source.
    assert "const place = window.__LIVE__" in page
    assert "window.__LIVE__ = false;" in page
    for gated in ("(SNAP ? '' : '<button data-act=\"save\">⭐</button>'",
                  "? (r.note ? '<div class=\"raw\">📝 '",     # note read-only
                  "if (SNAP) return;"):                       # no route planner
        assert gated in page, gated
    # …and what it must NOT remove: the contacts, per the user's decision
    assert "google.com/maps/dir" in page and "'wa'" not in page.split("_HEAD")[0]


def test_snapshot_makes_no_external_requests(temp_db, monkeypatch, tmp_path):
    """Self-contained is the whole point: it has to work on a phone with the PC off,
    and it must not phone home from someone else's device."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _save("k1", "רגר 5")
    page = dashboard.build_share().read_text(encoding="utf-8")
    for tag in ("<script src=", "<link rel=\"stylesheet\"", "@import", "tile.openstreetmap"):
        assert tag not in page, tag
    # the /img proxy needs the server, so a snapshot omits the tag instead of
    # shipping a request that can only fail
    assert "/img/" in page                   # the code path still exists for live mode
    assert "(!SNAP && r.photos" in page


def test_normal_build_keeps_the_write_controls(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "window.__SNAPSHOT__ = false;" in page
    assert "צילום מצב" not in page


def test_a_sent_snapshot_points_at_the_live_url(temp_db, monkeypatch, tmp_path):
    """A downloaded file has no way to tell you a fresher copy exists — which is
    exactly how the live URL went unnoticed after it was set up."""
    import publish
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(publish, "pages_url",
                        lambda: "https://example.github.io/dash/")
    _save("k1", "רגר 5")
    page = dashboard.build_share().read_text(encoding="utf-8")
    assert "https://example.github.io/dash/" in page
    assert "לגרסה המתעדכנת" in page


def test_an_unconfigured_publisher_leaves_no_broken_link(temp_db, monkeypatch, tmp_path):
    import publish
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(publish, "pages_url", lambda: "")
    _save("k1", "רגר 5")
    page = dashboard.build_share().read_text(encoding="utf-8")
    assert "צילום מצב" in page and "לגרסה המתעדכנת" not in page
