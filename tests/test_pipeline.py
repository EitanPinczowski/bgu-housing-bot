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


def test_no_amber_area_matches_dalet_only():
    assert pipeline._no_amber_area("שכונה ד'")
    assert pipeline._no_amber_area("רחוב הפלמ\"ח, שכונה ד")
    assert pipeline._no_amber_area("שכונת ד")
    # other neighborhoods keep their amber grace
    assert not pipeline._no_amber_area("שכונה ג")
    assert not pipeline._no_amber_area("שכונה ה'")
    assert not pipeline._no_amber_area("הבלוק")
    assert not pipeline._no_amber_area(None)
