"""streets.canonical — resolve a messy Hebrew street token to the real OSM name.
Uses the shipped area_features.json index (offline, no network)."""
import streets


def test_exact_and_prefix():
    assert streets.canonical("האיסיים") == ("האיסיים", "exact")
    assert streets.canonical("יד ושם")[0] == "יד ושם"
    # a colloquial ה prefix ("הסנהדרין") resolves to OSM's "סנהדרין"
    assert streets.canonical("הסנהדרין") == ("סנהדרין", "prefix")


def test_word_match_for_partial_name():
    # posts write "רגר"; OSM calls it "שדרות יצחק רגר" — a UNIQUE word match is safe
    name, how = streets.canonical("רגר")
    assert name == "שדרות יצחק רגר" and how == "word"
    assert streets.canonical("שיפר")[0] == "יצחק שיפר"


def test_fuzzy_fixes_real_misspellings():
    assert streets.canonical("רינגנבלום")[0] == "רינגלבלום"
    assert streets.canonical("רינגבלום")[0] == "רינגלבלום"
    assert streets.canonical("אלעזר בין יאיר")[0] == "אלעזר בן יאיר"


def test_prefix_beats_fuzzy_safety_trap():
    """THE trap: 'ברגר' is 'ב'+'רגר' (in Reger), but it fuzzy-matches 'ברנר' — a real but
    DIFFERENT street. The prefix/word tiers must win so we never pick ברנר."""
    name, how = streets.canonical("ברגר")
    assert name == "שדרות יצחק רגר"
    assert name != "ברנר" and how != "fuzzy"


def test_unknown_returns_none_not_a_bad_guess():
    assert streets.canonical("זזזזזזזז") == (None, None)
    assert streets.canonical("א") == (None, None)      # too short to be safe
    assert streets.canonical(None) == (None, None)


def test_geometry_available_for_interpolation():
    segs = streets.geometry("שדרות יצחק רגר")
    assert segs and all(len(p) == 2 for seg in segs for p in seg)
    assert streets.geometry("לא קיים") == []
