"""storage — the vote ledger (one vote per user, final) and the file_id cache
that keeps top-N albums alive after Facebook URLs expire."""
import sqlite3

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


def test_votes_stack_uncapped_above_100(temp_db):
    # a human ⭐ adds its full weight on top of the 0–100 quality score — never swallowed
    assert config.MARK_SCORE_DELTA >= 10                    # each vote adds at least ten
    storage.set_mark("k", "u1", "saved")
    assert storage.effective_score("k", base=100) == 100 + config.MARK_SCORE_DELTA


def test_file_ids_roundtrip_and_no_wipe(temp_db):
    k = "phone:2"
    storage.save_listing(_res(k))
    assert storage.get_images(k) == ["http://u1", "http://u2"]
    assert storage.get_file_ids(k) == []
    storage.set_file_ids(k, ["AAA", "BBB"])
    assert storage.get_file_ids(k) == ["AAA", "BBB"]
    storage.set_file_ids(k, [])                             # empty must be a no-op
    assert storage.get_file_ids(k) == ["AAA", "BBB"]


def _ex(addr=None, phone=None, **kw):
    from models import ListingExtract
    return ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=addr,
                          contact_phone_or_link=phone, **kw)


def test_one_landlord_two_numbered_flats_are_two_listings(temp_db):
    """The archive showed 42 phones advertising more than one numbered address (one
    posts 32), and phone-only keys collapsed 101 distinct flats into single rows —
    every flat after the first was dropped as 'already seen'."""
    a = _ex("אברהם אבינו 38", "050-1234567")
    b = _ex("אברהם אבינו 57", "050-1234567")
    assert storage.make_dedup_key(a) != storage.make_dedup_key(b)
    storage.mark_seen_all(storage.dedup_keys(a))
    assert storage.is_duplicate(a) is True             # the same flat again
    assert storage.is_duplicate(b) is False            # a different flat, same landlord


def test_the_same_flat_still_collapses_across_reposts(temp_db):
    """Why the phone key exists in the first place — a repost with the address written
    slightly differently must NOT alert twice."""
    a = _ex("רגר 164", "050-1234567", price_per_room_ils=1500)
    b = _ex("רגר 164 ", "0501234567", price_per_room_ils=1600)   # spacing + format differ
    storage.mark_seen_all(storage.dedup_keys(a))
    assert storage.is_duplicate(b) is True


def test_a_vague_read_never_becomes_a_second_row(temp_db):
    """A read with no house number can't be told apart from a vaguer re-read of the
    flat we stored, so it stays collapsed — a wrong duplicate alert is the worse bug."""
    precise = _ex("רגר 164", "050-1234567")
    storage.mark_seen_all(storage.dedup_keys(precise))
    assert storage.is_duplicate(_ex("רגר, אצל דני", "050-1234567")) is True
    assert storage.is_duplicate(_ex(None, "050-1234567")) is True
    # …but a different phone with no address is genuinely a different listing
    assert storage.is_duplicate(_ex("רגר", "052-7654321")) is False


def test_phone_listing_count_identifies_an_agency(temp_db):
    """Counted from the ARCHIVE, not the listings table: an agency whose flats are
    mostly out of the search zone would otherwise look like a private landlord."""
    from models import PipelineResult, Status
    for n in (3, 8, 21, 21):                      # 21 twice — distinct addresses only
        e = _ex(f"אברהם אבינו {n}", "050-1234567")
        storage.record_post(f"sig{n}-{len(str(n))}", "טקסט", "", [], "g", None, e,
                            PipelineResult(status=Status.DROP, dedup_key="x"))
    # a second contact, and a post with no house number (can't count as a distinct flat)
    storage.record_post("sig-other", "טקסט", "", [], "g", None,
                        _ex("קדש 5", "052-7654321"),
                        PipelineResult(status=Status.DROP, dedup_key="y"))
    storage.record_post("sig-bare", "טקסט", "", [], "g", None,
                        _ex("שכונה ב", "050-1234567"),
                        PipelineResult(status=Status.DROP, dedup_key="z"))
    storage.invalidate_broker_counts()
    assert storage.phone_listing_count("050-1234567") == 3     # not 4, not 5
    assert storage.phone_listing_count("0501234567") == 3      # formatting-independent
    assert storage.phone_listing_count("052-7654321") == 1
    assert storage.phone_listing_count("052-0000000") == 0
    assert storage.phone_listing_count(None) == 0
    assert storage.phone_listing_count("123") == 0     # too short to be a phone


def test_amenities_roundtrip(temp_db):
    from models import ListingExtract, PipelineResult, Status
    am = {"bus669": {"label": "669 מרגר", "icon": "🚌", "kind": "bus_route",
                     "options": [{"minutes": 6.0, "headway_min": 20}]}}
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="am:1",
                                        amenities=am,
                                        extract=ListingExtract(is_apartment_ad=True)))
    assert storage.listing_amenities("am:1") == am          # Hebrew survives the JSON


def test_amenities_absent_reads_as_empty(temp_db):
    # a row written before the column existed, and one with nothing resolved, both
    # read back as {} rather than blowing up a digest
    storage.save_listing(_res("am:2"))
    assert storage.listing_amenities("am:2") == {}
    assert storage.listing_amenities("no-such-key") == {}


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
    # False -> 0, None -> None (the null/false distinction survives). Separate rows:
    # writing None OVER a stored 0 no longer blanks it — see the enrichment tests.
    for key, val, exp in (("kf2", False, 0), ("kf3", None, None)):
        storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key=key,
                             score=80, extract=ListingExtract(is_apartment_ad=True, furnished=val)))
        assert sqlite3.connect(temp_db).execute(
            "SELECT furnished FROM listings WHERE dedup_key=?", (key,)).fetchone()[0] == exp


def test_resave_enriches_and_never_blanks(temp_db):
    """A thinner later read must only ever ADD detail. Before this, re-saving a flat
    whose price the LLM missed that time wiped the price we already had."""
    import sqlite3
    rich = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 5",
                          price_per_room_ils=1400, available_rooms_count=2, floor="3",
                          furnished=True, contact_phone_or_link="050-1234567")
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k", score=80,
                                        location_tier="GREEN", walk_minutes=7.0,
                                        images=["http://a"], extract=rich))
    thin = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 5")
    storage.save_listing(PipelineResult(status=Status.NEEDS_DATA, dedup_key="k", score=55,
                                        location_tier="AMBER", walk_minutes=9.0,
                                        extract=thin))
    row = sqlite3.connect(temp_db).execute(
        """SELECT price_per_room, available_rooms, floor, furnished, contact, images,
                  status, score, location_tier, walk_minutes
           FROM listings WHERE dedup_key='k'""").fetchone()
    # detail kept…
    assert row[:5] == (1400, 2, "3", 1, "050-1234567")
    assert row[5] == '["http://a"]'                    # photos aren't dropped either
    # …while the freshly computed verdict does replace the old one
    assert row[6:] == ("NEEDS_DATA", 55, "AMBER", 9.0)


def test_enrichment_fills_a_gap_rather_than_duplicating(temp_db):
    """The cross-source case: a second read knows the price the first one lacked."""
    import sqlite3
    first = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="קדש 3",
                           available_rooms_count=2)
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k2", score=70,
                                        extract=first))
    second = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="קדש 3",
                            price_per_room_ils=1550, balcony_or_garden="מרפסת")
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k2", score=75,
                                        extract=second))
    c = sqlite3.connect(temp_db)
    assert c.execute("SELECT COUNT(*) FROM listings WHERE dedup_key='k2'").fetchone()[0] == 1
    assert c.execute("SELECT available_rooms, price_per_room, balcony FROM listings "
                     "WHERE dedup_key='k2'").fetchone() == (2, 1550, "מרפסת")


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
    # the key is now the CANONICAL street + number, not the post's wording, so assert
    # the contract rather than one literal spelling
    assert storage._addr_key(_extract("רינגלבלום 1")).startswith("addr:")
    assert storage._addr_key(_extract("רינגלבלום 1")).endswith("|1")
    assert storage._addr_key(_extract("שכונה ב")) is None          # bare neighborhood
    assert storage._addr_key(_extract("רחוב קדש")) is None         # street, no number
    assert any(k.startswith("addr:") for k in storage.dedup_keys(_extract("רינגלבלום 1")))
    assert not any(k.startswith("addr:") for k in storage.dedup_keys(_extract("שכונה ב")))


def test_one_flat_described_two_ways_is_one_key():
    """Scrubbing whitespace was not enough: landlords describe one flat many ways and
    each phrasing minted its own key, so the same flat was stored twice. Measured over
    399 listings on 2026-08-02 — 11 duplicate pairs, all of them this."""
    def key(addr):
        return storage.make_dedup_key(_extract(addr, contact="050-8220245"))
    for a, b in (("רגר 93, הבלוק", "רגר 93, גבול בין שכונה ב' ל-שכונה ד', הבלוק"),
                 ("ברגר 155", "רגר 155"),                    # a ב proclitic
                 ("רחוב סוסו הכהן 6", "סוסו הכהן 6"),          # a road-type word
                 ("ו' הישנה, בן מתיתיהו 13", "בן מתיתיהו 13, ו' הישנה"),   # order
                 ("רח' וינגייט 64", "רחוב וינגייט 64")):       # an abbreviation
        assert key(a) == key(b), f"{a!r} vs {b!r}"
    # …and the 2026-07-29 rule is untouched: still phone + NUMBERED address
    assert key("רגר 93") != key("רגר 95"), "different flats must not merge"
    assert key("רחוב קדש") == "phone:508220245", "no number -> phone alone, as before"
    assert (storage.make_dedup_key(_extract("רגר 93", contact="0501111111")) !=
            storage.make_dedup_key(_extract("רגר 93", contact="0502222222")))


def test_a_flat_already_stored_is_not_re_alerted():
    """`seen` is full of keys built from the raw wording. Once the address part became
    `street|number` those stopped matching, so a repost of a known flat would have looked
    brand new and alerted again — for every listing in the table."""
    e = _extract("רגר 93, הבלוק", contact="050-8220245")
    keys = storage.dedup_keys(e)
    assert storage.make_dedup_key(e) in keys
    assert "phone:508220245|רגר 93, הבלוק" in keys, "the legacy key must still be checked"
    assert "addr:רגר 93, הבלוק" in keys


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


def test_stale_keys(temp_db):
    import sqlite3
    storage.save_listing(_res("phone:fresh"))
    storage.save_listing(_res("phone:old"))
    con = sqlite3.connect(temp_db)
    con.execute("UPDATE listings SET first_seen='2020-01-01 00:00:00' WHERE dedup_key='phone:old'")
    con.commit()
    con.close()
    stale = storage.stale_keys()
    assert "phone:old" in stale and "phone:fresh" not in stale


def test_posted_at_recorded(temp_db):
    import sqlite3
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 1")
    res = PipelineResult(status=Status.MATCH, score=80, extract=e)
    storage.record_post("s1", "raw", "", [], "g", "u", e, res, age_hours=5)
    got = sqlite3.connect(temp_db).execute(
        "SELECT posted_at FROM posts WHERE sig='s1'").fetchone()[0]
    assert got                                    # ~5h before now
    # unknown age is harmless, and a later re-record doesn't wipe a known posted_at
    storage.record_post("s2", "raw", "", [], "g", "u", e, res, age_hours=None)
    assert sqlite3.connect(temp_db).execute(
        "SELECT posted_at FROM posts WHERE sig='s2'").fetchone()[0] is None
    storage.record_post("s1", "raw", "", [], "g", "u", e, res, age_hours=None)
    assert sqlite3.connect(temp_db).execute(
        "SELECT posted_at FROM posts WHERE sig='s1'").fetchone()[0] == got


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


def test_rekey_migration_carries_votes(temp_db):
    """Old rows are keyed on the bare phone; without the migration a re-read would add
    a second row and the ⭐ votes would be stranded on the orphan."""
    import sqlite3
    e = _ex("רגר 164", "050-1234567")
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="phone:501234567",
                                        score=80, extract=e))
    storage.set_mark("phone:501234567", "u1", "saved")
    assert storage.rekey_phone_listings() == 1
    new = storage.make_dedup_key(e)          # whatever the current key format is
    assert new.startswith("phone:501234567|") and new.endswith("|164")
    c = sqlite3.connect(temp_db)
    assert c.execute("SELECT COUNT(*) FROM listings WHERE dedup_key=?", (new,)).fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM listings WHERE dedup_key='phone:501234567'"
                     ).fetchone()[0] == 0
    assert storage.effective_score(new, base=80) > 80          # the ⭐ came along
    assert storage.rekey_phone_listings() == 0                 # idempotent


def test_merge_never_collapses_two_different_landlords(temp_db):
    """One address is NOT one flat. Grouping by address alone survived while the key
    held the post's raw wording; once it became `street|number`, far more rows collide —
    and in a student building several landlords advertise different flats at the same
    number. Measured 2026-08-02: 11 of 40 colliding groups held more than one contact,
    17 rows, and merging them would have deleted real listings."""
    a = _extract("וינגייט 64, שכונה ג", price=1500, contact="054-3376992")
    b = _extract("רחוב וינגייט 64", price=1800, contact="052-4708225")
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k-a", score=80,
                                        extract=a))
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k-b", score=70,
                                        extract=b))
    assert storage.merge_duplicate_listings() == 0, "different contacts must not merge"

    # …but the case this exists for still works: one contact, and a read that missed it.
    # A DIFFERENT address, or these would join the two-landlord group above.
    c = _extract("סוסו הכהן 6", price=1500, contact="054-3376992")
    d = _extract("רחוב סוסו הכהן 6", price=1500)                 # no contact on this read
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k-c", score=80,
                                        extract=c))
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k-d", score=60,
                                        extract=d))
    assert storage.merge_duplicate_listings() == 1


def test_a_vague_read_folds_into_the_numbered_one(temp_db):
    """One flat arrives twice, once described and once addressed: `מול שער האוניברסיטה`
    and `רחוב רגר 153` from the same landlord, both 1300₪ for 3 rooms. is_duplicate
    collapses that at ingest only when the vague read comes SECOND."""
    vague = _extract("מול שער האוניברסיטה", price=1300, avail=3, contact="054-3972962")
    exact = _extract("רחוב רגר 153", price=1300, avail=3, contact="054-3972962")
    storage.save_listing(PipelineResult(status=Status.NEEDS_DATA, dedup_key="k-vague",
                                        score=40, extract=vague))
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k-exact",
                                        score=80, extract=exact))
    assert storage.merge_duplicate_listings() == 1
    import sqlite3
    c = sqlite3.connect(temp_db)
    left = [r[0] for r in c.execute("SELECT dedup_key FROM listings")]
    assert left == ["k-exact"], "the NUMBERED row is the one that knows where the flat is"


def test_a_vague_read_is_kept_when_it_could_be_a_second_flat(temp_db):
    """Stricter than the address merge on purpose: with no address to corroborate, a
    shared phone alone would swallow a landlord's genuine second flat."""
    vague = _extract("ליד הבלוק", price=1300, avail=3, contact="054-3972962")
    other = _extract("רחוב רגר 153", price=1900, avail=2, contact="054-3972962")
    storage.save_listing(PipelineResult(status=Status.NEEDS_DATA, dedup_key="k-v",
                                        score=40, extract=vague))
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k-o", score=80,
                                        extract=other))
    assert storage.merge_duplicate_listings() == 0, "price and rooms differ — keep both"

    # ambiguity is refused too: two numbered flats both fit
    a = _extract("רגר 100", price=1300, avail=3, contact="054-3972962")
    b = _extract("רגר 200", price=1300, avail=3, contact="054-3972962")
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k-a", score=80,
                                        extract=a))
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k-b", score=80,
                                        extract=b))
    assert storage.merge_duplicate_listings() == 0, "two candidates — do not guess"


def test_rekey_leaves_bare_address_rows_alone(temp_db):
    """No house number means we still can't tell two flats apart, so those rows keep
    the phone-only key."""
    e = _ex("שכונה ב", "050-1234567")
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="phone:501234567",
                                        score=70, extract=e))
    assert storage.rekey_phone_listings() == 0


def test_backfill_first_seen_restores_the_real_discovery_date(temp_db):
    """replay --apply used to INSERT OR REPLACE, resetting first_seen to now on every
    run, so the whole table read as "found today" and LISTING_STALE_DAYS, the /top
    windows and the freshness factor all stopped meaning anything."""
    import sqlite3
    e = _ex("רגר 164", "050-1234567")
    key = storage.make_dedup_key(e)
    storage.record_post("sig-old", "טקסט", "", [], "g", None, e,
                        PipelineResult(status=Status.MATCH, dedup_key=key))
    with storage._conn() as c:                       # archive says it was found earlier
        c.execute("UPDATE posts SET first_seen='2026-07-18 09:00:00' WHERE sig='sig-old'")
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key=key, score=80,
                                        extract=e))

    assert storage.backfill_first_seen() == 1
    got = sqlite3.connect(temp_db).execute(
        "SELECT first_seen FROM listings WHERE dedup_key=?", (key,)).fetchone()[0]
    assert got == "2026-07-18 09:00:00"
    assert storage.backfill_first_seen() == 0        # idempotent


def test_backfill_never_moves_a_date_forward(temp_db):
    """Only ever backwards — that's what keeps a re-run a no-op and stops the repair
    making a listing look newer than it is."""
    import sqlite3
    e = _ex("קדש 3", "052-7654321")
    key = storage.make_dedup_key(e)
    storage.record_post("sig-new", "טקסט", "", [], "g", None, e,
                        PipelineResult(status=Status.MATCH, dedup_key=key))
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key=key, score=70,
                                        extract=e))
    with storage._conn() as c:                       # listing is ALREADY older
        c.execute("UPDATE listings SET first_seen='2020-01-01 00:00:00' WHERE dedup_key=?",
                  (key,))
    assert storage.backfill_first_seen() == 0
    assert sqlite3.connect(temp_db).execute(
        "SELECT first_seen FROM listings WHERE dedup_key=?", (key,)).fetchone()[0] \
        == "2020-01-01 00:00:00"


def test_backfill_ignores_listings_with_no_archived_post(temp_db):
    storage.save_listing(_res("orphan:1"))
    assert storage.backfill_first_seen() == 0


# --- hand-placed coordinates -------------------------------------------------------
def test_manual_location_round_trips_and_clears(temp_db):
    assert storage.manual_location("k1") is None
    storage.set_manual_location("k1", 31.2605, 34.7965)
    assert storage.manual_location("k1") == (31.2605, 34.7965)
    storage.set_manual_location("k1", 31.2610, 34.7970)          # replaces, not duplicates
    assert storage.manual_location("k1") == (31.2610, 34.7970)
    assert storage.all_manual_locations() == {"k1": (31.2610, 34.7970)}
    assert storage.clear_manual_location("k1") is True
    assert storage.manual_location("k1") is None
    assert storage.clear_manual_location("k1") is False          # already gone


def test_saving_a_listing_does_not_disturb_its_manual_location(temp_db):
    """save_listing rewrites the verdict on every re-read. The hand-placed point lives
    in its own table precisely so a later scrape can't quietly undo the correction."""
    from models import ListingExtract, PipelineResult, Status
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 5",
                       price_per_room_ils=1500, available_rooms_count=2)
    storage.set_manual_location("k1", 31.2605, 34.7965)
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k1", score=80,
                                        location_tier="GREEN", extract=e))
    assert storage.manual_location("k1") == (31.2605, 34.7965)


# --- posted_at: the FIRST publication, never a repost's -----------------------------

def _posted_at(sig):
    import sqlite3
    with sqlite3.connect(config.DB_PATH) as c:
        row = c.execute("SELECT posted_at FROM posts WHERE sig=?", (sig,)).fetchone()
    return row[0] if row else None


def test_a_repost_does_not_move_posted_at_forward(temp_db):
    """`sig` is a content signature, so a landlord reposting the same text lands on the
    same archive row. COALESCE(new, old) let the later publication win while first_seen
    correctly stayed at the first sighting — so the row read "seen before it was posted"
    and stats._detection_lag discarded it. That was 1,968 of 3,027 rows."""
    res = _res("phone:1")
    storage.record_post("sig-repost", "טקסט", "", [], "g", None, res.extract, res,
                        age_hours=5)          # published 5h before we saw it
    first = _posted_at("sig-repost")
    assert first is not None

    # seen again later, and Facebook now calls it 1h old — a repost of the same text
    storage.record_post("sig-repost", "טקסט", "", [], "g", None, res.extract, res,
                        age_hours=1)
    assert _posted_at("sig-repost") == first, "a repost overwrote the original time"


def test_an_unknown_age_never_erases_a_known_one(temp_db):
    """A later sighting that could not parse an age passes None; the row must keep what
    it had rather than fall back to nothing."""
    res = _res("phone:2")
    storage.record_post("sig-none", "טקסט", "", [], "g", None, res.extract, res,
                        age_hours=3)
    known = _posted_at("sig-none")
    storage.record_post("sig-none", "טקסט", "", [], "g", None, res.extract, res,
                        age_hours=None)
    assert _posted_at("sig-none") == known


def test_a_first_ever_age_is_recorded_even_if_the_row_exists(temp_db):
    """The row may have been archived before an age could be parsed. When one finally
    arrives it must be taken, not discarded by the MIN against NULL."""
    res = _res("phone:3")
    storage.record_post("sig-late", "טקסט", "", [], "g", None, res.extract, res,
                        age_hours=None)
    assert _posted_at("sig-late") is None
    storage.record_post("sig-late", "טקסט", "", [], "g", None, res.extract, res,
                        age_hours=2)
    assert _posted_at("sig-late") is not None


# --- pruning orphans: a dead KEY is not a dead FLAT ---------------------------------

def _row(key):
    """Is this dedup_key still in the listings table?"""
    import sqlite3
    with sqlite3.connect(config.DB_PATH) as c:
        return c.execute("SELECT 1 FROM listings WHERE dedup_key=?", (key,)).fetchone()


def _listing(key, addr, phone=None, score=80):
    e = ListingExtract(is_apartment_ad=True, price_per_room_ils=1500,
                       available_rooms_count=2, total_roommates_in_apt=3,
                       street_address_or_neighborhood=addr,
                       contact_phone_or_link=phone)
    return PipelineResult(status=Status.MATCH, dedup_key=key, location_tier="GREEN",
                          score=score, extract=e)


def _archive(sig, addr, phone=None):
    """Put a post in the archive so its parse contributes live keys."""
    res = _listing("k-" + sig, addr, phone)
    storage.record_post(sig, "טקסט", "", [], "g", None, res.extract, res)


def test_a_sole_orphan_row_is_never_pruned(temp_db):
    """The premise that a dead key means a dead flat is false — key formats changed,
    retention nulls parses, and a re-parse moves the key while the flat stays real.
    Measured on the live DB: orphan-alone would have deleted 21 rows, 11 of them real,
    including a MATCH scoring 83 with a phone on it."""
    _archive("sig-live", "רגר 1")                      # gives the archive some live keys
    storage.save_listing(_listing("phone:999|רוטנברג|13", "ברוטנברג 13", "054-2403990"))
    assert storage.prune_orphan_listings() == 0
    assert _row("phone:999|רוטנברג|13") is not None


def test_a_redundant_orphan_is_pruned(temp_db):
    """Both conditions required: key-orphaned AND another row still represents the
    flat, so dropping this one loses nothing."""
    e = ListingExtract(is_apartment_ad=True, available_rooms_count=2,
                       street_address_or_neighborhood="אלון 5",
                       contact_phone_or_link="055-1234")
    live_key = storage.dedup_keys(e)[0]
    res = PipelineResult(status=Status.MATCH, dedup_key=live_key,
                         location_tier="GREEN", score=80, extract=e)
    storage.record_post("sig-alon", "טקסט", "", [], "g", None, e, res)   # makes it live
    storage.save_listing(res)
    storage.save_listing(_listing("hash:deadbeefdeadbeef", "אלון 5"))    # the orphan
    assert storage.prune_orphan_listings() == 1
    assert _row("hash:deadbeefdeadbeef") is None
    assert _row(live_key) is not None                 # the flat survives


def test_the_last_row_of_a_flat_survives_even_if_every_key_is_dead(temp_db):
    """Two orphans for one flat is still a real flat. Pruning both would lose it, so
    the count is decremented as rows go and the final one is always kept."""
    _archive("sig-live", "רגר 1")
    storage.save_listing(_listing("hash:aaaaaaaaaaaaaaaa", "אלון 7"))
    storage.save_listing(_listing("hash:bbbbbbbbbbbbbbbb", "אלון 7"))
    assert storage.prune_orphan_listings() == 1
    left = [k for k in ("hash:aaaaaaaaaaaaaaaa", "hash:bbbbbbbbbbbbbbbb") if _row(k)]
    assert len(left) == 1, "the flat must not disappear entirely"


def test_an_alternative_key_is_not_an_orphan(temp_db):
    """A listing legitimately holds ANY key the parse yields. Comparing against
    `make_dedup_key` alone counted 2,942 live keys instead of 5,238 and invented 11
    false orphans by itself."""
    e = ListingExtract(is_apartment_ad=True, available_rooms_count=2,
                       street_address_or_neighborhood="אלון 5",
                       contact_phone_or_link="055-1234")
    keys = storage.dedup_keys(e)
    assert len(keys) > 1, "this test needs a parse that yields several keys"
    res = PipelineResult(status=Status.MATCH, dedup_key=keys[-1], location_tier="GREEN",
                         score=80, extract=e)
    storage.record_post("sig-alt", "טקסט", "", [], "g", None, e, res)
    storage.save_listing(res)                       # stored under a NON-primary key
    assert storage.prune_orphan_listings() == 0
    assert _row(keys[-1]) is not None


def test_an_empty_archive_never_wipes_listings(temp_db):
    storage.save_listing(_listing("phone:1|אלון 5", "אלון 5", "055-1"))
    storage.save_listing(_listing("phone:2|אלון 5", "אלון 5", "055-2"))
    assert storage.prune_orphan_listings() == 0     # nothing to compare against


# --- ONE ADDRESS IS NOT ONE FLAT, and a tower is not a duplicate ---------------------
#
# Measured 2026-08-12. `merge_duplicate_listings` had two ways left to delete a real
# listing, and the duplicate scan found both:
#   1. `הורקנוס 45` — one landlord's 3.5-room furnished flat at 1300/room, and a
#      phone-less 3-room-with-balcony at 1250. `_by_landlord` absorbed the contactless
#      row into the single phone cluster and `_collapse` ranks the phone-keyed row
#      richer, so it would have dropped a MATCH scoring 88.
#   2. `אלכסנדר ינאי 17` — 10 listings from 10 DIFFERENT posts across 6 groups, in a
#      building the posts call a מגדל with two lifts. Merging would destroy 9 real ads.

def _grp_row(key, addr, price, contact):
    """A `merge_duplicate_listings` row tuple: (key, address, price, avail, mates,
    contact, score)."""
    return (key, addr, price, 2, 3, contact, 60)


def test_a_contactless_row_that_contradicts_the_price_is_not_absorbed():
    """The `הורקנוס 45` case. A row with no phone joins the one landlord present only if
    it does not contradict them — two different per-room prices are two different flats."""
    grp = [_grp_row("phone:1|הורקנוס|45", "הורקנוס 45", 1300, "050-3011408"),
           _grp_row("hash:aaa", "הורקנוס 45 פינת סוסו הכהן", 1250, None)]
    subs = storage._by_landlord(grp)
    assert len(subs) == 2, "the contradicting row must stand alone, not be merged away"


def test_a_contactless_row_with_no_price_is_still_absorbed():
    """The `השלום 67` / `רגר 162` case, which are REAL duplicates: the thinner read simply
    failed to extract a price. A null must never count as a contradiction, or the merge
    stops doing the one job it exists for."""
    grp = [_grp_row("phone:1|השלום|67", "דרך השלום 67", 1400, "052-5791151"),
           _grp_row("hash:bbb", "השלום 67", None, None)]
    assert len(storage._by_landlord(grp)) == 1


def test_the_same_price_still_collapses():
    """`רגר 93`: same landlord, same price, two wordings — the duplicate to remove."""
    grp = [_grp_row("phone:1|שלמה המלך|93", "שדרות רגר 93 פינת שלמה המלך", 1225, "050-8220245"),
           _grp_row("phone:1|שדרות יצחק רגר|93", "רגר 93, הבלוק", 1225, "050-8220245")]
    assert len(storage._by_landlord(grp)) == 1


def test_address_post_count_counts_distinct_posts_not_listings(temp_db):
    """Per POST, not per listing: a flat reposted five times is one flat, and it is the
    number of separate ADVERTS that tells you a building holds many units."""
    for i in range(3):
        storage.record_post(f"s{i}", "טקסט", "", [], "g", f"http://p/{i}",
                            _ex("אלכסנדר ינאי 17", None),
                            PipelineResult(status=Status.DROP, dedup_key=f"k{i}"))
    # the same post seen twice must not count twice
    storage.record_post("s0", "טקסט", "", [], "g", "http://p/0",
                        _ex("אלכסנדר ינאי 17", None),
                        PipelineResult(status=Status.DROP, dedup_key="k0"))
    storage.invalidate_address_posts()
    assert storage.address_post_count("אלכסנדר ינאי 17") == 3
    assert storage.address_post_count("רחוב אלכסנדר ינאי 17") == 3   # same normalised addr
    assert storage.address_post_count("קדש 5") == 0
    assert storage.address_post_count("שכונה ב") == 0                # no house number


def test_a_multi_unit_address_is_never_merged(temp_db, monkeypatch):
    """The tower rule. THE THRESHOLD IS 15, NOT 4 like the broker rule beside it:
    measured over the archive, real duplicates sit at 2-6 distinct posts (`השלום 67` 2,
    `רגר 93` 4, `רגר 162` 6) while towers sit at 30-61. A threshold of 4 would have
    blocked every true duplicate this function exists to remove."""
    monkeypatch.setattr(config, "MULTI_UNIT_MIN_POSTS", 3)
    for i in range(3):
        storage.record_post(f"t{i}", "טקסט", "", [], "g", f"http://t/{i}",
                            _ex("אלכסנדר ינאי 17", None),
                            PipelineResult(status=Status.DROP, dedup_key=f"m{i}"))
    storage.invalidate_address_posts()
    assert storage.is_multi_unit("אלכסנדר ינאי 17") is True
    assert storage.is_multi_unit("רגר 93") is False


# --- the position is part of the verdict, so it is stored with it ----------------------
#
# `listings` had no lat/lon, so every map dot was recomputed through `geocode_cached` while
# its confidence badge came from the stored `geocode_source` (`dashboard.py:85` vs `:94`).
# Two sources of truth for one pin — which is exactly how 15 listings came to be drawn up
# to 626 m from where the pipeline placed them, under a badge reading `exact` (2026-08-12).

def _placed(key, lat, lon, src="osm_addr", score=80):
    return PipelineResult(
        status=Status.MATCH, dedup_key=key, location_tier="GREEN", score=score,
        lat=lat, lon=lon, geo_source=src,
        extract=ListingExtract(is_apartment_ad=True, price_per_room_ils=1500,
                               available_rooms_count=2,
                               street_address_or_neighborhood="רגר 93"))


def test_a_listing_stores_the_point_it_was_placed_at(temp_db):
    storage.save_listing(_placed("k1", 31.2613, 34.7975))
    with sqlite3.connect(config.DB_PATH) as c:
        lat, lon, src = c.execute(
            "SELECT lat, lon, geocode_source FROM listings WHERE dedup_key='k1'").fetchone()
    assert (round(lat, 4), round(lon, 4)) == (31.2613, 34.7975)
    assert src == "osm_addr"


def test_the_point_is_recomputed_not_enriched(temp_db):
    """`save_listing` ENRICHES nullable columns — a thinner later read must never blank a
    field. The position is NOT one of those: it is recomputed from the address every time,
    like status/tier/score/walk, and it must travel with the `geocode_source` written
    beside it. A stale coordinate next to a fresh source is the very disagreement the
    column exists to remove."""
    storage.save_listing(_placed("k2", 31.2613, 34.7975, "osm_addr"))
    storage.save_listing(_placed("k2", 31.2590, 34.7967, "static"))
    with sqlite3.connect(config.DB_PATH) as c:
        lat, src = c.execute(
            "SELECT lat, geocode_source FROM listings WHERE dedup_key='k2'").fetchone()
    assert round(lat, 4) == 31.2590 and src == "static", (lat, src)


def test_a_listing_with_no_position_stores_none(temp_db):
    """An unplaced listing must not acquire a coordinate — 9 of the live rows have no
    location at all, and inventing one for them is the failure the whole geocoding
    section exists to avoid."""
    storage.save_listing(_placed("k3", None, None, None))
    with sqlite3.connect(config.DB_PATH) as c:
        assert c.execute("SELECT lat FROM listings WHERE dedup_key='k3'").fetchone()[0] is None


# --- the two clocks must agree, or the latency metric is fiction -----------------------

def test_posted_at_and_first_seen_use_the_same_clock(temp_db):
    """`first_seen` is SQLite's CURRENT_TIMESTAMP, written in **UTC**. `posted_at` was
    computed from `datetime.now()` — **local**, UTC+3 here — so a publication time sat
    three hours ahead of the sighting it is compared against.

    Measured 2026-08-13: 4,218 rows read as published AFTER they were seen, with an
    overshoot of `3h - age` giving a median of exactly **120 min** and a p90 of **170 min**
    — the fingerprint of the offset, not of anything about Facebook. That is 39% of the
    archive discarded as impossible, and every surviving row understated the lag by 3h.

    So the lag of a post known to be N minutes old must come back as N minutes."""
    from datetime import datetime
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 1")
    res = PipelineResult(status=Status.DROP, dedup_key="k")
    storage.record_post("s-age", "t", "", [], "g", "u", e, res, age_hours=1.0)
    with sqlite3.connect(config.DB_PATH) as c:
        p, f = c.execute("SELECT posted_at, first_seen FROM posts WHERE sig='s-age'").fetchone()
    lag_min = (datetime.fromisoformat(f) - datetime.fromisoformat(p)).total_seconds() / 60
    assert 55 <= lag_min <= 65, f"a 1-hour-old post reported a {lag_min:.0f} min lag"
    assert p <= f, "published after it was seen — the clocks have drifted apart again"


def test_a_publication_after_the_first_sighting_is_never_stored(temp_db):
    """We demonstrably had the post at `first_seen`, so a later candidate is known-false.
    It arises because the age is often missing on the FIRST sighting (the hover budget:
    SCRAPER_MAX_HOVERS_PER_RUN=300 against 262-390 posts a run), so the first age we ever
    get can arrive on a later run.

    Kept NULL rather than clamped to `first_seen`: clamping would invent a lag of ~0 and
    bias the very metric this protects."""
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 1")
    res = PipelineResult(status=Status.DROP, dedup_key="k")
    storage.record_post("s-late", "t", "", [], "g", "u", e, res, age_hours=None)
    with sqlite3.connect(config.DB_PATH) as c:
        c.execute("UPDATE posts SET first_seen='2020-01-01 00:00:00' WHERE sig='s-late'")
    storage.record_post("s-late", "t", "", [], "g", "u", e, res, age_hours=0.5)
    with sqlite3.connect(config.DB_PATH) as c:
        p = c.execute("SELECT posted_at FROM posts WHERE sig='s-late'").fetchone()[0]
    assert p is None, f"stored an impossible publication time: {p}"


# --- a vote must outlive the listing it was cast on -----------------------------------

def _voted_listing(key, addr="רגר 1", user="u1", mark="saved"):
    """One saved listing with one vote on it, through the normal write paths."""
    e = ListingExtract(is_apartment_ad=True, price_per_room_ils=1500,
                       available_rooms_count=2, total_roommates_in_apt=3,
                       street_address_or_neighborhood=addr)
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key=key,
                                        location_tier="GREEN", score=80, extract=e))
    storage.set_mark(key, user, mark)


def test_deleting_a_listing_carries_its_vote_to_the_surviving_row(temp_db):
    """A VOTE IS THE SCARCEST DATA HERE — 5 against 623 listings, and MIN_ALERT_SCORE is
    blocked until there are ~20. `delete_listing` used to drop the row and leave the mark
    pointing at nothing; `effective_score` reads marks only for keys that still exist, so
    the signal vanished while the vote COUNT stayed the same."""
    import storage
    _voted_listing("phone:5551234", "רגר 1")
    _voted_listing("phone:5551234|רגר 1", "רגר 1", user="u2")   # same flat, keyed with address
    storage.delete_listing("phone:5551234")
    assert storage.orphaned_marks() == [], "the vote was orphaned instead of carried"
    assert storage.mark_adjustment("phone:5551234|רגר 1") != 0, "vote lost its effect"


def test_a_vote_with_nowhere_to_go_is_kept_and_reported(temp_db):
    """Reported, never cleaned up. Deleting it would hide the leak, and the row still
    records which flat the group rejected. A content-hash key carries nothing to match on,
    so it cannot be followed — which is exactly the case of the two real orphans."""
    import storage
    _voted_listing("hash:deadbeef", "רגר 2")
    storage.delete_listing("hash:deadbeef")
    orphans = storage.orphaned_marks()
    assert [r[0] for r in orphans] == ["hash:deadbeef"]


def test_pruning_never_destroys_a_vote(temp_db):
    """`prune_orphan_listings` used to run a bare `DELETE FROM marks`, destroying the vote
    to tidy up a row the same flat still occupies under another key."""
    import storage
    _voted_listing("phone:5559999", "מצדה 5")
    _voted_listing("phone:5559999|מצדה 5", "מצדה 5", user="u2")
    with storage._conn() as c:
        storage._rescue_marks(c, "phone:5559999")
        c.execute("DELETE FROM listings WHERE dedup_key=?", ("phone:5559999",))
    assert storage.orphaned_marks() == []
    assert storage.mark_adjustment("phone:5559999|מצדה 5") != 0


def test_the_newer_vote_wins_when_both_rows_were_voted_on(temp_db):
    """(dedup_key, user_id) is the primary key, so a re-point can collide. The surviving
    row's own vote is the current one — that is the only case where dropping a mark is
    right, and it mirrors what `_collapse` already does on a merge."""
    import storage
    _voted_listing("phone:5557777", "הרצל 3")
    _voted_listing("phone:5557777|הרצל 3", "הרצל 3", mark="dismissed")
    storage.delete_listing("phone:5557777")
    assert storage.orphaned_marks() == []
    with storage._conn() as c:
        marks = c.execute("SELECT mark FROM marks WHERE user_id='u1'").fetchall()
    assert [m[0] for m in marks] == ["dismissed"], "the surviving row's vote must stand"


def test_retiring_a_resolved_name_clears_it_from_the_pin_queue(temp_db):
    """`unknown_locations` is a WORK QUEUE, not a record — the pipeline re-logs a name the
    moment it fails to place again, which is how every row got there. Left unswept it read
    199 items of which 66 were real, and a list that is two-thirds dead work does not get
    worked: exactly one pin has ever been placed."""
    storage.record_unknown_location("רחוב שנפתר")
    storage.record_unknown_location("רחוב שעדיין לא")
    assert len(storage.unknown_locations(days=3650)) == 2
    assert storage.retire_unknown_locations(["רחוב שנפתר"]) == 1
    left = [r[0] for r in storage.unknown_locations(days=3650)]
    assert left == ["רחוב שעדיין לא"]


def test_retiring_nothing_is_a_no_op(temp_db):
    """The sweep runs on every `replay --apply`, including the ones where the queue is
    already clean."""
    storage.record_unknown_location("רחוב כלשהו")
    assert storage.retire_unknown_locations([]) == 0
    assert storage.retire_unknown_locations([None, ""]) == 0
    assert len(storage.unknown_locations(days=3650)) == 1
