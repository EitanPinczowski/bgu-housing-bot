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


def test_furnished_floor_persisted(temp_db):
    import sqlite3
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 1",
                       floor="3", furnished=True)
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="kf",
                         location_tier="GREEN", score=80, extract=e))
    assert sqlite3.connect(temp_db).execute(
        "SELECT floor, furnished FROM listings WHERE dedup_key='kf'").fetchone() == ("3", 1)
    # False -> 0, None -> None (the null/false distinction survives)
    for val, exp in ((False, 0), (None, None)):
        storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="kf2",
                             score=80, extract=ListingExtract(is_apartment_ad=True, furnished=val)))
        assert sqlite3.connect(temp_db).execute(
            "SELECT furnished FROM listings WHERE dedup_key='kf2'").fetchone()[0] == exp


def test_set_source_url_backfill(temp_db):
    storage.save_listing(_res("phone:7"))
    storage.set_source_url("phone:7", "https://www.facebook.com/groups/1/posts/2/")
    import sqlite3
    assert sqlite3.connect(temp_db).execute(
        "SELECT source_url FROM listings WHERE dedup_key='phone:7'").fetchone()[0] \
        == "https://www.facebook.com/groups/1/posts/2/"


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


def test_group_yield(temp_db):
    e = ListingExtract(is_apartment_ad=True)

    def rec(sig, group, status):
        storage.record_post(sig, "t", "", [], group, "u", e,
                            PipelineResult(status=status, extract=e))

    rec("a", "g1", Status.MATCH)
    rec("b", "g1", Status.NEEDS_DATA)
    rec("c", "g2", Status.DROP)
    gy = {g: (tot, m, n, d) for g, tot, m, n, d, _na in storage.group_yield()}
    assert gy["g1"] == (2, 1, 1, 0)
    assert gy["g2"] == (1, 0, 0, 1)


def test_delete_listing(temp_db):
    import sqlite3
    import config as cfg
    storage.save_listing(_res("phone:9"))
    assert storage.base_score("phone:9") == 80
    storage.delete_listing("phone:9")
    n = sqlite3.connect(cfg.DB_PATH).execute(
        "SELECT COUNT(*) FROM listings WHERE dedup_key='phone:9'").fetchone()[0]
    assert n == 0


def test_prune_old_posts(temp_db):
    import sqlite3
    import config as cfg
    e = ListingExtract(is_apartment_ad=True, price_per_room_ils=1500)
    res = PipelineResult(status=Status.MATCH, score=80, extract=e)
    storage.record_post("old", "raw old", "", [], "g", "u", e, res)
    con = sqlite3.connect(cfg.DB_PATH)
    con.execute("UPDATE posts SET first_seen='2020-01-01 00:00:00' WHERE sig='old'")
    con.commit()
    con.close()
    storage.record_post("new", "raw new", "", [], "g", "u", e, res)
    assert storage.prune_old_posts(90) == 1
    rows = {p["sig"]: p for p in storage.all_posts()}
    assert rows["old"]["raw_text"] == "" and rows["old"]["verdict"] == "MATCH"  # kept, lightened
    assert rows["new"]["raw_text"] == "raw new"                                 # fresh intact
    assert len(rows) == 2                                                        # both survive


def test_unknown_locations_counts(temp_db):
    storage.record_unknown_location("הבלוק")
    storage.record_unknown_location("הבלוק")
    storage.record_unknown_location("הרובע")
    storage.record_unknown_location("  ")          # blank ignored
    rows = storage.unknown_locations(days=7)
    assert rows[0][0] == "הבלוק" and rows[0][1] == 2   # most frequent first
    assert ("הרובע", 1) == (rows[1][0], rows[1][1])


def _extract(addr, price=None, avail=None, total=None, contact=None):
    return ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=addr,
                          price_per_room_ils=price, available_rooms_count=avail,
                          total_roommates_in_apt=total, contact_phone_or_link=contact)


def test_addr_key_only_for_numbered_address():
    assert storage._addr_key(_extract("רינגלבלום 1")) == "addr:רינגלבלום 1"
    assert storage._addr_key(_extract("שכונה ב")) is None          # bare neighborhood
    assert storage._addr_key(_extract("רחוב קדש")) is None         # street, no number
    assert any(k.startswith("addr:") for k in storage.dedup_keys(_extract("רינגלבלום 1")))
    assert not any(k.startswith("addr:") for k in storage.dedup_keys(_extract("שכונה ב")))


def test_multikey_collapses_phone_and_field_flip(temp_db):
    # the רינגלבלום 1 case: same numbered flat, read A has the phone + one price,
    # read B has neither the phone nor the same price -> primary keys differ, but the
    # numbered-address key ties them so read B is recognised as already seen.
    a = _extract("רינגלבלום 1", price=2000, contact="050-1234567")
    b = _extract("רינגלבלום 1", price=1800)
    assert storage.make_dedup_key(a) != storage.make_dedup_key(b)
    assert not storage.is_seen_any(storage.dedup_keys(b))
    storage.mark_seen_all(storage.dedup_keys(a))
    assert storage.is_seen_any(storage.dedup_keys(b))


def test_bare_neighborhood_flats_stay_separate(temp_db):
    # two genuinely different flats in שכונה ב (no house number) must NOT collapse
    storage.mark_seen_all(storage.dedup_keys(_extract("שכונה ב", price=2000)))
    assert not storage.is_seen_any(storage.dedup_keys(_extract("שכונה ב", price=1500)))


def test_prune_orphan_listings(temp_db):
    # a listing whose key IS derivable from an archived parse is kept; one whose key
    # is not (its post was re-parsed to a different key) is pruned.
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 1",
                       contact_phone_or_link="050-1234567")
    live_key = storage.make_dedup_key(e)                 # phone:501234567
    storage.record_post("sig1", "raw", "", [], "g", "u", e,
                        PipelineResult(status=Status.MATCH, dedup_key=live_key, score=80, extract=e))
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key=live_key,
                         location_tier="GREEN", score=80, extract=e))
    # an orphan listing whose key maps to no archived parse
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="hash:orphan00000000",
                         location_tier="GREEN", score=60, extract=e))
    assert storage.prune_orphan_listings() == 1
    import sqlite3
    keys = [r[0] for r in sqlite3.connect(temp_db).execute("SELECT dedup_key FROM listings").fetchall()]
    assert keys == [live_key]                            # orphan gone, derivable kept


def test_merge_duplicate_listings(temp_db):
    def save(key, price, avail, total, contact, score):
        e = _extract("רגר 164", price, avail, total, contact)
        storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key=key,
                             location_tier="GREEN", score=score, extract=e))
    save("phone:1234567", 1100, 2, 3, "050-1234567", 82)   # richer row
    save("hash:deadbeef00000000", None, 2, None, None, 75)  # sparse duplicate
    storage.set_mark("hash:deadbeef00000000", "u1", "saved")   # a vote on the doomed row
    assert storage.merge_duplicate_listings() == 1
    import sqlite3
    keys = [r[0] for r in sqlite3.connect(temp_db).execute(
        "SELECT dedup_key FROM listings WHERE address='רגר 164'").fetchall()]
    assert keys == ["phone:1234567"]                       # kept the richer row
    assert storage.get_user_mark("phone:1234567", "u1") == "saved"   # vote migrated


def test_saved_listings_and_contacted(temp_db):
    storage.save_listing(_res("phone:501111111"))
    storage.set_mark("phone:501111111", "u1", "saved")
    assert any(r["dedup_key"] == "phone:501111111" for r in storage.saved_listings())
    # marking contacted records it and drops it from the saved list
    storage.set_contacted("phone:501111111")
    assert "phone:501111111" in storage.contacted_keys()
    assert not any(r["dedup_key"] == "phone:501111111" for r in storage.saved_listings())
    assert storage.mark_adjustment("phone:501111111") == config.MARK_SCORE_DELTA  # contacted not a vote


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
