"""storage — the vote ledger (one vote per user, final) and the file_id cache
that keeps top-N albums alive after Facebook URLs expire."""
import config
import storage
from models import ListingExtract, PipelineResult, Status


def _res(key):
    e = ListingExtract(is_apartment_ad=True, price_per_room_ils=1500,
                       available_rooms_count=2, total_roommates_in_apt=3,
                       street_address_or_neighborhood="רגר 1")
    return PipelineResult(status=Status.MATCH, dedup_key=key, location_tier="GREEN",
                          score=80, images=["http://u1", "http://u2"], extract=e)


def test_vote_is_once_per_user_and_final(temp_db):
    k = "phone:501234567"
    assert storage.set_mark(k, "u1", "saved") is True      # first vote records
    assert storage.set_mark(k, "u1", "saved") is False     # repeat rejected
    assert storage.set_mark(k, "u1", "dismissed") is False  # no flipping
    assert storage.get_user_mark(k, "u1") == "saved"        # original stands


def test_counts_and_net_adjustment(temp_db):
    k = "phone:1"
    storage.set_mark(k, "u1", "saved")
    storage.set_mark(k, "u2", "saved")
    storage.set_mark(k, "u3", "dismissed")
    assert storage.mark_counts(k) == {"saved": 2, "dismissed": 1}
    assert storage.mark_adjustment(k) == config.MARK_SCORE_DELTA   # 2*Δ - 1*Δ = Δ


def test_effective_score_is_base_plus_votes(temp_db):
    k = "hash:xyz"
    assert storage.base_score(k) == 0                       # no listing row yet
    storage.set_mark(k, "u1", "saved")
    assert storage.effective_score(k, base=10) == 10 + config.MARK_SCORE_DELTA


def test_file_ids_roundtrip_and_no_wipe(temp_db):
    k = "phone:2"
    storage.save_listing(_res(k))
    assert storage.get_images(k) == ["http://u1", "http://u2"]
    assert storage.get_file_ids(k) == []
    storage.set_file_ids(k, ["AAA", "BBB"])
    assert storage.get_file_ids(k) == ["AAA", "BBB"]
    storage.set_file_ids(k, [])                             # empty must be a no-op
    assert storage.get_file_ids(k) == ["AAA", "BBB"]


def test_save_listing_persists_score(temp_db):
    k = "phone:3"
    storage.save_listing(_res(k))
    assert storage.base_score(k) == 80


def test_post_archive_and_stats(temp_db):
    e = ListingExtract(is_apartment_ad=True, price_per_room_ils=1500,
                       street_address_or_neighborhood="רגר 1")
    match = PipelineResult(status=Status.MATCH, location_tier="GREEN", score=80,
                           reason="ok", extract=e)
    storage.record_post("sig1", "raw text", "", ["u1"], "grp", "http://x", e, match)
    posts = storage.all_posts()
    assert len(posts) == 1
    assert posts[0]["verdict"] == "MATCH" and posts[0]["raw_text"] == "raw text"
    # re-recording the same sig updates in place (no duplicate row)
    drop = PipelineResult(status=Status.DROP, reason="too far", extract=e)
    storage.record_post("sig1", "raw text", "", [], "grp", "http://x", e, drop)
    assert len(storage.all_posts()) == 1
    assert storage.verdict_counts() == {"DROP": 1}
    assert storage.drop_reason_counts()[0][0] == "too far"


def test_unknown_locations_counts(temp_db):
    storage.record_unknown_location("הבלוק")
    storage.record_unknown_location("הבלוק")
    storage.record_unknown_location("הרובע")
    storage.record_unknown_location("  ")          # blank ignored
    rows = storage.unknown_locations(days=7)
    assert rows[0][0] == "הבלוק" and rows[0][1] == 2   # most frequent first
    assert ("הרובע", 1) == (rows[1][0], rows[1][1])


def test_fuzzy_dedup_matches_near_identical(temp_db):
    base = set("דירת שלושה שותפים בשכונה מתפנים שני חדרים ממוזגת מרוהטת כניסה מיידית להשכרה".split())
    storage.record_fingerprint("phone:9", base)
    # a repost with one word changed / added -> still a duplicate
    repost = set(list(base) + ["טלפון", "לפרטים"])
    assert storage.find_similar(repost) == "phone:9"
    # a genuinely different flat shares only a few generic words -> not a dup
    other = set("דירת חדר יחיד סטודיו במרכז העיר קרוב לתחנה זולה משופצת".split())
    assert storage.find_similar(other) is None
    # too-short text is never fuzzy-matched
    assert storage.find_similar({"דירה", "להשכרה"}) is None
