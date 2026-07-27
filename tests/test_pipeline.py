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


def test_boundary_street_imprecise_is_red(monkeypatch):
    from models import ListingExtract
    # geocode returns an imprecise (street-name) GREEN point on a boundary street
    monkeypatch.setattr(pipeline.geocode, "geocode_detailed",
                        lambda a: ((31.262, 34.795), "overpass"))
    monkeypatch.setattr(pipeline.geocode, "is_boundary_street", lambda a: True)
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
