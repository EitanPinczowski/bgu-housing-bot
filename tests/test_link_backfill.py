"""link_backfill matcher/query — the offline core of the live link recovery
(the Facebook navigation itself isn't unit-tested)."""
import link_backfill as lb
import pipeline


def test_match_by_text_signature():
    txt = "דירה להשכרה שלושה שותפים ברגר 1 מיידי 0501234567"
    sig = pipeline._text_sig(pipeline._strip_bidi(txt))
    assert lb._match(txt, sig, "") is True
    assert lb._match("טקסט אחר לגמרי", sig, "") is False


def test_match_by_phone_in_text():
    assert lb._match("מחפשים שותף, לפרטים 050-123-4567", None, "501234567") is True
    assert lb._match("אין כאן טלפון תואם", None, "509999999") is False


def test_query_prefers_phone_then_address():
    assert lb._query("רגר 1", "050-123-4567") == "0501234567"
    assert lb._query("שדרות רגר 164", None) == "שדרות רגר 164"
    assert lb._query('רחוב הכ״ג 5', None) == "רחוב הכג 5"       # quotes stripped


def test_search_url_from_group():
    u = lb._search_url("https://www.facebook.com/groups/12345", "רגר")
    assert u.startswith("https://www.facebook.com/groups/12345/search/?q=")
    assert lb._search_url("https://example.com/notagroup", "x") is None
