"""pipeline helpers — the שכונה ד' no-amber rule and the text fingerprint."""
import pipeline


def test_price_second_chance():
    assert pipeline._price_second_chance('חדר 1500 ש"ח לחודש') == 1500
    assert pipeline._price_second_chance("מחיר 1,500 שח") == 1500          # thousands sep
    assert pipeline._price_second_chance("מחיר לפרטים 0501234567") is None  # phone, not price
    assert pipeline._price_second_chance('שכ"ד 6000') is None               # total, out of range
    assert pipeline._price_second_chance("סתם טקסט בלי מחיר") is None


def test_normalize_entry_date():
    assert pipeline._normalize_entry_date("כניסה מיידית!") == "מיידי"
    assert pipeline._normalize_entry_date("1.9") == "1.9"
    assert pipeline._normalize_entry_date(None) is None


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


def test_no_amber_area_matches_dalet_only():
    assert pipeline._no_amber_area("שכונה ד'")
    assert pipeline._no_amber_area("רחוב הפלמ\"ח, שכונה ד")
    assert pipeline._no_amber_area("שכונת ד")
    # other neighborhoods keep their amber grace
    assert not pipeline._no_amber_area("שכונה ג")
    assert not pipeline._no_amber_area("שכונה ה'")
    assert not pipeline._no_amber_area("הבלוק")
    assert not pipeline._no_amber_area(None)
