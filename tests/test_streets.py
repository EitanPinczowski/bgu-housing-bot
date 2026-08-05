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


def test_a_leading_road_type_word_does_not_hide_a_short_street():
    """`_pool_key` has always ignored `רחוב`/`שדרות`/…; the QUERY side did not, so a post
    spelling the street out in full reached only the word-run tier — which needs a
    4-character run. `בזל` is 3, so `רחוב בזל` resolved to NOTHING while `בזל` alone was
    exact, and `רחוב רמב"ם` / `רחוב בעלי התוספות` worked and hid it. 194 single-word
    street names here are 4 characters or fewer."""
    assert streets.canonical("בזל") == ("בזל", "exact")
    name, how = streets.canonical("רחוב בזל")
    assert name == "בזל" and how != "fuzzy", (name, how)
    assert streets.canonical("שדרות בזל")[0] == "בזל"
    for full, bare in (("רחוב רמב\"ם", "רמב\"ם"), ("רחוב בעלי התוספות", "בעלי התוספות"),
                       ("רחוב הרצל", "הרצל")):
        assert streets.canonical(full)[0] == bare, full


def test_dropping_a_road_type_word_must_not_cross_to_another_street():
    """`_index` also holds the single-letter prefix ALIASES, so a plain `road in idx`
    answered `כיכר אבות` with `אבות` -> `האבות`, a street **2,506 m** away — the exact
    error tier 3b's own-name test exists to stop. It fired the moment this tier was
    added. `כיכר אבות` must reach `כיכר האבות`, and never the far one."""
    assert streets.canonical("כיכר אבות")[0] == "כיכר האבות"
    assert streets.canonical("כיכר האבות") == ("כיכר האבות", "exact")
    assert streets.canonical("האבות") == ("האבות", "exact")
    # roads whose type word is part of the real OSM name still answer as themselves
    assert streets.canonical("דרך מצדה")[1] == "exact"
    assert streets.canonical("רחוב") == (None, None)
