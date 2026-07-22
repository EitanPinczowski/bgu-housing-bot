"""geocode — static table, Google fallback, bounds guard, and caching.
Google/Nominatim HTTP is mocked; no network is touched."""
import geocode


class _Resp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


def _gmap(lat, lon):
    return {"results": [{"geometry": {"location": {"lat": lat, "lng": lon}}}]}


def _fresh(monkeypatch, tmp_path):
    """Isolate the module cache and force Google on with a fake key. Overpass is off
    here so the Google/Nominatim tests below don't reach it; its own tests enable it."""
    monkeypatch.setattr(geocode, "_cache", {})
    monkeypatch.setattr(geocode, "_CACHE_PATH", tmp_path / "geo.json")
    monkeypatch.setattr(geocode.config, "USE_GOOGLE_GEOCODE", True)
    monkeypatch.setattr(geocode.config, "USE_OVERPASS_FALLBACK", False)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake-key")


def test_static_table_wins_without_network(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # if Google were called it'd blow up (no real net) — static must short-circuit
    monkeypatch.setattr(geocode, "_google", lambda t: (_ for _ in ()).throw(AssertionError("net!")))
    assert geocode.geocode("גר בשכונה ג ליד האוני") == geocode.STATIC_TABLE["שכונה ג"]


def test_google_result_inside_box_is_used_and_cached(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    calls = {"n": 0}
    import requests
    def fake_get(url, **kw):
        calls["n"] += 1
        return _Resp(_gmap(31.255, 34.79))          # a point inside Be'er Sheva
    monkeypatch.setattr(requests, "get", fake_get)
    # a name NOT in the static table, so it actually reaches (mocked) Google
    q = "כתובת בדיקה ייחודית 999"
    assert geocode.geocode(q) == (31.255, 34.79)
    # second lookup is served from cache — no extra HTTP call
    assert geocode.geocode(q) == (31.255, 34.79)
    assert calls["n"] == 1


def test_google_result_outside_box_is_rejected(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(geocode.config, "USE_NOMINATIM_FALLBACK", False)
    import requests
    monkeypatch.setattr(requests, "get", lambda url, **kw: _Resp(_gmap(32.08, 34.78)))  # Tel Aviv
    assert geocode.geocode("רחוב שלא קיים כאן") is None


def test_bare_neighborhood_detection():
    # bare neighborhood -> capped to amber; an accurate street address is not
    assert geocode.is_bare_neighborhood("שכונה ג")
    assert geocode.is_bare_neighborhood("שכונה ג'")
    assert geocode.is_bare_neighborhood("הנדיב, שכונה ג")        # no רחוב / number
    assert not geocode.is_bare_neighborhood('רחוב הכ"ג 5, שכונה ג')  # house number
    assert not geocode.is_bare_neighborhood("רחוב הנדיב, שכונה ג")   # street word
    assert not geocode.is_bare_neighborhood("הבלוק")             # not a שכונה
    assert not geocode.is_bare_neighborhood(None)
    assert geocode.is_precise_address("רחוב הנדיב") and geocode.is_precise_address("הנדיב 5")
    assert not geocode.is_precise_address("שכונה ג")


def test_disabled_without_key(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    assert geocode._google_enabled() is False


# --- #2: hardened static-table match --------------------------------------------
def test_static_forward_match_still_works():
    # the table key appears inside a longer post text (the common, safe direction)
    assert geocode.geocode("גר ברינגלבלום ליד האוני'") == geocode.STATIC_TABLE["רינגלבלום"]


def test_static_reverse_match_needs_length(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)                       # Google+Overpass mocked/off
    # a stray 1-char location must NOT map onto a whole-neighborhood centroid…
    monkeypatch.setattr(geocode.config, "USE_NOMINATIM_FALLBACK", False)
    monkeypatch.setattr(geocode, "_google", lambda t: None)
    assert geocode.geocode("ג") is None
    # …but a long-enough fragment of a key still resolves ("בלוק" ⊂ "הבלוק")
    assert geocode.geocode("בלוק") == geocode.STATIC_TABLE["הבלוק"]


# --- #1: Overpass fallback tier -------------------------------------------------
def _overpass_on(monkeypatch, tmp_path):
    monkeypatch.setattr(geocode, "_cache", {})
    monkeypatch.setattr(geocode, "_CACHE_PATH", tmp_path / "geo.json")
    monkeypatch.setattr(geocode.config, "USE_GOOGLE_GEOCODE", False)
    monkeypatch.setattr(geocode.config, "USE_OVERPASS_FALLBACK", True)
    monkeypatch.setattr(geocode.config, "USE_NOMINATIM_FALLBACK", False)
    monkeypatch.setattr(geocode.time, "sleep", lambda *a: None)   # no polite delay in tests


def test_overpass_node_used_and_cached(monkeypatch, tmp_path):
    _overpass_on(monkeypatch, tmp_path)
    calls = {"n": 0}
    import requests
    def fake_post(url, **kw):
        calls["n"] += 1
        return _Resp({"elements": [{"type": "node", "lat": 31.256, "lon": 34.798}]})
    monkeypatch.setattr(requests, "post", fake_post)
    q = "רחוב שדרים ייחודי 123"                          # not in the static table
    assert geocode.geocode(q) == (31.256, 34.798)
    assert geocode.geocode(q) == (31.256, 34.798)       # second call served from cache
    assert calls["n"] == 1


def test_overpass_way_center_and_box_guard(monkeypatch, tmp_path):
    _overpass_on(monkeypatch, tmp_path)
    import requests
    # a way carries a computed `center`; a first hit outside the BS box is skipped
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp({"elements": [
        {"type": "way", "center": {"lat": 32.08, "lon": 34.78}},   # Tel Aviv — rejected
        {"type": "way", "center": {"lat": 31.257, "lon": 34.80}},  # BS — used
    ]}))
    assert geocode.geocode("כתובת מרחוב כלשהו") == (31.257, 34.80)


def test_overpass_skipped_when_disabled(monkeypatch, tmp_path):
    _overpass_on(monkeypatch, tmp_path)
    monkeypatch.setattr(geocode.config, "USE_OVERPASS_FALLBACK", False)
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("overpass!")))
    assert geocode.geocode("רחוב שאינו מוכר 77") is None


def test_overpass_name_strips_number_and_street_word():
    assert geocode._overpass_name("רחוב רינגלבלום 5") == "רינגלבלום"
    assert geocode._overpass_name('שד\' יצחק רגר 90') == "יצחק רגר"


def test_overpass_pick_prefers_exact_highway():
    els = [
        {"type": "node", "lat": 31.25, "lon": 34.79, "tags": {"name": "רגר", "shop": "kiosk"}},
        {"type": "way", "center": {"lat": 31.264, "lon": 34.792},
         "tags": {"name": "רגר", "highway": "primary"}},
    ]
    # the actual street (highway) wins over a same-named shop node; source = street-level
    assert geocode._overpass_pick(els, "רגר") == ((31.264, 34.792), "overpass")


def test_overpass_pick_prefers_precise_address_node():
    els = [
        {"type": "way", "center": {"lat": 31.264, "lon": 34.792},
         "tags": {"name": "אברהם אבינו", "highway": "residential"}},
        {"type": "node", "lat": 31.270, "lon": 34.798,
         "tags": {"addr:street": "אברהם אבינו", "addr:housenumber": "38"}},
    ]
    # the exact house node wins and is labelled precise
    assert geocode._overpass_pick(els, "אברהם אבינו", "38") == ((31.270, 34.798), "osm_addr")


def test_bare_street_and_precise_source():
    assert geocode.is_bare_street("אברהם אבינו") is True
    assert geocode.is_bare_street("רחוב הנדיב") is True
    assert geocode.is_bare_street("אברהם אבינו 60") is False   # has a number
    assert geocode.is_bare_street("שכונה ג") is False          # bare neighborhood
    assert geocode.is_precise_source("static") and geocode.is_precise_source("osm_addr")
    assert not geocode.is_precise_source("overpass")           # street-level = imprecise


def test_overpass_name_hardening():
    assert geocode._overpass_name("רחבת רד״ק 13/6, באר שבע").startswith("רד")
    assert geocode._house_number("אברהם אבינו 38") == "38"
    assert geocode._house_number("רחבת רד״ק 13/6") == "13"     # compound -> first
    assert geocode._house_number("רחוב קדש") is None


# --- #1: negative-result cache with a TTL ---------------------------------------
def test_negative_result_cached_with_ttl(monkeypatch, tmp_path):
    _overpass_on(monkeypatch, tmp_path)
    import requests
    calls = {"n": 0}
    def empty_post(url, **kw):
        calls["n"] += 1
        return _Resp({"elements": []})                      # a real "not found"
    monkeypatch.setattr(requests, "post", empty_post)
    q = "רחוב שלא נמצא בכלל 12345"
    assert geocode.geocode(q) is None
    assert geocode.geocode(q) is None                       # served from the negative cache
    assert calls["n"] == 1                                  # not re-queried within TTL
    from datetime import datetime, timedelta
    geocode._cache[geocode._normalize(q)] = {"m": (datetime.now() - timedelta(days=8)).isoformat()}
    assert geocode.geocode(q) is None
    assert calls["n"] == 2                                  # expired miss -> re-queried


def test_transient_overpass_failure_is_not_cached(monkeypatch, tmp_path):
    _overpass_on(monkeypatch, tmp_path)
    import requests
    calls = {"n": 0}
    def boom(url, **kw):
        calls["n"] += 1
        raise requests.exceptions.ReadTimeout("down")       # every mirror times out
    monkeypatch.setattr(requests, "post", boom)
    q = "רחוב שהשרת נפל עליו 42"
    assert geocode.geocode(q) is None
    assert geocode.geocode(q) is None
    # a network blackout is NOT a real miss -> re-queried every time (all mirrors each call)
    assert calls["n"] == 2 * len(geocode.config.OVERPASS_URLS)
    assert geocode._normalize(q) not in geocode._cache


# --- #3: geocode_detailed reports which tier resolved the name ------------------
def test_geocode_detailed_reports_source(monkeypatch, tmp_path):
    _overpass_on(monkeypatch, tmp_path)
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(
        {"elements": [{"type": "node", "lat": 31.257, "lon": 34.80}]}))
    assert geocode.geocode_detailed("גר בשכונה ג")[1] == "static"     # static table
    assert geocode.geocode_detailed("רחוב חדש כלשהו 5")[1] == "overpass"


# --- #11: uncache a bad pin / stale miss -----------------------------------------
def test_uncache_removes_matching_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(geocode, "_cache", {
        "גר ברינגלבלום 5": {"c": [31.26, 34.79], "s": "overpass"},
        "רחוב אחר": {"c": [31.25, 34.80], "s": "nominatim"},
    })
    monkeypatch.setattr(geocode, "_CACHE_PATH", tmp_path / "geo.json")
    assert geocode.uncache("רינגלבלום") == ["גר ברינגלבלום 5"]
    assert "גר ברינגלבלום 5" not in geocode._cache
    assert "רחוב אחר" in geocode._cache      # untouched
    assert geocode.uncache("") == []
