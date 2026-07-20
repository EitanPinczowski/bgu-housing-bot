"""query: parse a free Hebrew/English search string into filters, and rank the
matching stored listings by vote-adjusted score."""
import query
import storage
from models import ListingExtract, PipelineResult, Status


def test_parse_mixed_query():
    f = query._parse("2 rooms under 1500 green october רגר")
    assert f["max_price"] == 1500 and f["rooms"] == 2
    assert f["tier"] == "GREEN" and f["month"] == 10
    assert f["terms"] == ["רגר"]                       # month word not treated as text


def test_parse_hebrew_and_stars():
    f = query._parse("4 כוכבים צהוב עד 1800")
    assert f["min_score"] == 70 and f["tier"] == "AMBER" and f["max_price"] == 1800
    assert "terms" not in f                             # all words were filter keywords


def _save(key, addr, price, avail, tier, score, lease=None, status=Status.MATCH):
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=addr,
                       price_per_room_ils=price, available_rooms_count=avail,
                       lease_start_date=lease)
    storage.save_listing(PipelineResult(status=status, dedup_key=key,
                         location_tier=tier, score=score, extract=e))


def test_search_filters_and_ranks(temp_db):
    _save("k1", "רגר 1", 1400, 2, "GREEN", 60, lease="1.10")
    _save("k2", "הרצל 5", 1200, 2, "GREEN", 80, lease="1.10")
    _save("k3", "רמות 9", 1900, 2, "GREEN", 90)          # too pricey for 'under 1500'
    _save("k4", "אלון 3", 1000, 1, "AMBER", 95)          # amber + only 1 room
    rows = query.search("2 rooms under 1500 green", limit=10)
    keys = [r["dedup_key"] for r in rows]
    assert keys == ["k2", "k1"]                          # k3 filtered (price), k4 (tier+rooms); ranked by score


def test_search_votes_boost_ranking(temp_db):
    _save("a", "רגר 1", 1400, 2, "GREEN", 70)
    _save("b", "הרצל 5", 1400, 2, "GREEN", 75)
    storage.set_mark("a", "u1", "saved")                 # +MARK_SCORE_DELTA lifts 'a' over 'b'
    rows = query.search("green", limit=10)
    assert [r["dedup_key"] for r in rows] == ["a", "b"]


def test_search_month_filter(temp_db):
    _save("oct", "רגר 1", 1400, 2, "GREEN", 80, lease="1.10")
    _save("sep", "הרצל 5", 1400, 2, "GREEN", 90, lease="1.9")
    rows = query.search("october green", limit=10)
    assert [r["dedup_key"] for r in rows] == ["oct"]     # september one excluded
