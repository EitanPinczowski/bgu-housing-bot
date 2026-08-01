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


def test_static_prefers_first_mentioned_location():
    # a multi-cue address must resolve to the EARLIEST-mentioned static key, so a
    # trailing slang POI ("…כיכר האבות, הבלוק") can't hijack the real anchor.
    coords, src = geocode.geocode_detailed("רחוב אברהם אבינו, על כיכר האבות, הבלוק")
    assert src == "static"
    assert coords == geocode.STATIC_TABLE["כיכר האבות"]     # not הבלוק (which comes later)


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
    assert geocode._overpass_name("רחוב האיסיים 5, שכונה ד") == "האיסיים"   # nbhd+num+comma stripped
    assert geocode._overpass_name("שכונה ג', רחוב זאב זבוטינסקי 48") == "זאב זבוטינסקי"
    assert geocode._house_number("אברהם אבינו 38") == "38"
    assert geocode._house_number("רחבת רד״ק 13/6") == "13"     # compound -> first
    assert geocode._house_number("רחוב קדש") is None


def test_candidate_tokens_strips_city_and_splits(monkeypatch):
    # the PROVEN failure: 'רגר 179' resolved but 'רחוב רגר 179, באר שבע' did not
    cands = geocode._candidate_tokens("רחוב רגר 179, באר שבע")
    assert "שדרות יצחק רגר" in cands            # canonical street, city stripped
    assert not any("באר" in c for c in cands)
    # an intersection is split into its parts, not glued into one token
    assert geocode._candidate_tokens("יוחנן הורקנוס/יטבתה")[:1] == ["יוחנן הורקנוס"]
    # a comma-joined pair yields both streets
    c2 = geocode._candidate_tokens("שיפר, רינגבלום")
    assert "יצחק שיפר" in c2 and "רינגלבלום" in c2
    # a ב prefix resolves to the right street (never the look-alike ברנר)
    assert geocode._candidate_tokens("ברגר 155")[0] == "שדרות יצחק רגר"


def test_interpolate_house_between_anchors(monkeypatch):
    # a synthetic N-S street with known houses 1 and 11 -> #6 lands in the middle
    line = [[31.260 + i * 0.0002, 34.800] for i in range(11)]
    monkeypatch.setattr(geocode.streets, "geometry", lambda s: [line])
    monkeypatch.setattr(geocode, "_anchors",
                        {"X": {"1": [31.260, 34.800], "11": [31.262, 34.800]}})
    mid = geocode.interpolate_house("X", "6")
    assert mid is not None and 31.2605 <= mid[0] <= 31.2615      # between the anchors
    # monotonic: a lower number sits nearer the low anchor
    assert geocode.interpolate_house("X", "3")[0] < mid[0]
    # NEVER extrapolate past the known range, and refuse with <2 anchors
    assert geocode.interpolate_house("X", "99") is None
    monkeypatch.setattr(geocode, "_anchors", {"X": {"1": [31.260, 34.800]}})
    assert geocode.interpolate_house("X", "2") is None


def _user_anchor_file(monkeypatch, tmp_path):
    monkeypatch.setattr(geocode, "_USER_ANCHORS_PATH", tmp_path / "user_anchors.json")
    monkeypatch.setattr(geocode, "_anchors", None)
    monkeypatch.setattr(geocode, "_median_gap", None)


def test_a_pin_becomes_an_anchor_and_places_the_rest_of_the_street(monkeypatch, tmp_path):
    """The point of Part 4: a 📍 on ONE numbered flat has to place the others too.
    Without it the 18 streets OSM has no addresses for can never be fixed at all."""
    line = [[31.260 + i * 0.0002, 34.800] for i in range(11)]
    monkeypatch.setattr(geocode.streets, "geometry", lambda s: [line])
    monkeypatch.setattr(geocode, "_ANCHORS_PATH", tmp_path / "house_anchors.json")
    _user_anchor_file(monkeypatch, tmp_path)
    _no_buildings(monkeypatch)
    assert geocode.interpolate_house("X", "6") is None          # nothing known yet

    assert geocode.add_anchor("X", "1", 31.2600, 34.800) is True
    assert geocode.add_anchor("X", "11", 31.2620, 34.800) is True
    mid = geocode.interpolate_house("X", "5")
    assert mid is not None and 31.2600 < mid[0] < 31.2620       # a DIFFERENT flat


def test_a_user_anchor_beats_osm_and_survives_a_pbf_rebuild(monkeypatch, tmp_path):
    """User anchors live in their own file so load_osm_addresses.py cannot wipe them,
    and they win: a person looked at the map, OSM did not."""
    (tmp_path / "house_anchors.json").write_text(
        '{"X": {"1": [31.2000, 34.8000]}}', encoding="utf-8")
    monkeypatch.setattr(geocode.streets, "geometry",
                        lambda s: [[[31.2600, 34.800], [31.2620, 34.800]]])
    monkeypatch.setattr(geocode, "_ANCHORS_PATH", tmp_path / "house_anchors.json")
    _user_anchor_file(monkeypatch, tmp_path)
    assert geocode.add_anchor("X", "1", 31.2601, 34.800) is True
    assert geocode._load_anchors()["X"]["1"] == [31.2601, 34.8]


def test_a_mistap_far_from_the_street_is_refused_as_an_anchor(monkeypatch, tmp_path):
    """One bad anchor moves every address on the street, so the same 200 m rule that
    guards OSM's own data guards a hand-placed point. The listing's own manual location
    is stored separately and is unaffected."""
    monkeypatch.setattr(geocode.streets, "geometry",
                        lambda s: [[[31.2600, 34.800], [31.2620, 34.800]]])
    monkeypatch.setattr(geocode, "_ANCHORS_PATH", tmp_path / "house_anchors.json")
    _user_anchor_file(monkeypatch, tmp_path)
    assert geocode.add_anchor("X", "7", 31.2900, 34.8400) is False   # ~4 km away
    assert not (tmp_path / "user_anchors.json").exists()
    # and nothing that isn't a house number ever becomes an anchor
    assert geocode.add_anchor("X", "ב", 31.2610, 34.800) is False


def test_snap_moves_a_point_onto_a_building_but_only_a_near_one(monkeypatch):
    monkeypatch.setattr(geocode, "_buildings",
                        {"cell": 0.002, "cells": {"15630:17400": [[31.26011, 34.8000]]}})
    near = geocode.snap_to_building((31.2600, 34.8000))
    assert near == (31.26011, 34.8000)                       # ~12 m away: snapped
    far = geocode.snap_to_building((31.2610, 34.8000))
    assert far == (31.2610, 34.8000)                         # ~110 m away: left alone


def test_a_street_type_word_that_is_part_of_the_name_is_kept(monkeypatch):
    """`דרך`/`שדרות`/`סמטת` are usually noise, but sometimes they ARE the name, and
    stripping them then names a different REAL street: `דרך מצדה 69` was placed on
    `מצדה`, 585 m away. The full name is tried first — but only on an exact index
    match, so a fuzzy hit can't invent `רחוב רגר`."""
    toks = geocode._candidate_tokens("דרך מצדה 69")
    assert toks[0] == "דרך מצדה" and "מצדה" in toks
    # the ordinary case is untouched: the type word is still stripped
    assert geocode._candidate_tokens("רחוב רינגלבלום 5")[0] == "רינגלבלום"
    assert geocode._candidate_tokens("רחוב רגר 179, באר שבע")[0] == "שדרות יצחק רגר"


def test_our_own_placement_is_held_to_the_off_street_rule(monkeypatch):
    """The 250 m gate used to apply only to Overpass/Nominatim, so a fall-through to a
    similarly-named street looked fully confident. Rejecting sends the listing on to
    the next tier (and ultimately to NEEDS_DATA), where a human sees it."""
    seen = []
    monkeypatch.setattr(geocode, "_plausible_external",
                        lambda text, pt, src: seen.append((text, src)) or False)
    monkeypatch.setattr(geocode, "place_house", lambda st, hn: ((31.0, 34.0), "interpolated"))
    monkeypatch.setattr(geocode, "_overpass", lambda t: (None, None, True))
    monkeypatch.setattr(geocode.config, "USE_NOMINATIM_FALLBACK", False)
    monkeypatch.setattr(geocode, "_cache", {})
    got, src = geocode.geocode_detailed("אלכסנדר ינאי 32")
    assert seen and seen[0][1] == "interpolated"      # the internal hit WAS checked
    assert got is None                                # and refused, not returned


def test_dead_mirror_is_skipped_after_first_failure(monkeypatch, tmp_path):
    """A dead Overpass mirror must cost its timeout ONCE, not on every lookup — that
    stall was making a single address take minutes."""
    _overpass_on(monkeypatch, tmp_path)
    monkeypatch.setattr(geocode, "_dead_mirrors", set())
    monkeypatch.setattr(geocode.config, "OVERPASS_URLS",
                        ["https://dead.example/api", "https://live.example/api"])
    tried = []
    import requests

    def fake_post(url, **kw):
        tried.append(url)
        if "dead" in url:
            raise requests.exceptions.ReadTimeout("down")
        return _Resp({"elements": [{"type": "node", "lat": 31.26, "lon": 34.80}]})

    monkeypatch.setattr(requests, "post", fake_post)
    assert geocode._overpass_query("רגר", None)[0] == (31.26, 34.80)
    assert tried == ["https://dead.example/api", "https://live.example/api"]
    # second lookup skips the known-dead mirror entirely
    tried.clear()
    assert geocode._overpass_query("רגר", None)[0] == (31.26, 34.80)
    assert tried == ["https://live.example/api"]


def test_confidence_tiers():
    assert geocode.confidence("static") == "exact"
    assert geocode.confidence("osm_addr") == "exact"
    assert geocode.confidence("interpolated") == "high"
    assert geocode.confidence("overpass") == "street"
    assert geocode.confidence(None) == "none"
    # an interpolated point is precise enough to keep its real tier
    assert geocode.is_precise_source("interpolated")
    assert not geocode.is_precise_source("overpass")


def test_cache_version_invalidates_stale_miss(monkeypatch, tmp_path):
    monkeypatch.setattr(geocode, "_CACHE_PATH", tmp_path / "geo.json")
    # a miss recorded by OLDER logic must be retried, not honoured
    monkeypatch.setattr(geocode, "_cache", {"x": {"m": "2099-01-01T00:00:00", "v": 1}})
    monkeypatch.setattr(geocode, "GEOCODE_LOGIC_VERSION", 2)
    assert geocode._cache_lookup("x")[0] == "none"          # retry
    # a miss from the CURRENT version, still fresh, is honoured
    from datetime import datetime
    monkeypatch.setattr(geocode, "_cache",
                        {"x": {"m": datetime.now().isoformat(), "v": 2}})
    assert geocode._cache_lookup("x")[0] == "miss"


def test_precise_street_skips_neighborhood_centroid(monkeypatch, tmp_path):
    # a real street that ALSO names a שכונה must geocode the STREET (overpass), not the
    # neighborhood's static centroid — else every ד/ג/ב street reads the green centroid.
    _overpass_on(monkeypatch, tmp_path)
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(
        {"elements": [{"type": "node", "lat": 31.268, "lon": 34.792}]}))
    coords, src = geocode.geocode_detailed("רחוב האיסיים 5, שכונה ד")
    # The invariant is that the NEIGHBOURHOOD centroid never wins — not which tier does.
    # Since the govmap seed, האיסיים has enough anchors to place number 5 locally, so this
    # now answers `interpolated` without touching the network, which is strictly better
    # than the `overpass` it used to need. Assert the rule, not the route.
    assert src != "static_area"
    assert coords != geocode.STATIC_TABLE["שכונה ד"]
    assert geocode.confidence(src) in ("exact", "high")          # a point, not an area
    # a BARE neighborhood still uses the static centroid (bare path unchanged), but it
    # is labelled as the AREA it is — see test_a_neighborhood_centroid_is_graded_area
    assert geocode.geocode_detailed("שכונה ד")[1] == "static_area"


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
    assert geocode.geocode_detailed("גר בשכונה ג")[1] == "static_area"   # static table
    assert geocode.geocode_detailed("רחוב חדש כלשהו 5")[1] == "overpass"


# --- #11: uncache a bad pin / stale miss -----------------------------------------
def test_add_pin_resolves(monkeypatch, tmp_path):
    monkeypatch.setattr(geocode, "_USER_PINS_PATH", tmp_path / "pins.json")
    monkeypatch.setattr(geocode, "_user_pins", None)
    monkeypatch.setattr(geocode.config, "USE_OVERPASS_FALLBACK", False)
    monkeypatch.setattr(geocode.config, "USE_NOMINATIM_FALLBACK", False)
    geocode.add_pin("רחבת שלמה המלך", 31.255, 34.805)      # a place not in the static table
    # it now resolves (merged into the static match, forward substring)
    assert geocode.geocode("להשכרה ברחבת שלמה המלך") == (31.255, 34.805)


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


# --- placement accuracy ----------------------------------------------------------
def test_house_numbers_are_not_swallowed_by_a_static_street_entry():
    """The וינגייט bug: a STATIC_TABLE STREET entry answered every house number with
    one coordinate, so interpolate_house never ran and each flat landed on the same
    spot (which was itself 520 m off the street)."""
    import geocode
    a = geocode.geocode_detailed("וינגייט 74")
    b = geocode.geocode_detailed("וינגייט 16")
    assert a[0] and b[0]
    assert a[0] != b[0], "different house numbers must not share one point"
    assert a[1] != "static" and b[1] != "static"


def test_a_bare_street_still_uses_the_static_point():
    """Only NUMBERED addresses bypass the entry — don't over-correct and lose the
    placement for a street named with no number."""
    import geocode
    coords, src = geocode.geocode_detailed("וינגייט")
    assert coords and src == "static"


def test_a_bare_neighborhood_still_resolves():
    import geocode
    coords, src = geocode.geocode_detailed("שכונה ג")
    assert coords and src == "static_area"


def test_a_neighborhood_centroid_is_graded_area_not_exact():
    """19 listings whose post said only `שכונה ד` sat on ONE point drawn as solid,
    precise dots — the biggest pile on the map, and a lie. An area centroid is an area.
    `הבלוק` is the same thing without the word שכונה: a whole student quarter."""
    import geocode
    assert geocode.confidence("static_area") == "area"
    assert geocode.is_precise_source("static_area") is False
    assert geocode._static_source("שכונה ד") == "static_area"
    assert geocode._static_source("הבלוק") == "static_area"
    # a real place keeps its precision
    assert geocode._static_source("כיכר האבות") == "static"
    assert geocode.confidence("static") == "exact"


def test_every_static_entry_sits_on_the_street_it_names():
    """The permanent guard for the whole class. Median offset across stored listings
    is ~3 m, so 150 m is a blunder threshold, not a tolerance."""
    import audit_geocode
    bad = audit_geocode.audit_static()
    assert bad == [], f"static points off their own street: {bad}"


def test_street_fallback_is_reported_as_imprecise():
    """When a numbered address can't be resolved precisely we fall back to the skipped
    street point — but it must NOT count as precise, or the boundary rules would trust
    a street-level guess near the zone edge."""
    import geocode
    assert not geocode.is_precise_source("static_street")
    assert geocode.confidence("static_street") == "street"


# --- recall: Hebrew abbreviation marks, word prefixes, descriptive locations -------
def test_hebrew_abbreviation_marks_are_folded_to_ascii():
    """The proven bug: `הכ״ג 5` (gershayim U+05F4) resolved to nothing while `הכ"ג 5`
    resolved fine, because the external geocoders were handed the RAW address. Israeli
    street names are full of these (רד״ק, רמב״ם, הכ״ג, שד״ל)."""
    import geocode
    assert geocode._fold_quotes("הכ״ג 5") == 'הכ"ג 5'
    assert geocode._fold_quotes("רח׳ רד״ק") == "רח' רד\"ק"
    assert geocode._fold_quotes("“x”") == '"x"'
    assert geocode._fold_quotes(None) == ""
    # and the tokenizer reaches the same street either way
    import streets
    for form in ("הכ״ג 5", 'הכ"ג 5'):
        toks = geocode._candidate_tokens(geocode._fold_quotes(form))
        assert any(streets.canonical(t)[0] for t in toks), form


def test_word_prefixes_are_stripped_from_street_names():
    import geocode
    import streets
    for addr, street in (("רחבת הרב עוזיאל", "עוזיאל"), ("סמטת יונתן", "יונתן"),
                         ("משעול הדס", "הדס")):
        toks = geocode._candidate_tokens(addr)
        assert not any(t.startswith(("רחבת", "סמטת", "משעול")) for t in toks), toks
    assert streets.canonical("גמל")[0] == "גמל"        # sanity: the index works


def test_a_described_position_near_a_landmark_resolves():
    """'ליד האוניברסיטה וסורוקה' names no street but is unambiguously campus-adjacent;
    it used to come back UNKNOWN and get dropped."""
    import geocode
    for text in ("ליד האוניברסיטה וסורוקה", "קרוב לאוניברסיטת בן גוריון",
                 "מול שער האוניברסיטה", "בסמוך לסורוקה"):
        assert geocode._descriptive_landmark(text) is not None, text


def test_a_landmark_mentioned_in_passing_never_hijacks_a_real_address(monkeypatch):
    """This is why the landmark tier runs LAST instead of being a static key: as a
    static entry 'האוניברסיטה' would capture any address that merely mentions it."""
    import geocode
    # a real street resolves normally -> the landmark tier is never consulted
    monkeypatch.setattr(geocode, "_cache_lookup", lambda n: ("none", None, None))
    monkeypatch.setattr(geocode, "_overpass", lambda t: ((31.2437, 34.7936), "overpass", True))
    monkeypatch.setattr(geocode, "_save_cache", lambda: None)
    coords, src = geocode.geocode_detailed("רגר 5, 5 דקות מהאוניברסיטה")
    assert coords and src == "overpass"
    # only when every real tier fails does the description answer
    monkeypatch.setattr(geocode, "_overpass", lambda t: (None, None, True))
    monkeypatch.setattr(geocode, "_nominatim", lambda t: None)
    coords, src = geocode.geocode_detailed("ליד האוניברסיטה וסורוקה")
    assert src == "landmark"


def test_landmark_needs_both_a_proximity_word_and_a_landmark():
    import geocode
    assert geocode._descriptive_landmark("אוניברסיטת בן גוריון") is None   # no bearing
    assert geocode._descriptive_landmark("ליד הסופר") is None              # no landmark
    assert geocode._descriptive_landmark(None) is None


def test_landmark_points_are_treated_as_imprecise():
    """A landmark point describes a neighbourhood-sized area, so the boundary rules
    must stay cautious about it."""
    import geocode
    assert not geocode.is_precise_source("landmark")


def test_a_hand_placed_point_outranks_every_geocoder():
    """'manual' means a person looked at the map and said "the flat is here", which is
    strictly better evidence than any automatic tier — so it must not be treated as a
    low-confidence source and capped from GREEN to AMBER by the precision rules."""
    assert geocode.confidence("manual") == "exact"
    assert geocode.is_precise_source("manual") is True
    assert geocode.confidence(None) == "none"
    assert geocode.confidence("overpass") == "street"


# --- projecting past / around the anchors ------------------------------------------
def _no_buildings(monkeypatch):
    """Turn the building snap off. These tests are about the ARITHMETIC; leaving the
    real buildings.json in play would make them depend on whether a Be'er Sheva shed
    happens to sit near a synthetic test coordinate."""
    monkeypatch.setattr(geocode, "_buildings", {"cell": 0.002, "cells": {}})


def _anchored(monkeypatch, anchors, pts):
    monkeypatch.setattr(geocode, "_anchors", anchors)
    monkeypatch.setattr(geocode, "_street_axis", lambda st: (pts, 0))
    monkeypatch.setattr(geocode, "_median_gap", 10.0)
    _no_buildings(monkeypatch)


def test_a_number_past_the_last_anchor_is_projected_not_abandoned(monkeypatch):
    """Refusing entirely sent the listing to the street centroid, which measured up to
    3.5 km out via the external fallbacks. A bounded projection is real evidence."""
    pts = [(31.2600 + i * 0.0002, 34.79) for i in range(20)]
    _anchored(monkeypatch, {"X": {"2": [31.2600, 34.79], "10": [31.2608, 34.79]}}, pts)
    pt, how = geocode.place_house("X", "6")
    assert how == "interpolated"                       # in range: unchanged
    pt, how = geocode.place_house("X", "14")
    assert how == "extrapolated" and pt is not None    # just past: projected
    assert pt[0] > 31.2608                             # …in the right direction


def test_extrapolation_is_bounded(monkeypatch):
    """Past the cap the numbering assumption stops being evidence and we say so."""
    pts = [(31.2600 + i * 0.0002, 34.79) for i in range(200)]
    _anchored(monkeypatch, {"X": {"2": [31.2600, 34.79], "10": [31.2608, 34.79]}}, pts)
    assert geocode.place_house("X", "9999")[0] is None


def test_a_single_anchor_street_is_usable(monkeypatch):
    """24 streets have exactly one anchor. With the city's typical spacing that still
    beats the centroid, and it stays bounded — but it is labelled 'projected', because
    one anchor fixes where a number is and not which way the numbers run."""
    pts = [(31.2600 + i * 0.0002, 34.79) for i in range(20)]
    _anchored(monkeypatch, {"X": {"10": [31.2604, 34.79]}}, pts)
    pt, how = geocode.place_house("X", "16")
    assert how == "projected" and pt is not None
    assert geocode.place_house("X", "900")[0] is None   # still capped


def test_extrapolated_is_high_but_never_counts_as_precise():
    """It is a projection, not a survey: pipeline._classify must keep applying its
    boundary-street and near-edge caution, which keys on is_precise_source."""
    assert geocode.confidence("extrapolated") == "high"
    assert geocode.is_precise_source("extrapolated") is False
    assert geocode.is_precise_source("interpolated") is True


def test_numbers_past_the_end_of_the_street_are_refused_not_clamped(monkeypatch):
    """`_point_on_axis` clamps to the last vertex, so every number projecting past the
    polyline answered with the SAME point. On אלכסנדר ינאי that put 17, 19, 21, 23, 28,
    30 and 32 on one coordinate, graded `high` and drawn as a confident dot. Refusing
    hands the address to a tier that can answer."""
    pts = [(31.2600 + i * 0.0002, 34.79) for i in range(6)]      # street ends at .2610
    _anchored(monkeypatch, {"X": {"2": [31.2600, 34.79], "8": [31.2606, 34.79]}}, pts)
    assert geocode.place_house("X", "12")[0] is not None          # still inside: fine
    far = geocode.place_house("X", "24")                          # projects past the end
    assert far == (None, None)


def test_a_degenerate_anchor_gradient_falls_back_to_the_city_spacing(monkeypatch):
    """Two anchors 16 m apart across 6 house numbers is 2.7 m/number against the city's
    measured 11.2 m — they are not laid out the way house numbers are. Believing them
    compressed a whole street onto one point."""
    pts = [(31.2600 + i * 0.0002, 34.79) for i in range(40)]
    _anchored(monkeypatch, {"X": {"8": [31.2620, 34.79], "14": [31.26202, 34.79]}}, pts)
    a = geocode.place_house("X", "16")[0]
    b = geocode.place_house("X", "20")[0]
    assert a and b and a != b, "distinct numbers must not collapse onto one point"


def test_a_single_anchor_projection_is_graded_street_not_high():
    """Two anchors give a measured gradient; one gives a guessed direction. Same
    coordinate, weaker evidence, and the map has to say so."""
    assert geocode.confidence("projected") == "street"
    assert geocode.is_precise_source("projected") is False


def test_house_numbers_do_not_collapse_onto_one_vertex(monkeypatch):
    """The user's actual complaint, generated by the geocoder rather than the data:
    snapping the computed position to the nearest street VERTEX put eight different
    numbers on אלכסנדר ינאי at ONE point, because a polyline has few vertices and every
    number between two of them rounded to the same one."""
    pts = [(31.2600, 34.79), (31.2610, 34.79), (31.2620, 34.79)]   # 3 vertices only
    monkeypatch.setattr(geocode, "_anchors",
                        {"X": {"2": [31.2600, 34.79], "40": [31.2620, 34.79]}})
    monkeypatch.setattr(geocode, "_street_axis", lambda st: (pts, 0))
    _no_buildings(monkeypatch)
    placed = {geocode.place_house("X", str(n))[0] for n in range(4, 36, 4)}
    assert len(placed) == 8, f"8 numbers collapsed onto {len(placed)} point(s)"


def test_a_projected_point_still_lies_on_the_street(monkeypatch):
    """Interpolating along the segment must not drift off the line it came from."""
    pts = [(31.2600, 34.7900), (31.2620, 34.7900)]
    monkeypatch.setattr(geocode, "_anchors",
                        {"X": {"2": [31.2600, 34.79], "40": [31.2620, 34.79]}})
    monkeypatch.setattr(geocode, "_street_axis", lambda st: (pts, 0))
    _no_buildings(monkeypatch)
    lat, lon = geocode.place_house("X", "20")[0]
    assert 31.2600 <= lat <= 31.2620 and abs(lon - 34.7900) < 1e-9


# --- what we accept from the external geocoders ------------------------------------
def test_an_external_hit_far_from_its_own_street_is_rejected(monkeypatch):
    """The two worst errors in the hold-out were both this: ההגנה 89 placed 3,528 m out
    by nominatim and רחבת יבנה 29 2,964 m out by overpass. A street-level point may sit
    anywhere ALONG its street, so distance FROM that street is the honest test — median
    offset across stored listings is 6 m, so this only catches "wrong street entirely"."""
    monkeypatch.setattr(geocode.streets, "canonical", lambda t: ("רגר", "exact"))
    monkeypatch.setattr(geocode.streets, "geometry",
                        lambda st: [[(31.2600, 34.7900), (31.2610, 34.7900)]])
    near = (31.2605, 34.7901)                       # ~56 m off the nearest vertex
    far = (31.2900, 34.8300)                        # kilometres away
    assert geocode._plausible_external("רגר 5", near, "overpass") is True
    assert geocode._plausible_external("רגר 5", far, "nominatim") is False


def test_an_unknown_street_is_not_judged(monkeypatch):
    """No geometry means no opinion — we must not reject what we cannot check."""
    monkeypatch.setattr(geocode.streets, "canonical", lambda t: (None, None))
    monkeypatch.setattr(geocode.streets, "geometry", lambda st: [])
    assert geocode._plausible_external("רגר 5", (31.29, 34.83), "nominatim") is True


def test_nominatim_must_return_somewhere_to_live(monkeypatch):
    """"ליד האוניברסיטה" matched the RAILWAY STATION named …אוניברסיטה, 783 m away, and
    became a MATCH. We asked for a flat; a station, shop or bus stop is a wrong answer
    however well the name overlaps."""
    import types

    def fake_get(url, **kw):
        return types.SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: [{"lat": "31.265", "lon": "34.801",
                           "class": kw["_cls"], "type": "x"}])

    for cls, expected in (("railway", None), ("amenity", None), ("shop", None),
                          ("highway", (31.265, 34.801)), ("place", (31.265, 34.801))):
        monkeypatch.setattr(geocode.time, "sleep", lambda s: None)
        monkeypatch.setattr("requests.get",
                            lambda url, _cls=cls, **kw: fake_get(url, _cls=_cls, **kw))
        assert geocode._nominatim("ליד האוניברסיטה") == expected, cls


def test_anchors_from_a_same_named_street_do_not_get_merged(tmp_path, monkeypatch):
    """Street names repeat inside the bounding box. Binding anchors on the NAME alone
    gave ההגנה a set containing points 10 m from its geometry AND points 2,887 m away,
    which then placed ההגנה 89 about 3.5 km from the real address — every multi-kilometre
    error in the hold-out came from five anchors like these."""
    import load_osm_addresses as loader
    monkeypatch.setattr(loader, "_collect",
                        lambda path: [(31.2381, 34.7854, "14", "ההגנה"),
                                      (31.2385, 34.7858, "18", "ההגנה"),
                                      (31.2601, 34.8067, "82", "ההגנה")])  # the impostor
    monkeypatch.setattr(loader, "_street_points",
                        lambda: {"ההגנה": [(31.2380, 34.7850), (31.2390, 34.7860)]})
    monkeypatch.setattr(loader.streets, "canonical", lambda t: ("ההגנה", "exact"))
    monkeypatch.setattr(loader, "_pbf", lambda: tmp_path / "fake.osm.pbf")
    (tmp_path / "fake.osm.pbf").write_bytes(b"")
    monkeypatch.setattr(loader.geocode, "_load_anchors", lambda: {})
    out = loader.build(dry_run=True)
    assert set(out["ההגנה"]) == {"14", "18"}, "the far same-named anchor was kept"
