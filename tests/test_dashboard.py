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
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5", price=1400)
    _save("k2", "קדש 3", price=1900, status=Status.NEEDS_DATA)
    page = dashboard.build()
    assert "רגר 5" in page and "קדש 3" in page
    for control in ("id=\"q\"", "id=\"st\"", "id=\"tier\"", "id=\"maxp\"",
                    "id=\"minr\"", "id=\"stale\"", "id=\"nobroker\"", "id=\"saved\""):
        assert control in page
    # the filter attributes the JS reads must be emitted on each row
    assert "data-status='MATCH'" in page and "data-price='1400'" in page


def test_address_is_escaped(temp_db, monkeypatch, tmp_path):
    """A listing address is untrusted text straight from a Facebook post."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "<script>alert(1)</script> רגר")
    page = dashboard.build()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_unknown_price_and_walk_sort_last_ascending(temp_db, monkeypatch, tmp_path):
    """A blank price is not "the cheapest" — unknowns must sink, not top the list."""
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5", price=None, walk=None)
    page = dashboard.build()
    assert 'data-v="1000000"' in page               # price sentinel
    assert 'data-v="999"' in page                   # walk sentinel
    # …and a known value is written as itself, not a sentinel
    _save("k2", "קדש 3", price=1400, walk=6.0)
    page = dashboard.build()
    assert 'data-v="1400"' in page and 'data-v="6.0"' in page


def test_broker_and_vote_flags_show(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "OUT", tmp_path / "d.html")
    _save("k1", "רגר 5")
    storage.set_mark("k1", "u1", "saved")
    monkeypatch.setattr(storage, "phone_listing_count",
                        lambda p: config.BROKER_MIN_LISTINGS + 1)
    page = dashboard.build()
    assert "⭐" in page
    assert f"⚠️{config.BROKER_MIN_LISTINGS + 1}" in page
    assert "data-broker='1'" in page and "data-saved='1'" in page


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
