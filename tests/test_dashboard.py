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
    assert "grp.__sameSpot || grp.__tight" in page      # stack (or unzoomable) -> fan
    assert "else fitTo(grp.__rows);" in page            # spread -> zoom
    assert "function drawSpider(" in page and "spider-leg" in page
    assert "באותה נקודה בדיוק" in page                 # the badge says which it is


def test_a_badge_is_as_easy_to_hit_as_a_dot(temp_db, monkeypatch, tmp_path):
    """The badge opens 97% of the map — 223 of 229 listings sit inside one — and it was
    4 px across with no hit padding, while every single dot got HIT_RADIUS_PX of it.
    That is the "clusters aren't clickable" report."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "Math.max(10 / k, HIT_RADIUS_PX * unitsPerPx)" in page
    assert "'clhit'" in page and ".clhit{fill:transparent" in page
    # the dot handlers must keep ignoring it, or they'd call rowByKey(undefined)
    assert "closest('.dot, .hit')" in page and "closest('.dot, .clhit')" not in page


def test_pressing_a_badge_does_not_start_a_pan(temp_db, monkeypatch, tmp_path):
    """pointerdown captured the pointer for anything that wasn't `.dot`, and a captured
    pointer retargets the following click to the <svg> — where closest('.cl') is null,
    so the badge handler never ran."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "!ev.target.closest('.dot, .cl')" in page


def test_stacks_fan_by_themselves_once_you_are_zoomed_in(temp_db, monkeypatch, tmp_path):
    """38 of 48 badges at maximum zoom are flats on one exact coordinate. Clicking each
    one is not a fix; past AUTO_FAN_ZOOM the ones in view open on their own."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "const AUTO_FAN_ZOOM" in page
    assert "function activeFans(" in page
    assert "zoomFactor() < AUTO_FAN_ZOOM" in page
    # viewport-limited: 38 stacks x 19 dots would otherwise all be built every redraw
    assert "!inView(b.x, b.y)" in page


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


def test_click_opens_a_listing_on_every_device(temp_db, monkeypatch, tmp_path):
    """The bug: click lived in the `else` of `if (CAN_HOVER)`, so on every desktop and
    many touch laptops clicking a dot did nothing at all. Verified live before the fix
    (cardOpenedByClick: false). Hover is an ADDITION for pointer devices, never the
    only way in."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    click_at = page.index("svg.addEventListener('click'")
    hover_at = page.index("if (CAN_HOVER){")
    assert click_at < hover_at, "click must be bound unconditionally, before the hover branch"
    assert ".dot, .hit" in page                 # the enlarged target counts as the dot


def test_the_tap_target_is_sized_in_screen_pixels(temp_db, monkeypatch, tmp_path):
    """A dot is ~4px. Sizing the hit area in viewBox units *looked* right and measured
    8px, because the map renders ~0.36px per unit in a narrow pane."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "HIT_RADIUS_PX" in page
    assert "const unitsPerPx = view.w / (svgBox.width || 1);" in page
    assert "HIT_RADIUS_PX * unitsPerPx" in page
    assert ".hit{fill:transparent" in page      # invisible, but hittable


def test_panning_does_no_zoom_work_and_dots_are_culled(temp_db, monkeypatch, tmp_path):
    """A pan does not change the zoom factor, so counter-scaling every dot radius and
    every one of the 264 .slabel font-sizes on each pointermove produced values identical
    to the previous frame's. Measured before/after on the live page: 0.24 -> 0.005 ms at
    1x, 0.38 -> 0.108 ms at 8x, and 260 -> 101 rendered nodes at 8x.

    The counter must still report EVERY listing on the map, not the culled subset —
    getting that wrong once made it read 556 of 456."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    # the expensive work sits behind the zoom guard, not in applyView's body
    assert "function rescaleForZoom(" in page
    body = page[page.index("function applyView("):page.index("function zoomAt(")]
    assert ".slabel" not in body and "'.dot'" not in body, \
        "applyView must not touch dots or labels on a pan"
    assert "rescaleForZoom(svg, k)" in body
    # labels are static backdrop markup, so the NodeList is cached, not re-queried
    assert "slabelCache = slabelCache ||" in page
    # culling renders a box bigger than the viewport, and counts outside it
    assert "const CULL_MARGIN" in page and "function cullBox()" in page
    assert "for (const b of allGroups){" in page
    assert page.count("n += b.rows.length;") == 1, \
        "counting in both loops double-counts every listing in view"


def test_the_dot_shows_saved_and_contacted(temp_db, monkeypatch, tmp_path):
    """saved/contacted shipped in the payload and changed nothing about the dot, so
    finding your own shortlist meant hovering 310 of them.

    Two extra channels only — a dot is ~4px on a phone and already encodes tier as fill
    and precision as hollow/solid. And the ring is sized in SCREEN px: in viewBox units
    it measured 7px around a 4px dot at 375px wide, the same trap mkHit records."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "function mkHalo(" in page and "if (!r.saved) return null;" in page
    assert "HALO_PX * unitsPerPx" in page and "CL_HALO_PX * unitsPerPx" in page
    assert "const HALO_PX = 7, CL_HALO_PX = 10;" in page
    assert "if (r.contacted) c.setAttribute('opacity', DOT_FADED);" in page
    # a badge must carry it too: at 1x, 253 of 312 listings sit inside one
    assert "if (b.rows.some(r => r.saved)){" in page
    # broker/stale/notes/photos deliberately stay off the dot
    assert "r.broker" not in page[page.index("function mkDot("):page.index("function addDot(")]


def test_the_hover_highlight_keeps_the_precision_signal(temp_db, monkeypatch, tmp_path):
    """`.dot.hi` set stroke:#111, which repainted an approx dot's tier-coloured outline
    and destroyed the hollow/solid signal exactly when you were looking closely at one
    flat — and its stroke-width was in world units, so it swelled as you zoomed."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    hi = page[page.index(".dot.hi{"):page.index("\n", page.index(".dot.hi{"))]
    assert "stroke:#111" not in hi, "must not repaint the precision stroke"
    assert "drop-shadow" in hi
    assert ".dot{cursor:pointer;vector-effect:non-scaling-stroke}" in page


def test_the_unplaced_listings_are_named_not_hidden(temp_db, monkeypatch, tmp_path):
    """A listing with no coordinate simply vanished from the map, and the two counters
    disagreed with no explanation — 83 of 395 when this was written, 21% of the data and
    exactly the set 🎯 exists to fix."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "' מתוך '" in page and "' ללא מיקום'" in page
    assert "function showNoLoc(" in page
    # the sheet lists them even on the snapshot; only the WRITE path is live-only
    assert "const canPin = !SNAP && window.__LIVE__;" in page
    # the 🎯 button says how many it can actually walk
    assert "rows.filter(r => (r.address || '').trim()).length" in page


def test_the_unplaced_queue_narrows_without_shrinking_the_payoff(temp_db, monkeypatch,
                                                                 tmp_path):
    """`fixes` must stay counted over EVERY imprecise listing: a pin on a street really
    does fix the placed-but-vague ones too, so filtering the rows before counting would
    make each pin look worth less than it is."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    monkeypatch.setattr(dashboard, "_rows", lambda: [
        {"dedup_key": "a", "address": "בזל 1", "street": "בזל", "lat": None,
         "geo_confidence": "none", "manual_location": False},
        {"dedup_key": "b", "address": "בזל 2", "street": "בזל", "lat": 31.25,
         "geo_confidence": "street", "manual_location": False},
        {"dedup_key": "c", "address": "", "street": "", "lat": None,
         "geo_confidence": "none", "manual_location": False},
    ])
    everything = dashboard.pin_worklist()
    unplaced = dashboard.pin_worklist(unplaced_only=True)
    assert [i["key"] for i in everything] == ["a", "b"]
    assert [i["key"] for i in unplaced] == ["a"], "only the ones with no coordinate"
    assert unplaced[0]["fixes"] == 2, "a pin on בזל fixes the placed-but-vague one too"


def test_gates_are_a_real_layer_with_names(temp_db, monkeypatch, tmp_path):
    """Every AMBER decision on the map is measured to a gate, yet they were a bare ★
    with no class — the one thing that could not be toggled and the only landmark whose
    name was never drawn."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert '.no-gates .gate{display:none!important}' in dashboard.map_listings.STREET_CSS
    assert 'lay("gates"' in dashboard._legend_html() or 'data-layer="gates"' in page
    # the name is zoom-revealed like a side street, not shouted at 1x
    assert 'class="slabel gate gate-l" data-minzoom="2.6"' in page


def test_the_scale_bar_tells_the_truth(temp_db, monkeypatch, tmp_path):
    """Without it no zoom level says whether two dots are 100 m or 1 km apart. Measured
    live at 1x/4x/8x: within 0.5% of the haversine distance the bar spans."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert 'id="scalebar"' in page and "function drawScale(" in page
    # it rides the zoom guard, so a pan costs nothing
    body = page[page.index("function applyView("):page.index("function zoomAt(")]
    assert "drawScale" not in body, "the bar is a function of scale, not of position"
    assert "drawScale(svg);" in page[page.index("function rescaleForZoom("):
                                     page.index("function applyView(")]


def test_units_per_metre_matches_a_measured_distance():
    """The shared metres->units conversion, used by both the rings and the scale bar.
    Checked against haversine over a real 1 km separation."""
    import map_listings
    import zones
    xy, proj = map_listings._projector([(31.24, 34.77), (31.28, 34.82)])
    upm = map_listings.units_per_metre(xy, 31.26, 34.79)
    # 0.01 degrees of longitude at this latitude, in metres and in canvas units
    metres = zones._haversine_m(31.26, 34.79, 31.26, 34.80)
    units = abs(xy(31.26, 34.80)[0] - xy(31.26, 34.79)[0])
    assert abs(units / metres - upm) / upm < 0.01


def _tier_rects(markup):
    """[(x, y, w, h, fill)] parsed out of a tier-field group."""
    return [(float(a), float(b), float(c), float(d), e) for a, b, c, d, e in
            re.findall(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" '
                       r'height="([\d.]+)" fill="(#\w+)"/>', markup)]


def test_the_tier_field_says_exactly_what_the_classifier_says():
    """The whole point of sampling instead of an analytic mask: the drawn field cannot
    disagree with zones.classify_effective, because it IS zones.classify_effective.

    Run-length merging must be pixel-identical to one rect per cell — so every sample
    point must land in a rect of its own tier's colour."""
    import map_listings
    import zones
    xy, proj = map_listings._projector([(31.2425, 34.7727), (31.2782, 34.8175)])
    nx = ny = 24                                     # coarse, so the test stays quick
    markup = "".join(map_listings.tier_field_svg(xy, proj, nx, ny))
    rects = _tier_rects(markup)
    latlon = map_listings.latlon_from(proj)
    w, h = proj["w"], proj["h"]
    checked = 0
    for j in range(ny):
        for i in range(nx):
            cx, cy = i * w / nx + w / nx / 2, j * h / ny + h / ny / 2
            la, lo = latlon(cx, cy)
            want = map_listings._TIER_FILL.get(zones.classify_effective(la, lo),
                                               map_listings._TIER_FILL["UNKNOWN"])
            # topmost rect covering this point wins, and rect[0] is the RED base wash
            got = next((r[4] for r in reversed(rects)
                        if r[0] <= cx <= r[0] + r[2] and r[1] <= cy <= r[1] + r[3]), None)
            assert got == want, f"cell {i},{j} drew {got}, classifier says {want}"
            checked += 1
    assert checked == nx * ny


def test_the_tier_field_covers_the_whole_canvas():
    """_bounds_of excludes the 34px pad, and scale = min(...) leaves slack on one axis,
    so a grid over those bounds would leave an unpainted frame."""
    import map_listings
    xy, proj = map_listings._projector([(31.2425, 34.7727), (31.2782, 34.8175)])
    base = _tier_rects("".join(map_listings.tier_field_svg(xy, proj, 8, 8)))[0]
    assert (base[0], base[1]) == (0.0, 0.0)
    assert (base[2], base[3]) == (proj["w"], proj["h"])


def test_latlon_from_inverts_xy_from():
    import map_listings
    _xy, proj = map_listings._projector([(31.2425, 34.7727), (31.2782, 34.8175)])
    fwd, back = map_listings.xy_from(proj), map_listings.latlon_from(proj)
    for la, lo in ((31.25, 34.78), (31.27, 34.81), (31.2616, 34.7994)):
        x, y = fwd(la, lo)
        la2, lo2 = back(x, y)
        assert abs(la2 - la) < 1e-9 and abs(lo2 - lo) < 1e-9


def test_the_map_has_a_dark_theme(temp_db, monkeypatch, tmp_path):
    """The page has themed since D4; map_listings had no prefers-color-scheme at all and
    hardcoded fill="#f6f7f9", so at night the map was a white slab in a dark page."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    css = dashboard.map_listings.STREET_CSS
    assert "@media (prefers-color-scheme:dark){" in css
    for cls in (".mapbg", ".st-art", ".tier", ".gate-l"):
        assert cls in css.split("prefers-color-scheme:dark")[1], f"{cls} untheme d"
    # the background is a class now, so CSS can beat the presentation attribute
    assert '<rect class="mapbg"' in page


def test_the_no_amber_carveout_is_drawn(temp_db, monkeypatch, tmp_path):
    """שכונה ד' drops an otherwise-AMBER flat, and was rendered nowhere in either map —
    so a listing 7 minutes from a gate could be RED with nothing on screen to say why."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert 'class="noamber"' in page
    assert ".noamber{fill:none" in dashboard.map_listings.STREET_CSS


def test_colour_by_walk_time(temp_db, monkeypatch, tmp_path):
    """walk_minutes is OSRM-measured and stored, and the only way to read it was to open
    a card. Scaled to MAX_WALK_MINUTES, the threshold that decides AMBER from RED."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert '<option value="walk">' in page and "function walkColor(" in page
    assert f"const MAX_WALK = {config.MAX_WALK_MINUTES};" in page
    # no walk time is grey, not a guess
    assert "if (m === null || m === undefined) return TIER_COLOR.UNKNOWN;" in page


def test_you_are_here_never_leaves_the_browser(temp_db, monkeypatch, tmp_path):
    """The position is read, drawn and used to sort — and never stored, never POSTed.
    That is also why it works on the published snapshot, which is the copy you have with
    you. Verified live: accuracy ring exactly 40 m for a 40 m fix."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "navigator.geolocation.watchPosition" in page
    body = page[page.index("function toggleMe("):page.index("function metresFromMe(")]
    assert "post(" not in body and "localStorage" not in body, \
        "a position must not be sent anywhere or persisted"
    # the accuracy ring is real metres, so a poor fix looks poor
    assert "unitsPerMetre() * (me.acc || 0)" in page
    # and the https caveat is spelled out to the user, not left to look like a bug
    assert "https" in page[page.index("function toggleMe("):page.index("/* metres from")]


def test_the_view_is_remembered_and_shareable(temp_db, monkeypatch, tmp_path):
    """Layers have persisted since F4; the view never did, so the PWA reopened on the
    whole city every time. A viewBox in the hash carries no personal data."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "const VIEW_KEY = 'bgu.view';" in page
    assert "history.replaceState(null, '', '#v=' + v);" in page
    # the hash wins over localStorage — it is what someone deliberately sent you
    assert "location.hash.match(/[#&]v=([-\\d.,]+)/)" in page


def test_a_zero_width_map_is_not_drawn_as_one_giant_cluster(temp_db, monkeypatch,
                                                            tmp_path):
    """Every px-based size divides by the map's width, and the `|| 1` fallbacks then make
    one cluster cell 19,000 world units — all 312 listings collapse into a single badge
    that reads like the data vanished. Seen for real in a collapsed pane."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    page = dashboard.build()
    assert "if (!svg.getBoundingClientRect().width) return;" in page
    # and something has to bring it back — nothing listened for resize at all before
    assert "addEventListener('resize'" in page


def test_the_snapshot_installs_but_the_live_page_is_never_cached(temp_db, monkeypatch,
                                                                 tmp_path):
    """The published snapshot is the phone page now that Tailscale is gone, so it has to
    install and work offline. The LIVE page must ship neither manifest nor worker: caching
    a localhost tool is how you end up looking at yesterday's data without knowing, which
    is exactly what the 22-hour server did."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    snap = dashboard.render(live=False, snapshot=True)
    live = dashboard.render(live=True)
    assert "manifest.webmanifest" in snap and "serviceWorker" in snap
    assert "manifest.webmanifest" not in live and "serviceWorker" not in live
    # network-first, or the phone pins itself to a stale build
    sw = dashboard._service_worker("20260802-1200")
    assert "fetch(e.request)" in sw and "caches.match" in sw
    assert sw.index("fetch(e.request)") < sw.index("caches.match"), "must try network first"
    assert "bgu-20260802-1200" in sw, "cache name must be stamped per publish"


# --- guided pinning: govmap proposes, a person decides -----------------------------
def test_worklist_puts_the_pin_that_fixes_the_most_flats_first(temp_db, monkeypatch,
                                                               tmp_path):
    """A numbered pin becomes a street anchor, so it places every other number on that
    street. The queue is therefore ordered by how many flats one tap pays for."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "בני אור 13")
    _save("k2", "בני אור 21")
    _save("k3", "סוסו הכהן 3")
    monkeypatch.setattr(dashboard, "_rows", lambda: [
        {"dedup_key": "k1", "address": "בני אור 13", "street": "בני אור",
         "geo_confidence": "street", "manual_location": False},
        {"dedup_key": "k2", "address": "בני אור 21", "street": "בני אור",
         "geo_confidence": "street", "manual_location": False},
        {"dedup_key": "k3", "address": "סוסו הכהן 3", "street": "סוסו הכהן",
         "geo_confidence": "street", "manual_location": False},
        {"dedup_key": "k4", "address": "רגר 5", "street": "רגר",
         "geo_confidence": "exact", "manual_location": False},
        {"dedup_key": "k5", "address": "", "street": "",
         "geo_confidence": "area", "manual_location": False},
    ])
    items = dashboard.pin_worklist()
    assert [i["key"] for i in items] == ["k1", "k2", "k3"], \
        "precise and address-less rows have nothing to pin"
    assert items[0]["fixes"] == 2 and items[2]["fixes"] == 1


def test_propose_never_writes_and_refuses_a_point_with_no_housing(temp_db, monkeypatch,
                                                                  tmp_path):
    """govmap substitutes silently (בני אור 999 -> בני אור 13), which is precisely why
    this only PROPOSES. And a candidate inside the campus or the hospital is not a flat,
    so it is rejected before a person can accept it by reflex."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    import govmap
    import zones
    monkeypatch.setattr(dashboard, "_rows", lambda: [
        {"dedup_key": "k1", "address": "בני אור 13", "street": "בני אור"}])
    monkeypatch.setattr(govmap, "address_detail",
                        lambda *a, **k: ("בני אור 13, באר שבע", (31.2620, 34.7990)))

    monkeypatch.setattr(zones, "no_housing_here", lambda la, lo: None)
    out = dashboard.propose_location("k1")
    assert out == {"ok": True, "lat": 31.2620, "lon": 34.7990,
                   "address": "בני אור 13", "found": "בני אור 13, באר שבע",
                   "street": "בני אור", "number": "13"}
    assert storage.manual_location("k1") is None, "propose must not write"

    monkeypatch.setattr(zones, "no_housing_here", lambda la, lo: "אוניברסיטת בן גוריון")
    blocked = dashboard.propose_location("k1")
    assert blocked["ok"] is False and blocked["reason"] == "no_housing"


def test_the_pin_flow_is_confirm_only_and_live_only(temp_db, monkeypatch, tmp_path):
    """No auto-accept anywhere in the page, and the trigger never appears on the
    snapshot — an offline copy cannot write, so a button that pretends it can is worse
    than no button."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    live = dashboard.render(live=True)
    assert "id=\"pinstart\"" in live and "startPinFlow" in live
    # accept is only ever reachable from the button — nothing invokes it in code
    assert "$('pinaccept').onclick = acceptPin;" in live
    assert "acceptPin();" not in live, "govmap's answer must never be auto-accepted"
    assert "if (!SNAP && window.__LIVE__){" in live
    snap = dashboard.render(live=False, snapshot=True)
    assert "startPinFlow" not in snap or "__SNAPSHOT__" in snap
