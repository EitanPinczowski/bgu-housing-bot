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
