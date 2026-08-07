"""pipeline helpers — the שכונה ד' no-amber rule and the text fingerprint."""
import pipeline
from models import ListingExtract


def test_price_second_chance():
    assert pipeline._price_second_chance('חדר 1500 ש"ח לחודש') == 1500
    assert pipeline._price_second_chance("מחיר 1,500 שח") == 1500          # thousands sep
    assert pipeline._price_second_chance("מחיר לפרטים 0501234567") is None  # phone, not price
    assert pipeline._price_second_chance('שכ"ד 6000') is None               # total, out of range
    assert pipeline._price_second_chance("סתם טקסט בלי מחיר") is None


def test_normalize_entry_date():
    assert pipeline._normalize_entry_date("כניסה מיידית!") == "מיידי"
    assert pipeline._normalize_entry_date("1.9") == "01.09"          # DD.MM, zero-padded
    assert pipeline._normalize_entry_date("01/10") == "01.10"
    assert pipeline._normalize_entry_date("15.8.26") == "15.08"      # year dropped
    assert pipeline._normalize_entry_date("ספטמבר") == "01.09"       # month only -> 1st
    assert pipeline._normalize_entry_date("15 בספטמבר") == "15.09"   # day kept
    assert pipeline._normalize_entry_date("גמיש") == "גמיש"
    assert pipeline._normalize_entry_date("1.9 או 1.10") == "01.09, 01.10"   # multiple
    assert pipeline._normalize_entry_date("1-9") == "01.09"          # hyphen separator
    assert pipeline._normalize_entry_date("כניסה מידית") == "מיידי"  # misspelled immediate
    assert pipeline._normalize_entry_date("2026-2027") == "2026-2027"  # year range, not a date
    assert pipeline._normalize_entry_date(None) is None


def test_normalize_phone():
    assert pipeline._normalize_phone("0501234567") == "050-1234567"
    assert pipeline._normalize_phone("050 123 4567") == "050-1234567"
    assert pipeline._normalize_phone("+972-50-1234567") == "050-1234567"
    assert pipeline._normalize_phone("צרו קשר 050-1234567 או 052-7654321") == \
        "050-1234567, 052-7654321"
    assert pipeline._normalize_phone("https://wa.me/972501234567") == "050-1234567"
    assert pipeline._normalize_phone("08-6412345") == "08-6412345"   # landline left as-is
    assert pipeline._normalize_phone("https://facebook.com/x") == "https://facebook.com/x"
    assert pipeline._normalize_phone(None) is None


def test_clean_address():
    assert pipeline._clean_address("רחוב הברושים etur habrisot") == "רחוב הברושים"
    assert pipeline._clean_address("רחוב Ben Gurion 5") == "רחוב Ben Gurion 5"  # numbered kept
    assert pipeline._clean_address("שכונה ג__") == "שכונה ג"
    assert pipeline._clean_address(None) is None


def test_strip_bidi_stabilizes_signature():
    clean = "דירת 3 שותפים בשכונה ג להשכרה 1500 שח"
    dirty = "דירת‏ 3 שותפים‫ בשכונה ג‎ להשכרה 1500 שח"
    assert pipeline._strip_bidi(dirty) == clean
    assert pipeline._text_sig(pipeline._strip_bidi(dirty)) == pipeline._text_sig(clean)
    assert pipeline._strip_bidi(None) is None


def test_process_post_dedups_phone_flip(temp_db, monkeypatch):
    """The live bug: the SAME numbered flat re-read with the phone extracted on only
    one read (and a different price) must be DROPped as already-seen on the second
    pass, not re-alerted. Text differs between reads so the text-signature dedup
    doesn't fire — this isolates the new numbered-address key."""
    calls = {"n": 0}

    def fake_extract(text, comments=None, images=None):
        calls["n"] += 1
        first = calls["n"] == 1
        return ListingExtract(is_apartment_ad=True,
                              street_address_or_neighborhood="רינגלבלום 1",
                              price_per_room_ils=1500 if first else 1400,
                              available_rooms_count=2, total_roommates_in_apt=3,
                              contact_phone_or_link="050-1234567" if first else None)

    monkeypatch.setattr(pipeline.llm, "extract", fake_extract)
    monkeypatch.setattr(pipeline.geocode, "geocode", lambda a: (31.25, 34.80))
    monkeypatch.setattr(pipeline.geocode, "is_bare_neighborhood", lambda a: False)
    monkeypatch.setattr(pipeline.osrm, "walk_to_nearest", lambda lat, lon: (5.0, "gate1"))
    monkeypatch.setattr(pipeline.zones, "classify_location", lambda lat, lon, walk_min=None: "GREEN")
    monkeypatch.setattr(pipeline.zones, "in_no_amber_zone", lambda lat, lon: False)
    monkeypatch.setattr(pipeline.notifier, "notify", lambda res: None)
    monkeypatch.setattr(pipeline.sheets, "save_listing", lambda res: None)

    text = "דירה להשכרה רינגלבלום 1 שני חדרים פנויים"
    r1 = pipeline.process_post(text, commit=True)
    r2 = pipeline.process_post(text + " עודכן", commit=True)   # different text sig
    assert r1.status.value == "MATCH"
    assert r2.status.value == "DROP" and "already seen" in r2.reason


def test_neighborhood_letter():
    assert pipeline._neighborhood_letter("שכונה ב") == "ב"
    assert pipeline._neighborhood_letter("בשכונה ג'") == "ג"
    assert pipeline._neighborhood_letter("שכונת ד, רחוב כלשהו") == "ד"
    assert pipeline._neighborhood_letter("שכונה ה'") == "ה"
    assert pipeline._neighborhood_letter("רינגלבלום 5") is None       # a plain street
    assert pipeline._neighborhood_letter("הבלוק") is None             # a named area
    assert pipeline._neighborhood_letter("שכונה ברושים") is None      # not a lone letter
    assert pipeline._neighborhood_letter(None) is None


def test_drop_neighborhood_outside_allowed_set():
    from models import ListingExtract
    # a post that NAMES שכונה ה (not ב/ג/ד) is dropped before geocoding (no network)
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="שכונה ה'",
                       available_rooms_count=2)
    res = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert res.status.value == "DROP" and "ב/ג/ד" in res.reason
    # ב/ג/ד are allowed (the letter parses, and it's in the allowed set)
    for letter in ("ב", "ג", "ד"):
        assert letter in pipeline.config.ALLOWED_NEIGHBORHOODS


def test_has_location_is_the_street_line():
    """The user's rule, 2026-08-03, expressed in the tiers geocode already computes:
    a street is an address, a neighbourhood on its own is not, an institution is not."""
    import geocode
    for src in ("osm_addr", "manual", "static", "interpolated", "overpass", "nominatim",
                "static_street", "landmark", "projected"):
        assert geocode.has_location(src), src
    for src in ("static_area", None, ""):
        assert not geocode.has_location(src), src
    # the tiers themselves, so a new source name cannot silently change the rule
    assert geocode.confidence("static_area") == "area"
    assert geocode.confidence(None) == "none"


def _placeless(rooms):
    """Cannot be placed, but is NOT a bare quarter and NOT a bearing off a landmark —
    so the SCORE gate is the only thing that can act on it, which is what these tests
    are about. It used to say `שכונה ב`; once a bare quarter became a drop at any score
    (2026-08-04) that fixture stopped exercising the score gate at all and three tests
    started passing/failing for the wrong reason.

    `אנדלה אמבלו` is a real street that OSM does not have, so it cannot be placed; the
    quarter stays in the string because the score depends on it (the street alone scores
    33, well under the gate, and would drop for the wrong reason).

    `rooms` straddles the threshold: 2 free rooms of 4 occupants scores 50, 3 of 4
    scores 52."""
    from models import ListingExtract
    return ListingExtract(is_apartment_ad=True,
                          street_address_or_neighborhood="אנדלה אמבלו, שכונה ב",
                          available_rooms_count=rooms, total_roommates_in_apt=4,
                          price_per_room_ils=1990)      # near the ceiling -> low score


def test_no_address_and_a_poor_score_is_dropped():
    """Kept-not-lost is right for a flat we merely cannot place, but one that names no
    street AND scores poorly is not a lead — it sits in the list forever unactionable."""
    res = pipeline._classify(_placeless(2), "", None, None, [], None, commit=False)
    assert res.status.value == "DROP"
    assert "no address" in res.reason
    # …and the same listing one point above the threshold is kept
    kept = pipeline._classify(_placeless(3), "", None, None, [], None, commit=False)
    assert kept.status.value == "NEEDS_DATA" and kept.score > 50


def test_a_named_street_is_never_dropped_for_being_vague():
    """"street is okay" — a numberless street keeps its listing however low it scores."""
    from models import ListingExtract
    e = ListingExtract(is_apartment_ad=True,
                       street_address_or_neighborhood="רחוב רינגלבלום",
                       available_rooms_count=2, total_roommates_in_apt=2,
                       price_per_room_ils=1990)
    res = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert res.status.value != "DROP", res.reason


def _bearing(text, rooms=3):
    """A bearing off a landmark. `rooms=3` of 4 scores 52 — ABOVE the score gate, so
    only the landmark rule can drop it and the test cannot pass by accident."""
    from models import ListingExtract
    return ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=text,
                          available_rooms_count=rooms, total_roommates_in_apt=4,
                          price_per_room_ils=1990)


def test_a_bearing_off_a_landmark_is_dropped_whatever_it_scores():
    """`באר שבע, קרוב לאוניברסיטת בן גוריון וסורוקה` scored 55 and so survived the score
    gate. "Near the university" is not an address at any score (user, 2026-08-03)."""
    for text in ("ליד האוניברסיטה", "מול שער האוניברסיטה", "אזור האוניברסיטה וסורוקה",
                 "באר שבע, קרוב לאוניברסיטת בן גוריון וסורוקה", "אוניברסיטת בן גוריון"):
        res = pipeline._classify(_bearing(text), "", None, None, [], None, commit=False)
        assert res.status.value == "DROP", f"{text} -> {res.status.value}"
        assert "bearing off a landmark" in res.reason, text
    # and it really is above the score gate, so the landmark rule is what dropped it
    import config
    assert pipeline._classify(_placeless(3), "", None, None, [], None,
                              commit=False).score > config.MIN_SCORE_WITHOUT_ADDRESS


def test_a_pinned_landmark_survives_the_bearing_rule():
    """THE PAIRING IS THE POINT. The text test alone answers True for `מגדלי דוד, סורוקה`
    — a real building the user pinned by hand — because the static table, not the text,
    is what tells the two apart. has_location() is that guard, and dropping it here
    would delete the landmark the user supplied coordinates for."""
    import geocode
    assert geocode.names_only_a_landmark("מגדלי דוד, סורוקה")     # text alone: ambiguous
    assert geocode.has_location(geocode.geocode_detailed("מגדלי דוד, סורוקה")[1])
    res = pipeline._classify(_bearing("מגדלי דוד, סורוקה", rooms=2), "", None, None, [],
                             None, commit=False)
    assert res.status.value != "DROP", res.reason


def test_a_bare_neighbourhood_is_not_a_bearing():
    """Narrower than the score rule on purpose: `שכונה ד` at 97 stays, as decided."""
    import geocode
    for text in ("שכונה ד", "שכונה ב", "הבלוק", "גוש עציון, שכונה ג"):
        assert not geocode.names_only_a_landmark(text), text


def test_the_threshold_is_the_raw_score_not_the_voted_one(temp_db):
    """Reproducibility: replay must give the same verdict whatever the group clicked.
    A ⭐ therefore cannot rescue a placeless flat — deliberate, the user's call."""
    import storage
    e = _placeless(2)
    first = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert first.status.value == "DROP"
    storage.set_mark(storage.make_dedup_key(e), "u1", "saved")
    again = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert again.status.value == "DROP", "a vote must not change the verdict"


def test_blacklisted_named_neighborhood_drops():
    from models import ListingExtract
    # a NAMED non-ב/ג/ד neighborhood is an instant hard-drop before geocoding
    for area in ("נאות לון", "הרובע", "רסקו", "העיר העתיקה"):
        e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=area,
                           available_rooms_count=2)
        res = pipeline._classify(e, "", None, None, [], None, commit=False)
        assert res.status.value == "DROP", area


def test_seeks_female_roommates():
    assert pipeline._seeks_female_roommates("מחפשות שותפה לדירה בשכונה ג")
    assert pipeline._seeks_female_roommates("דרושה שותפה, בנות בלבד")
    assert pipeline._seeks_female_roommates("מחפשים שותפה נחמדה")
    assert not pipeline._seeks_female_roommates("מחפשים שני שותפים לדירה")   # neutral/male
    assert not pipeline._seeks_female_roommates("דירת שותפים להשכרה")
    assert not pipeline._seeks_female_roommates(None)


def test_bare_neighborhood_is_needs_data():
    from models import ListingExtract
    # a bare neighborhood has no street — not a real address -> missing critical data
    assert pipeline._missing_critical(ListingExtract(
        is_apartment_ad=True, street_address_or_neighborhood="שכונה ד", available_rooms_count=2))
    assert pipeline._missing_critical(ListingExtract(
        is_apartment_ad=True, street_address_or_neighborhood="שכונה ג'", available_rooms_count=2))
    # a real street address is complete
    assert not pipeline._missing_critical(ListingExtract(
        is_apartment_ad=True, street_address_or_neighborhood="רגר 153", available_rooms_count=2))


def test_recover_house_number():
    # a numberless street + a "<street> <n>" in the post text -> number appended
    assert pipeline._recover_house_number(
        "רחוב אברהם אבינו", "דירה ברחוב אברהם אבינו 38 להשכרה") == "רחוב אברהם אבינו 38"
    # already numbered -> untouched; bare neighborhood -> untouched
    assert pipeline._recover_house_number("אברהם אבינו 5", "אברהם אבינו 5") == "אברהם אבינו 5"
    assert pipeline._recover_house_number("שכונה ג", "שכונה ג 12 משהו") == "שכונה ג"


def test_explain_traces_the_funnel(monkeypatch, temp_db):
    from models import ListingExtract
    # a post with no housing keyword is dropped before the LLM — and explain says so
    monkeypatch.setattr(pipeline.llm, "extract",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM!")))
    steps = pipeline.explain("חתול אבוד ברחוב")
    assert steps[0][0] == "keyword pre-filter" and steps[0][1] is False

    # a real ad walks the whole funnel and ends with the alert-bar comparison
    monkeypatch.setattr(pipeline.llm, "extract", lambda *a, **k: ListingExtract(
        is_apartment_ad=True, street_address_or_neighborhood="הבלוק",
        available_rooms_count=2, total_roommates_in_apt=3, price_per_room_ils=1500))
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed",
                        lambda a: ((31.259386, 34.796130), "static"))
    monkeypatch.setattr(pipeline.osrm, "walk_to_nearest", lambda lat, lon: (8.0, "gate"))
    steps = pipeline.explain("דירה להשכרה בהבלוק, 2 חדרים פנויים")
    names = [s[0] for s in steps]
    assert "geocode" in names and "zone tier" in names and "alert bar" in names
    assert any(n.startswith("verdict") for n in names)
    assert all(len(s) == 3 for s in steps)          # (step, ok, detail)


def test_recover_rooms():
    from models import ListingExtract

    def rooms(text, existing=None):
        e = ListingExtract(is_apartment_ad=True, available_rooms_count=existing)
        return pipeline._recover_rooms(e, text).available_rooms_count

    # whole apartment: Israeli "דירת N חדרים" = N-1 bedrooms + salon
    assert rooms("להשכרה דירת 3 חדרים ברחוב גמל 1") == 2
    assert rooms("להשכרה דירת 5 חדרים באכלוס ראשון") == 4
    assert rooms("להשכרה דירת 2.5 חדרים ברחוב משחררים") == 1
    # roommate-share phrasings are more specific and win
    assert rooms("מחפשים שותפה לדירה שלנו, דירת 4 חדרים") == 1
    assert rooms("מחפשים שני שותפים לדירת 4 חדרים") == 2
    assert rooms("מתפנים 2 חדרים בדירת 4 חדרים") == 2
    assert rooms("מפנה חדר בדירת 2 שותפים") == 1
    assert rooms("להשכרה יחידת דיור, חדר שינה וסלון") == 1
    # never overwrite what the LLM already extracted, and no text -> unchanged
    assert rooms("להשכרה דירת 3 חדרים", existing=1) == 1
    assert rooms("") is None


def test_walk_claim_conflict():
    # the post claims a short walk but routing says far -> suspect address match
    assert pipeline._walk_claim_conflict("כ 10-12 דקות הליכה לאוניברסיטה", 30.0)
    assert pipeline._walk_claim_conflict("5 דקות הליכה", 25.0)
    # agreement, small gaps, and missing data are all fine
    assert pipeline._walk_claim_conflict("10 דקות הליכה", 12.0) is None
    assert pipeline._walk_claim_conflict("10 דקות הליכה", None) is None
    assert pipeline._walk_claim_conflict("דירה נחמדה", 30.0) is None


def test_edge_uncertain_flags_needs_data(monkeypatch):
    from models import ListingExtract
    # a street-level (imprecise) point right at the zone boundary can't tell green from
    # red -> NEEDS_DATA rather than a confident verdict
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed",
                        lambda a: ((31.262, 34.795), "overpass"))
    monkeypatch.setattr(pipeline.osrm, "walk_to_nearest", lambda lat, lon: (6.0, "gate"))
    monkeypatch.setattr(pipeline.zones, "classify_location", lambda lat, lon, walk_min=None: "GREEN")
    monkeypatch.setattr(pipeline.zones, "in_no_amber_zone", lambda lat, lon: False)
    monkeypatch.setattr(pipeline.zones, "in_allowed_neighborhood", lambda lat, lon: True)
    monkeypatch.setattr(pipeline.zones, "neighborhood_of", lambda lat, lon: None)
    monkeypatch.setattr(pipeline.zones, "_dist_point_to_polygon_m", lambda lat, lon: 20.0)
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רחוב כלשהו 5",
                       available_rooms_count=2, total_roommates_in_apt=3)
    res = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert res.status.value == "NEEDS_DATA" and "גבול" in res.reason
    # an EXACT point at the same spot keeps its real tier (MATCH)
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed",
                        lambda a: ((31.262, 34.795), "interpolated"))
    assert pipeline._classify(e, "", None, None, [], None, commit=False).status.value == "MATCH"


def _green_world(monkeypatch, coords=(31.262, 34.800), source="interpolated"):
    """Stub everything spatial so a listing lands as a clean GREEN MATCH."""
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed", lambda a: (coords, source))
    monkeypatch.setattr(pipeline.osrm, "walk_to_nearest", lambda lat, lon: (6.0, "gate"))
    monkeypatch.setattr(pipeline.zones, "classify_location",
                        lambda lat, lon, walk_min=None: "GREEN")
    monkeypatch.setattr(pipeline.zones, "in_no_amber_zone", lambda lat, lon: False)
    monkeypatch.setattr(pipeline.zones, "in_allowed_neighborhood", lambda lat, lon: True)
    monkeypatch.setattr(pipeline.zones, "neighborhood_of", lambda lat, lon: None)


def test_amenities_are_attached_but_never_change_the_score(monkeypatch):
    """The whole contract of this feature: it decorates a result and nothing more.
    Same listing, same verdict, same score — with and without amenity data."""
    from models import ListingExtract
    _green_world(monkeypatch)
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 100",
                       available_rooms_count=2, total_roommates_in_apt=3,
                       price_per_room_ils=1400)

    monkeypatch.setattr(pipeline.amenities, "nearby", lambda lat, lon: {})
    plain = pipeline._classify(e, "", None, None, [], None, commit=False)

    rich_data = {"gym": {"label": "חדר כושר עזריאלי", "icon": "🏋️", "kind": "poi",
                         "options": [{"minutes": 14.0, "name": "עזריאלי"}]}}
    monkeypatch.setattr(pipeline.amenities, "nearby", lambda lat, lon: rich_data)
    rich = pipeline._classify(e, "", None, None, [], None, commit=False)

    assert plain.amenities == {} and rich.amenities == rich_data
    assert plain.score == rich.score                 # identical, not merely close
    assert plain.status == rich.status and plain.location_tier == rich.location_tier


def test_amenity_failure_cannot_break_a_run(monkeypatch):
    """A raising lookup would take down every listing in the run, so nearby() swallows
    its own errors — assert the pipeline still produces a normal result."""
    from models import ListingExtract
    _green_world(monkeypatch)
    monkeypatch.setattr(pipeline.amenities, "_load_data",
                        lambda: (_ for _ in ()).throw(RuntimeError("corrupt")))
    monkeypatch.setattr(pipeline.amenities, "_cache", {})
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 100",
                       available_rooms_count=2, total_roommates_in_apt=3)
    res = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert res.status.value == "MATCH" and res.amenities == {}


def test_dropped_listings_never_pay_for_amenity_routing(monkeypatch):
    """Amenities are computed after the RED/DROP exits, so a rejected listing costs
    no OSRM calls — the reason it sits below the score, not beside the geocode."""
    from models import ListingExtract
    called = []
    monkeypatch.setattr(pipeline.amenities, "nearby",
                        lambda lat, lon: called.append(1) or {})
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רמות",
                       available_rooms_count=2, total_roommates_in_apt=3)
    assert pipeline._classify(e, "", None, None, [], None, commit=False).status.value == "DROP"
    assert called == []


def test_neighborhood_conflict_flags_needs_data(monkeypatch):
    from models import ListingExtract
    # post says שכונה ג but the point lands in ד -> suspect, flag it
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed",
                        lambda a: ((31.262, 34.795), "interpolated"))
    monkeypatch.setattr(pipeline.osrm, "walk_to_nearest", lambda lat, lon: (6.0, "gate"))
    monkeypatch.setattr(pipeline.zones, "classify_location", lambda lat, lon, walk_min=None: "GREEN")
    monkeypatch.setattr(pipeline.zones, "in_no_amber_zone", lambda lat, lon: False)
    monkeypatch.setattr(pipeline.zones, "in_allowed_neighborhood", lambda lat, lon: True)
    monkeypatch.setattr(pipeline.zones, "neighborhood_of", lambda lat, lon: "ד")
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רחוב כלשהו 5, שכונה ג",
                       available_rooms_count=2, total_roommates_in_apt=3)
    res = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert res.status.value == "NEEDS_DATA" and "שכונה" in res.reason


def _boundary_setup(monkeypatch, frac):
    """An imprecise (street-level) GREEN point on a boundary street whose geometry is
    `frac` in-range."""
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed",
                        lambda a: ((31.262, 34.795), "overpass"))
    monkeypatch.setattr(pipeline.geocode, "is_boundary_street", lambda a: True)
    monkeypatch.setattr(pipeline, "_boundary_street_name", lambda a: "X")
    monkeypatch.setattr(pipeline.zones, "street_in_range_fraction", lambda s: frac)
    monkeypatch.setattr(pipeline.osrm, "walk_to_nearest", lambda lat, lon: (6.0, "gate"))
    monkeypatch.setattr(pipeline.zones, "classify_location", lambda lat, lon, walk_min=None: "GREEN")
    monkeypatch.setattr(pipeline.zones, "in_no_amber_zone", lambda lat, lon: False)
    monkeypatch.setattr(pipeline.zones, "in_allowed_neighborhood", lambda lat, lon: True)
    monkeypatch.setattr(pipeline.zones, "neighborhood_of", lambda lat, lon: None)
    monkeypatch.setattr(pipeline.zones, "_dist_point_to_polygon_m", lambda lat, lon: 500.0)
    from models import ListingExtract
    return ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רחוב כלשהו 5",
                          available_rooms_count=2, total_roommates_in_apt=3)


def test_boundary_street_judged_by_in_range_fraction(monkeypatch):
    # a street that is overwhelmingly IN RANGE (e.g. השלום, 98%) must NOT be dropped just
    # because the house can't be pinned — that was throwing away good apartments
    e = _boundary_setup(monkeypatch, 0.98)
    assert pipeline._classify(e, "", None, None, [], None, commit=False).status.value == "MATCH"
    # overwhelmingly RED (e.g. יהודה הלוי, 9%) -> confidently dropped
    e = _boundary_setup(monkeypatch, 0.09)
    assert pipeline._classify(e, "", None, None, [], None, commit=False).status.value == "DROP"
    # genuinely split (e.g. אברהם אבינו, 24%) -> NEEDS_DATA, surfaced not silently lost
    e = _boundary_setup(monkeypatch, 0.24)
    res = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert res.status.value == "NEEDS_DATA"
    # unknown geometry is treated as ambiguous too (never a confident drop)
    e = _boundary_setup(monkeypatch, None)
    assert pipeline._classify(e, "", None, None, [], None, commit=False).status.value == "NEEDS_DATA"


def test_boundary_street_imprecise_is_red(monkeypatch):
    from models import ListingExtract
    # an imprecise (street-name) point on a MOSTLY-RED boundary street is dropped
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed",
                        lambda a: ((31.262, 34.795), "overpass"))
    monkeypatch.setattr(pipeline.geocode, "is_boundary_street", lambda a: True)
    monkeypatch.setattr(pipeline, "_boundary_street_name", lambda a: "X")
    monkeypatch.setattr(pipeline.zones, "street_in_range_fraction", lambda s: 0.05)
    monkeypatch.setattr(pipeline.osrm, "walk_to_nearest", lambda lat, lon: (5.0, "gate"))
    monkeypatch.setattr(pipeline.zones, "classify_location", lambda lat, lon, walk_min=None: "GREEN")
    monkeypatch.setattr(pipeline.zones, "in_no_amber_zone", lambda lat, lon: False)
    monkeypatch.setattr(pipeline.zones, "in_allowed_neighborhood", lambda lat, lon: True)
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="אברהם אבינו 38",
                       available_rooms_count=2, total_roommates_in_apt=3)
    res = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert res.status.value == "DROP" and "גבול האזור" in res.reason
    # a PRECISE placement on the same street keeps its GREEN tier (MATCH)
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed",
                        lambda a: ((31.262, 34.795), "osm_addr"))
    res2 = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert res2.status.value == "MATCH"


def test_no_amber_area_matches_dalet_only():
    assert pipeline._no_amber_area("שכונה ד'")
    assert pipeline._no_amber_area("רחוב הפלמ\"ח, שכונה ד")
    assert pipeline._no_amber_area("שכונת ד")
    # other neighborhoods keep their amber grace
    assert not pipeline._no_amber_area("שכונה ג")
    assert not pipeline._no_amber_area("שכונה ה'")
    assert not pipeline._no_amber_area("הבלוק")
    assert not pipeline._no_amber_area(None)


# --- whole-apartment price -> per-room -------------------------------------------
def _px(price, text):
    """Run the price recovery over one extract and return (price, was_derived)."""
    from models import ListingExtract
    e = ListingExtract(is_apartment_ad=True, price_per_room_ils=price)
    e = pipeline._recover_price_per_room(e, text)
    return e.price_per_room_ils, e.price_is_derived


def test_whole_flat_total_becomes_a_per_room_price():
    """Measured on the archive: 144 of 407 price-drops were whole-flat ads whose TOTAL
    rent was stored per-room and hard-dropped. Israeli usage: דירת N חדרים = N-1
    bedrooms, the same convention _recover_rooms already uses."""
    assert _px(2400, 'להשכרה דירת 3 חדרים ברחוב הכנסת 16, 2400 ש"ח') == (1200, True)
    assert _px(3600, "להשכרה דירת 4 חדרים, 3600") == (1200, True)
    # fractional room counts truncate, exactly as _recover_rooms does: דירת 2.5 חדרים
    # is 2 rooms -> 1 bedroom, so 2800 stays over the cap and the drop stands. (Such a
    # flat also fails MIN_AVAILABLE_ROOMS anyway.)
    assert _px(2800, "להשכרה דירת 2.5 חדרים ברחוב המשחררים, 2800") == (2800, False)


def test_a_per_person_price_is_never_divided():
    """'2400 לשותף' already IS the per-room figure — dividing it would invent a
    listing that doesn't exist."""
    for marker in ("לשותף", "לכל שותף", "לאדם", "לנפש", "לחדר"):
        assert _px(2400, f"דירת 3 חדרים, 2400 {marker}") == (2400, False)


def test_a_price_already_under_the_cap_is_left_alone():
    """The rule only rescues listings that were about to be dropped, so it can never
    turn a correct price into a wrong one."""
    assert _px(1500, "דירת 3 חדרים, 1500") == (1500, False)


def test_still_too_expensive_after_dividing_stays_dropped():
    # 9000 / 2 = 4500, way past the cap -> leave it, the drop was right
    assert _px(9000, "דירת 3 חדרים, 9000") == (9000, False)


def test_no_whole_flat_phrasing_means_no_division():
    assert _px(2400, "חדר להשכרה בדירת שותפים, 2400") == (2400, False)
    assert _px(2400, None) == (2400, False)


def test_one_room_flat_never_divides_by_zero():
    assert _px(2400, "דירת 1 חדרים, 2400") == (2400, False)


def test_derived_price_is_penalised_like_an_uncertain_one():
    import fit
    labels = [lbl for lbl, _ in fit.breakdown(1200, 8.0, "GREEN", price_is_derived=True)]
    assert any("מחושב" in lbl for lbl in labels)
    plain = fit.score(1200, 8.0, "GREEN", 2, 2)
    derived = fit.score(1200, 8.0, "GREEN", 2, 2, price_is_derived=True)
    assert derived < plain            # an inference, not a quote


def test_the_whole_rule_runs_inside_postprocess():
    """It must sit in _postprocess_extract so replay applies it to the archive with no
    LLM calls — that's how the 144 get re-tested offline."""
    from models import ListingExtract
    e = ListingExtract(is_apartment_ad=True, price_per_room_ils=2400)
    e = pipeline._postprocess_extract(e, 'להשכרה דירת 3 חדרים, 2400 ש"ח', "")
    assert e.price_per_room_ils == 1200 and e.price_is_derived is True


# --- hand-drawn zone edge --------------------------------------------------------
def _edge_world(monkeypatch, dist_m, source="interpolated", addr="רגר 100"):
    """A point classified AMBER that sits `dist_m` outside the green polygon."""
    from models import ListingExtract
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed", lambda a: ((31.262, 34.80), source))
    monkeypatch.setattr(pipeline.osrm, "walk_to_nearest", lambda lat, lon: (7.0, "gate"))
    monkeypatch.setattr(pipeline.zones, "classify_location",
                        lambda lat, lon, walk_min=None: "AMBER")
    monkeypatch.setattr(pipeline.zones, "in_no_amber_zone", lambda lat, lon: False)
    monkeypatch.setattr(pipeline.zones, "in_allowed_neighborhood", lambda lat, lon: True)
    monkeypatch.setattr(pipeline.zones, "neighborhood_of", lambda lat, lon: None)
    monkeypatch.setattr(pipeline.zones, "_dist_point_to_polygon_m", lambda lat, lon: dist_m)
    monkeypatch.setattr(pipeline.amenities, "nearby", lambda lat, lon: {})
    return ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=addr,
                          available_rooms_count=2, total_roommates_in_apt=3,
                          price_per_room_ils=1400)


def test_a_precise_point_just_outside_the_hand_drawn_edge_counts_as_green():
    """green_zone.json is traced by hand, so 5 m outside the line is not a real
    verdict — 35 of 239 placed listings sit within 50 m of it."""
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        e = _edge_world(mp, dist_m=35)
        res = pipeline._classify(e, "", None, None, [], None, commit=False)
        assert res.location_tier == "GREEN" and res.preferred is True
        assert "hand-drawn" in res.reason
    finally:
        mp.undo()


def test_the_grace_does_not_reach_genuinely_distant_flats():
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        e = _edge_world(mp, dist_m=200)
        assert pipeline._classify(e, "", None, None, [], None, commit=False).location_tier == "AMBER"
    finally:
        mp.undo()


def test_an_imprecise_point_near_the_edge_gets_no_grace():
    """An imprecise placement near the line is the EXISTING NEEDS_DATA case — its own
    error already swamps the polygon's, so it must not be promoted."""
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        e = _edge_world(mp, dist_m=35, source="overpass")
        res = pipeline._classify(e, "", None, None, [], None, commit=False)
        assert res.location_tier != "GREEN"
    finally:
        mp.undo()


def test_a_bare_neighborhood_is_never_graced_into_green():
    """Its 'precise' static point is a whole-area centroid, so the grace must not
    undo the bare-neighborhood cap."""
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        e = _edge_world(mp, dist_m=10, source="static", addr="שכונה ג")
        assert pipeline._classify(e, "", None, None, [], None, commit=False).location_tier != "GREEN"
    finally:
        mp.undo()


def test_a_no_amber_area_is_never_graced_into_green():
    """שכונה ד' outside the polygon is RED by an explicit rule; the grace runs after
    it and must not resurrect it."""
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        e = _edge_world(mp, dist_m=10, addr="שכונה ד, רחוב האיסיים 5")
        mp.setattr(pipeline.zones, "in_no_amber_zone", lambda lat, lon: True)
        assert pipeline._classify(e, "", None, None, [], None, commit=False).location_tier == "RED"
    finally:
        mp.undo()


# --- a location corrected by hand is authoritative --------------------------------
def test_a_hand_placed_listing_beats_the_geocoder(temp_db, monkeypatch):
    """The whole point of the dashboard's place-mode. _classify is also what
    replay.py re-runs, so if the override were consulted anywhere else instead, every
    re-apply would silently drag the dot back to the geocoder's guess."""
    import geocode
    import storage
    e = ListingExtract(is_apartment_ad=True,
                       street_address_or_neighborhood="אוניברסיטת בן גוריון",
                       price_per_room_ils=1500, available_rooms_count=2,
                       contact_phone_or_link="050-1234567")
    key = storage.make_dedup_key(e)
    monkeypatch.setattr(geocode, "geocode_detailed",
                        lambda t: ((31.2631, 34.8022), "cache"))   # the campus
    plain = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert (round(plain.lat, 4), plain.geo_source) == (31.2631, "cache")

    storage.set_manual_location(key, 31.2605, 34.7965)             # a real street
    fixed = pipeline._classify(e, "", None, None, [], None, commit=False)
    assert (round(fixed.lat, 4), round(fixed.lon, 4)) == (31.2605, 34.7965)
    assert fixed.geo_source == "manual"
    # and the geography that follows from it is recomputed, not carried over
    assert fixed.walk_minutes != plain.walk_minutes

    storage.clear_manual_location(key)
    assert round(pipeline._classify(e, "", None, None, [], None,
                                    commit=False).lat, 4) == 31.2631


def test_a_bare_quarter_is_dropped_whatever_it_scores():
    """User, 2026-08-04: "keep them only if in a known location like הבלוק". שכונה ד is
    2,375 m across — its centroid is a dot in the middle of thousands of flats. 14
    listings sat on those, one scoring 97, all kept by the score gate."""
    import config
    for text in ("שכונה ד", "שכונה ג", "שכונה ב", "שכונה ג'"):
        res = pipeline._classify(_bearing(text), "", None, None, [], None, commit=False)
        assert res.status.value == "DROP", f"{text} -> {res.status.value}"
        assert "only a neighbourhood" in res.reason, text
    # …and it really is above the score gate, so THIS rule is what dropped it
    assert pipeline._classify(_placeless(3), "", None, None, [], None,
                              commit=False).score > config.MIN_SCORE_WITHOUT_ADDRESS


def test_a_street_survives_even_when_we_cannot_place_it():
    """"A street is okay" is the user's wording. `אנדלה אמבלו` is missing from OSM, so it
    still lands on the שכונה ד centroid — but failing to geocode a street is OUR
    limitation, not the post's, and the flat is not a bare quarter."""
    import geocode
    for text in ("אנדלה אמבלו, שכונה ד", "אלעזר בן יאיר שכונה ד", "גוש עציון, שכונה ג"):
        assert not geocode.names_only_a_neighbourhood(text), text
    res = pipeline._classify(_bearing("אנדלה אמבלו, שכונה ד"), "", None, None, [], None,
                             commit=False)
    assert res.status.value != "DROP", res.reason


def test_a_known_location_is_not_a_bare_quarter():
    """The 'keep' half of the same rule — הבלוק is surveyed at 123 m across."""
    import geocode
    assert not geocode.names_only_a_neighbourhood("הבלוק")
    assert geocode.has_location(geocode.geocode_detailed("הבלוק")[1])


def test_a_close_spelling_variant_still_names_its_street():
    """`יוסף בן מתתיהו, שכונה ד` (score 84) is ONE LETTER from OSM's `יוסף בן מתיתיהו`.
    The gate wanted a match both non-fuzzy AND verbatim, and `_candidate_tokens` offers
    the corrected spelling (exact, not verbatim) and the written one (verbatim, fuzzy) —
    each failed a different half, so a street we know was called no street at all."""
    import geocode
    assert geocode.geocode_detailed("יוסף בן מתתיהו, שכונה ד")[1] != "static_area"
    assert not geocode.names_only_a_neighbourhood("יוסף בן מתתיהו, שכונה ד")


def test_the_street_gate_still_refuses_a_coincidence():
    """`האוני` fuzzy-matches `הגאונים` at 0.833 — below the 0.90 bar. Both halves of the
    guard matter: `_candidate_tokens` ALSO emits the corrected `הגאונים`, which resolves
    `exact`, so dropping the verbatim test lets it through the other way."""
    import geocode
    assert geocode._names_a_street("האוני", geocode._normalize("ליד האוני")) is False
    assert geocode._names_a_street("הגאונים", geocode._normalize("ליד האוני")) is False
    assert geocode.geocode_detailed("גר בשכונה ג ליד האוני")[1] == "static_area"


# --- ...and when the TEXT cannot answer, use what the model extracted --------------

def _px_counts(price, text, mates=None, avail=None):
    """Price recovery where the divisor may have to come from the extract itself."""
    from models import ListingExtract
    e = ListingExtract(is_apartment_ad=True, price_per_room_ils=price,
                       total_roommates_in_apt=mates, available_rooms_count=avail)
    e = pipeline._recover_price_per_room(e, text)
    return e.price_per_room_ils, e.price_is_derived


def test_an_image_post_still_gets_its_total_divided():
    """The rule required `דירת N חדרים` in raw_text, so it missed every IMAGE post — the
    ad is a picture, raw_text is the anti-scrape junk, and the LLM read the flat from the
    image. Measured 2026-08-07: 125 archived posts dropped for price alone where
    price/residents lands under the cap, most with no readable text at all."""
    junk = "Zohar Renee Golan e͏ p͏ o͏ s͏ S͏ t͏ r͏ 1͏ m͏ 5͏ 4͏ a͏ 9͏"
    assert _px_counts(3000, junk, mates=3) == (1000, True)
    assert _px_counts(3450, junk, mates=4) == (862, True)
    # available_rooms_count is the fallback when the model gave no resident count
    assert _px_counts(2800, junk, avail=2) == (1400, True)


def test_the_extracted_divisor_never_overrides_a_per_person_price():
    """A per-person phrasing means the number really IS per room. That guard must hold
    whichever divisor is used."""
    text = "מחפשים שותף, 2600 לשותף בדירת 3 חדרים"
    assert _px_counts(2600, text, mates=3) == (2600, False)


def test_no_divisor_means_no_division():
    junk = "Ohad Isak n͏ e͏ t͏ S͏ s͏ p͏ o͏ o͏ d͏ r͏ 4͏ 7͏ 1͏ 8͏"
    assert _px_counts(2800, junk) == (2800, False)          # nothing to divide by
    assert _px_counts(2800, junk, mates=1) == (2800, False)  # one resident is not a share


def test_a_flat_that_is_still_too_expensive_stays_dropped():
    junk = "Tomer Katan e͏ r͏ n͏ s͏ o͏ t͏ d͏ S͏ p͏ o͏"
    assert _px_counts(9000, junk, mates=2) == (9000, False)  # 4500/room — the drop stands


def test_the_text_rule_still_wins_when_the_text_does_say_it():
    """N-1 bedrooms from `דירת N חדרים` is the established convention and must not be
    replaced by the resident count when the text is readable."""
    assert _px_counts(2400, 'להשכרה דירת 3 חדרים, 2400 ש"ח', mates=4) == (1200, True)
