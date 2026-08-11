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
    # …but a long-enough fragment of a key still resolves ("בלוק" ⊂ "הבלוק").
    # The answer is the SURVEYED centroid, not the hand-dropped STATIC_TABLE pin —
    # a drawn outline's centre beats a guess, and the two differ by 67 m here.
    assert geocode.geocode("בלוק") == geocode.landmark_point("הבלוק")


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
        {"type": "way", "center": {"lat": 31.257, "lon": 34.795}}, # BS — used
    ]}))
    assert geocode.geocode("כתובת מרחוב כלשהו") == (31.257, 34.795)


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
    line = [[31.260 + i * 0.0002, 34.795] for i in range(11)]
    monkeypatch.setattr(geocode.streets, "geometry", lambda s: [line])
    monkeypatch.setattr(geocode, "_ANCHORS_PATH", tmp_path / "house_anchors.json")
    _user_anchor_file(monkeypatch, tmp_path)
    _no_buildings(monkeypatch)
    assert geocode.interpolate_house("X", "6") is None          # nothing known yet

    assert geocode.add_anchor("X", "1", 31.2600, 34.795) is True
    assert geocode.add_anchor("X", "11", 31.2620, 34.795) is True
    mid = geocode.interpolate_house("X", "5")
    assert mid is not None and 31.2600 < mid[0] < 31.2620       # a DIFFERENT flat


def test_a_user_anchor_beats_osm_and_survives_a_pbf_rebuild(monkeypatch, tmp_path):
    """User anchors live in their own file so load_osm_addresses.py cannot wipe them,
    and they win: a person looked at the map, OSM did not."""
    (tmp_path / "house_anchors.json").write_text(
        '{"X": {"1": [31.2000, 34.7950]}}', encoding="utf-8")
    monkeypatch.setattr(geocode.streets, "geometry",
                        lambda s: [[[31.2600, 34.795], [31.2620, 34.795]]])
    monkeypatch.setattr(geocode, "_ANCHORS_PATH", tmp_path / "house_anchors.json")
    _user_anchor_file(monkeypatch, tmp_path)
    assert geocode.add_anchor("X", "1", 31.2601, 34.795) is True
    assert geocode._load_anchors()["X"]["1"] == [31.2601, 34.795]


def test_a_mistap_far_from_the_street_is_refused_as_an_anchor(monkeypatch, tmp_path):
    """One bad anchor moves every address on the street, so the same 200 m rule that
    guards OSM's own data guards a hand-placed point. The listing's own manual location
    is stored separately and is unaffected."""
    monkeypatch.setattr(geocode.streets, "geometry",
                        lambda s: [[[31.2600, 34.795], [31.2620, 34.795]]])
    monkeypatch.setattr(geocode, "_ANCHORS_PATH", tmp_path / "house_anchors.json")
    _user_anchor_file(monkeypatch, tmp_path)
    assert geocode.add_anchor("X", "7", 31.2900, 34.8400) is False   # ~4 km away
    assert not (tmp_path / "user_anchors.json").exists()
    # and nothing that isn't a house number ever becomes an anchor
    assert geocode.add_anchor("X", "ב", 31.2610, 34.795) is False


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
    the next tier, where either something better answers or a human sees it.

    THE ASSERTION MOVED, THE RULE DID NOT. This used to assert `got is None`, which was
    the same thing while nothing could answer below the rejection. Two changes made that
    consequence obsolete rather than the rule: the `anchor_neighbour` tier now offers a
    surveyed point on the claimed street, and seeding gave `אלכסנדר ינאי` anchors
    [8, 14, 17, 19, 21, 23, 24, 28, 30, 32] — including 32 itself. What must hold is that
    the REFUSED coordinate is not what comes back; being replaced by better evidence on
    the right street is the tier chain working, not a regression."""
    seen = []
    monkeypatch.setattr(geocode, "_plausible_external",
                        lambda text, pt, src: seen.append((text, src)) or False)
    monkeypatch.setattr(geocode, "place_house", lambda st, hn: ((31.0, 34.0), "interpolated"))
    monkeypatch.setattr(geocode, "_overpass", lambda t: (None, None, True))
    monkeypatch.setattr(geocode.config, "USE_NOMINATIM_FALLBACK", False)
    monkeypatch.setattr(geocode, "_cache", {})
    got, src = geocode.geocode_detailed("אלכסנדר ינאי 32")
    assert seen and seen[0][1] == "interpolated"      # the internal hit WAS checked
    assert got != (31.0, 34.0)                        # and refused, not returned
    assert src != "interpolated"


def test_a_far_away_anchor_is_not_next_door(monkeypatch):
    """`_nearest_anchor_point` must not resurrect the `אלכסנדר ינאי` disaster, where
    anchors 8 and 14 sat past the end of the street's own polyline and numbers 17-32 all
    resolved to one clamped point. Bounded by house NUMBER, not only by metres: at the
    city's median spacing a metres-only bound admitted an anchor eighteen numbers away."""
    monkeypatch.setattr(geocode, "_load_anchors",
                        lambda: {"אלכסנדר ינאי": {"8": [31.2647, 34.7942],
                                                  "14": [31.2648, 34.7943]}})
    assert geocode._nearest_anchor_point("אלכסנדר ינאי 32") is None      # 18 numbers away
    assert geocode._nearest_anchor_point("אלכסנדר ינאי 16") is not None  # 2 -> next door


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
        {"elements": [{"type": "node", "lat": 31.257, "lon": 34.795}]}))
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

    `הבלוק` was assumed to be the same thing without the word שכונה — "a whole student
    quarter, several streets across". SURVEYED (landmarks.json) it is 85 x 96 m, a 123 m
    diagonal: TIGHTER than a typical street centroid, so it is a real place and grades
    precise. Size decides now, not the spelling of the key."""
    import geocode
    assert geocode.confidence("static_area") == "area"
    assert geocode.is_precise_source("static_area") is False
    assert geocode._static_source("שכונה ד") == "static_area"
    # a real place keeps its precision
    assert geocode._static_source("כיכר האבות") == "static"
    assert geocode.confidence("static") == "exact"


def test_a_landmark_is_graded_by_its_measured_size():
    """THE POLYGON IS THE UNCERTAINTY. Guessing goes wrong both ways: `הבלוק` (123 m)
    was thrown away as an area, and calling `אביסרור` (299 m) exact would claim a
    precision it does not have."""
    import geocode
    assert geocode._landmark_grade(123) == "static"          # הבלוק
    assert geocode._landmark_grade(115) == "static"          # מגדלי דוד
    assert geocode._landmark_grade(299) == "static_street"   # אביסרור
    assert geocode._landmark_grade(2375) == "static_area"    # שכונה ד's real extent
    # and the real data agrees
    assert geocode._static_source("הבלוק") == "static"
    assert geocode.has_location(geocode.geocode_detailed("הבלוק")[1]) is True
    assert geocode.has_location(geocode.geocode_detailed("שכונה ד")[1]) is False


def test_an_unsurveyed_key_keeps_its_old_behaviour():
    """No polygon -> nothing changes, so importing a survey can never regress a key it
    does not cover."""
    import geocode
    assert "כיכר האבות" not in geocode.landmarks()
    assert geocode._static_source("כיכר האבות") == "static"
    assert geocode._static_source("שכונת נווה זאב") == "static_area"


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


def test_near_the_university_is_not_a_location():
    """`ליד האוניברסיטה` used to resolve to the campus CENTRE, which is inside the
    campus polygon — 8 listings got a dot in the middle of a university nobody can rent
    in. "Near the university" is not a location, so it now resolves to nothing and the
    listing lands in NEEDS_DATA where a person sees it. A real residential quarter is
    still a landmark."""
    import geocode
    for text in ("ליד האוניברסיטה וסורוקה", "קרוב לאוניברסיטת בן גוריון",
                 "מול שער האוניברסיטה", "בסמוך לסורוקה"):
        assert geocode._descriptive_landmark(text) is None, text
    assert geocode._descriptive_landmark("ליד הבלוק") is not None


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
    # …and mentioning the campus no longer contributes a position at all, so it cannot
    # hijack anything even as a last resort
    monkeypatch.setattr(geocode, "_overpass", lambda t: (None, None, True))
    monkeypatch.setattr(geocode, "_nominatim", lambda t: None)
    assert geocode.geocode_detailed("ליד האוניברסיטה")[0] is None
    # the one landmark left is a residential quarter, and it is ALSO a static key, so
    # the static tier answers it first — as an area, which is what it is
    assert geocode.geocode_detailed("ליד הבלוק")[1] == "static_area"


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


def test_a_stale_process_cannot_wipe_the_geocode_cache(monkeypatch, tmp_path):
    """Twice in one day the on-disk cache went from ~300 entries to 1, and recovering
    cost a 35-minute re-geocode of every listing while the map quietly lost two thirds
    of its dots. The mechanism is always a process holding a small `_cache` — a hold-out
    harness using it as scratch, or a long-lived server whose copy predates a rebuild —
    calling _save_cache() once. The cache is a pure accelerator, so refusing a suspicious
    write can only cost time; allowing one costs data."""
    import json
    p = tmp_path / "geocode_cache.json"
    monkeypatch.setattr(geocode, "_CACHE_PATH", p)
    full = {f"k{i}": {"c": [31.25, 34.79], "s": "static", "v": 7} for i in range(300)}
    p.write_text(json.dumps(full), encoding="utf-8")

    monkeypatch.setattr(geocode, "_cache", {"only": {"m": "now", "v": 7}})
    geocode._save_cache()
    assert len(json.loads(p.read_text(encoding="utf-8"))) == 300, "the wipe got through"

    # a real edit — one uncache — must still persist
    monkeypatch.setattr(geocode, "_cache", {k: v for k, v in list(full.items())[:299]})
    geocode._save_cache()
    assert len(json.loads(p.read_text(encoding="utf-8"))) == 299


def _street_length_m(name):
    import geocode
    import streets
    return sum(geocode._haversine_m(a[0], a[1], b[0], b[1])
               for s in streets.geometry(name) for a, b in zip(s, s[1:]))


def test_one_road_split_by_word_order_is_pooled():
    """OSM writes the same street's name in more than one word order, and each spelling
    kept its own fragment: `ביאליק חיים נחמן` held 135 m while `חיים נחמן ביאליק` held
    2,849 m of the SAME road, nearest vertices 0 m apart. A 135 m stub then failed every
    distance check for house 122, so six ביאליק listings shared one dot."""
    a = _street_length_m("ביאליק חיים נחמן")
    b = _street_length_m("חיים נחמן ביאליק")
    assert a == b > 2000, "both spellings must see the whole road"


def test_one_road_split_by_a_road_type_word_is_pooled():
    """The same split, caused by OSM writing `דרך` on some ways of a road and not others:
    `דרך מצדה` held 5 points and one anchor while `מצדה` held 225 points and 21. So
    `דרך מצדה 69` projected off a single anchor on a stub and came out 585 m from the
    street it names, and was rejected — three מצדה listings lost their house number.

    Pooling the geometry is only half of it: anchors are keyed by street name too, so
    both halves have to be pooled or the numbers stay split."""
    import geocode
    assert _street_length_m("דרך מצדה") == _street_length_m("מצדה") > 2000
    anchors = geocode._load_anchors()
    # the same house numbers under both spellings. Not the same POINTS: where both
    # spellings already carried a number, each keeps its own survey rather than one
    # arbitrarily overwriting the other.
    #
    # The COUNT is a "there are plenty" sanity check, not the invariant — the invariant is
    # that both spellings carry the SAME numbers. It was `> 20` until 2026-08-11, when
    # `seed_anchors.seed_conflict` dropped three govmap seeds on this road (10 and 12
    # sitting 167 m and 198 m from surveyed 11, 48 at 130 m from 47 — implausible for
    # adjacent numbers even across a divided boulevard) and took מצדה to 19. Loosened
    # rather than re-pinned to 19, so honest seed filtering does not fail this test again.
    assert set(anchors["דרך מצדה"]) == set(anchors["מצדה"]) and len(anchors["מצדה"]) > 15
    assert (geocode.interpolate_house("דרך מצדה", "69")
            == geocode.interpolate_house("מצדה", "69") is not None)


def test_the_house_next_door_beats_interpolating_across_the_street(monkeypatch):
    """`שמעון בר גיורא` carries its odd numbers ~200 m from its even ones. Number 26 has no
    even anchor above 24, so the same-parity bracket fails and `_anchors_for` falls back to
    ALL anchors — which bracketed it between odd 25 and 27 on the far arm, 200 m out. That
    turned a RED flat GREEN, the one error class this project treats as worse than not
    placing at all, while even 24 sat two numbers away and 6 m from the truth."""
    import geocode
    known = {"18": [31.2600, 34.7920], "20": [31.2601, 34.7920],
             "22": [31.2602, 34.7912], "24": [31.2603, 34.7910],
             "25": [31.2604, 34.7928], "27": [31.2605, 34.7928]}
    got = geocode._same_parity_neighbour(known, "26")
    assert got == (31.2603, 34.7910)               # even 24, not the odd pair


def test_a_number_its_own_side_can_bracket_is_left_to_interpolation(monkeypatch):
    """The guard is narrow ON PURPOSE: it must not pre-empt a correct interpolation. 22 has
    even anchors on both sides, so the right side of the street can answer without help."""
    import geocode
    known = {"18": [31.2600, 34.7920], "20": [31.2601, 34.7920],
             "24": [31.2603, 34.7910], "25": [31.2604, 34.7928]}
    assert geocode._same_parity_neighbour(known, "22") is None


def test_a_neighbour_further_than_next_door_is_not_evidence():
    """Bounded by NEIGHBOUR_MAX_NUMBERS for the reason that constant already gives: beyond
    next door it is a different part of the road, and the אלכסנדר ינאי disaster (17..32 all
    answered by one clamped point) is what a loose bound buys."""
    import geocode
    known = {"10": [31.26, 34.79], "11": [31.26, 34.795]}
    assert geocode._same_parity_neighbour(known, "20") is None


def test_a_railway_station_is_not_a_street():
    """`תחנת רכבת צפון - אוניברסיטה` sat in the street index, and because a unique word
    run wins in `_words_index`, `האוניברסיטה` canonicalised straight to it — so
    `ליד האוניברסיטה` resolved to a platform 783 m from anywhere anyone lives."""
    import streets
    assert streets.canonical("האוניברסיטה") == (None, None)
    assert streets.canonical("תחנת רכבת צפון - אוניברסיטה") == (None, None)
    assert streets.geometry("תחנת רכבת צפון - אוניברסיטה") == []
    assert streets.canonical("רגר")[0], "a real street must still resolve"


def test_no_external_geocoder_may_answer_a_bare_proximity_phrase():
    """Dropping the university from _LANDMARKS was only HALF of the 2026-08-01 decision.

    The phrase still fell through to Overpass, which answered `ליד האוניברסיטה` with a
    point outside the campus polygon — so the no-housing mask did not catch it either —
    and two listings came back as AMBER MATCHes in the next replay.
    `_plausible_external` cannot cover this: it abstains when there is no street to
    measure against, which is exactly this case."""
    import geocode
    for vague in ("ליד האוניברסיטה", "בסמוך לסורוקה", "קרוב לאוניברסיטת בן גוריון",
                  "ליד האוניברסיטה וסורוקה"):
        assert geocode._is_bare_proximity(vague), vague
    # A proximity phrase that also names a real STREET is a real address, so the
    # external tiers must still run for it.
    for real in ("רגר 5, ליד האוניברסיטה", "רינגלבלום ליד האוניברסיטה"):
        assert not geocode._is_bare_proximity(real), real
    # `ליד הבלוק` IS bare proximity, but הבלוק is a residential quarter we hold a point
    # for, so the guard hands off to the landmark tier instead of refusing.
    assert geocode._is_bare_proximity("ליד הבלוק")
    assert geocode._descriptive_landmark("ליד הבלוק") is not None


def test_an_institution_is_not_an_address():
    """`אוניברסיטת בן גוריון` as a whole address resolved through Overpass to a real
    campus coordinate — a dot on a lawn. Nobody rents there (user, 2026-08-03), so it
    resolves to nothing and the listing lands in NEEDS_DATA where a person sees it."""
    import geocode
    for inst in ("אוניברסיטת בן גוריון", "אוניברסיטה", "שער האוניברסיטה",
                 "סורוקה", "קמפוס"):
        geocode.uncache(inst)
        assert geocode.geocode_detailed(inst) == (None, None), inst
    # a NAMED BUILDING that merely mentions one still resolves — the static table
    # answers before this guard runs
    assert geocode.geocode_detailed("מגדלי דוד, סורוקה")[0] is not None
    # and a real street with a house number is untouched
    assert geocode.geocode_detailed("רינגלבלום ליד האוניברסיטה")[0] is not None


def test_the_two_hand_supplied_student_blocks():
    """Both are named constantly in posts and no free source places them.
    `מגדלי דוד, סורוקה` was actively WRONG before: it fell through to Overpass and
    landed on the hospital/campus point."""
    import geocode
    # Both are SURVEYED now, so the answer is the polygon centroid rather than the
    # single coordinate they were first pinned with (מגדלי דוד moved 8 m, אביסרור 89 m).
    assert geocode.geocode_detailed("מגדלי דוד") == (geocode.landmark_point("מגדלי דוד"),
                                                     "static")
    # אביסרור is 299 m across — a district you still have to walk, so street-level
    assert geocode.geocode_detailed("אביסרור")[1] == "static_street"
    # the bare key matches the spellings the posts actually use
    for spelling in ("מגדלי גראנד אביסרור", "אביסרורים הגבוהים", "אביסרורים"):
        assert geocode.geocode_detailed(spelling)[0] == geocode.landmark_point("אביסרור"), spelling


def test_a_descriptive_tail_does_not_hide_the_street():
    """A post often ends the address with what the flat is LIKE rather than where it is,
    and `_SPLIT_RE` only splits on punctuation, so the lot was glued into one token:
    `וינגייט 74 שכונה ג קומה שניה` produced the candidate `וינגייט קומה שניה`, matching
    no street, although `canonical("וינגייט")` is exact."""
    import geocode
    import streets
    for addr, want in (("וינגייט 74 שכונה ג קומה שניה", "וינגייט"),
                       ("רחוב אביה השופט 4 שכונה ד ( ו הישנה), באר שבע", "אביה השופט"),
                       ("מצדה 17 (מול הפארק)", "מצדה")):
        tok = geocode._candidate_tokens(addr)
        assert tok, addr
        assert streets.canonical(tok[0])[0] == want, f"{addr} -> {tok}"
    # the parenthetical case only works because the tail is stripped BEFORE the split —
    # _SPLIT_RE breaks on `מול` and would tear `(מול הפארק)` in half
    assert "(" not in " ".join(geocode._candidate_tokens("מצדה 17 (מול הפארק)"))


def test_several_hits_that_are_one_road_is_not_ambiguity():
    """`קלישר` matched both `צבי קלישר` and `קלישר הרב צבי` — two OSM spellings of one
    street, measured 0.0 m apart — and the uniqueness rule threw it away. The honorific
    kept their word bags apart so they never pooled either."""
    import streets
    assert streets._pool_key("צבי קלישר") == streets._pool_key("קלישר הרב צבי")
    real, how = streets.canonical("קלישר")
    assert real in ("צבי קלישר", "קלישר הרב צבי") and how == "word"
    # …and a genuine ambiguity still refuses: these two are 2,652 m apart, so they never
    # pool, and answering either one would be a guess
    assert streets._nearest_m(streets._raw_geometry()["ז'בוטינסקי"],
                              streets._raw_geometry()["יוהנה זבוטינסקי"]) > 1000


def test_a_shared_word_bag_is_not_enough_to_pool():
    """The guard that makes the pooling safe. `כיכר האבות` and `האבות` share a word bag
    once the road-type word is dropped, and lie 2.5 km apart — welding those together is
    exactly the multi-kilometre error the 200 m off-street guard exists to catch."""
    import streets
    assert streets._pool_key("כיכר האבות") == streets._pool_key("האבות")
    assert streets.aliases("כיכר האבות") == ["כיכר האבות"]
    assert _street_length_m("כיכר האבות") != _street_length_m("האבות")


def test_an_area_key_steps_aside_for_a_house_number():
    """`רגר 137, הבלוק` resolved to the slang QUARTER instead of house 137, even though
    רגר is anchored 53-191. The static match already stood aside for street keys when the
    address carried a number; area keys did not."""
    import geocode
    for addr in ("רגר 137, הבלוק", "מצדה 6, הבלוק", "יצחק אבינו 20, הבלוק"):
        _pt, src = geocode.geocode_detailed(addr)
        assert src not in ("static_area", "static"), f"{addr} was hijacked by the area key"
    # A HOUSE NUMBER BEATS EVERY LANDMARK, however tight — that is what the loop above
    # asserts. Once `הבלוק` was surveyed it stopped grading `static_area` and silently
    # dropped out of the stand-aside rule, re-breaking `רגר 137, הבלוק`; the rule now
    # tests membership of `landmarks()` too. A house number interpolates to ~13 m, the
    # smallest surveyed landmark is 115 m.
    assert geocode.geocode_detailed("הבלוק")[1] == "static"


def test_a_named_street_beats_a_co_occurring_neighbourhood():
    """"A street is okay" (user's rule) — but a neighbourhood key only stood aside for a
    HOUSE NUMBER, so an address naming a street and a quarter got the quarter's centroid.
    Measured: 13 listings drawn 364-1,070 m from the street their own post names,
    `שלמה המלך, שכונה ג` worst at 1,070 m."""
    import geocode
    import zones
    for addr, street in (("שלמה המלך, שכונה ג", "שלמה המלך"),
                         ("אברהם אבינו, שכונה ד", "אברהם אבינו"),
                         ("אלעזר בן יאיר שכונה ד", "אלעזר בן יאיר")):
        pt, src = geocode.geocode_detailed(addr)
        assert src != "static_area", f"{addr} still answered by the neighbourhood"
        assert geocode.has_location(src), addr
        sp, _ = geocode.geocode_detailed(street)
        if pt and sp:                       # within the street's own extent, not a quarter away
            assert zones._haversine_m(pt[0], pt[1], sp[0], sp[1]) < 300, addr


def test_the_street_gate_ignores_a_fuzzy_match():
    """`גר בשכונה ג ליד האוני` has `האוני` FUZZY-matching the street `הגאונים`. A gate
    that decides whether to throw away a working placement has to be certain, or "near
    the university" looks like a street address and loses the only key that can place it."""
    import geocode
    assert geocode.geocode_detailed("גר בשכונה ג ליד האוני")[1] == "static_area"


def test_a_bare_neighbourhood_still_has_no_location():
    """The half of the user's rule this change must NOT touch."""
    import geocode
    for addr in ("שכונה ד", "שכונה ג", "שכונה ב"):
        assert geocode.has_location(geocode.geocode_detailed(addr)[1]) is False, addr


def test_near_a_landmark_is_not_at_the_landmark():
    """Spotted by the user. The static table answers several tiers before
    `_is_bare_proximity` would ever run, so `ליד מגדלי דוד` returned the building's own
    point graded `static` — claiming the flat IS there. Nothing in the 321 current
    listings says "near", but grading `הבלוק` precise turns tomorrow's `ליד הבלוק` from a
    vague blob into a confidently wrong dot."""
    import geocode
    for addr in ("ליד מגדלי דוד", "ליד הבלוק", "קרוב להבלוק", "בסמוך להבלוק"):
        _pt, src = geocode.geocode_detailed(addr)
        assert geocode.confidence(src) == "area", f"{addr} claimed to be AT the landmark"
    # …while actually being there keeps full precision
    assert geocode.geocode_detailed("מגדלי דוד, סורוקה")[1] == "static"
    assert geocode.geocode_detailed("הבלוק")[1] == "static"


def test_the_proximity_word_must_govern_the_landmark():
    """Position, not mere presence: `רגר 5, ליד הבלוק` is a real address that happens to
    mention a bearing, and must keep its house number."""
    import geocode
    _pt, src = geocode.geocode_detailed("רגר 5, ליד הבלוק")
    assert src not in ("static", "static_area"), src
    assert geocode.has_location(src)


def test_a_landmark_the_flat_is_AT_beats_one_it_is_only_NEAR():
    """Posts name several places at once, and the bearing belongs to exactly one of them.
    `ליד הבלוק, מגדלי דוד` is NEAR הבלוק and AT מגדלי דוד. Ranking purely by position
    answered with the landmark it was near and graded the whole address `area`, throwing
    away the address the post actually gave. Word order must not decide this."""
    import geocode
    for addr in ("מגדלי דוד, ליד הבלוק", "ליד הבלוק, מגדלי דוד"):
        pt, src = geocode.geocode_detailed(addr)
        assert src == "static", addr
        assert pt == geocode.landmark_point("מגדלי דוד"), addr
    # …and the mirror image resolves to the other one
    pt, src = geocode.geocode_detailed("ליד מגדלי דוד, הבלוק")
    assert src == "static" and pt == geocode.landmark_point("הבלוק")
    # only when EVERY match is a bearing does it degrade to an area
    assert geocode.geocode_detailed("דירה ליד הבלוק")[1] == "static_area"


def test_the_bearing_lookback_stops_at_a_separator():
    """The window that decides "is this a bearing" must not reach over a comma into the
    previous place's proximity word."""
    import geocode
    n = geocode._normalize
    assert geocode._near_governs(n("דירה ליד הבלוק"), n("הבלוק")) is True
    assert geocode._near_governs(n("ליד הבלוק, מגדלי דוד"), n("מגדלי דוד")) is False
    assert geocode._near_governs(n("ליד הבלוק, מגדלי דוד"), n("הבלוק")) is True
# --- the street's own polyline: a street we hold geometry for is a location -------
def _offline(monkeypatch, tmp_path):
    """Every network tier off and a private cache, so what's left is exactly what the
    LOCAL tiers can do. The street tier deliberately runs last, after the geocoders have
    had their chance, so a test that left them on would be measuring the network."""
    monkeypatch.setattr(geocode, "_cache", {})
    monkeypatch.setattr(geocode, "_CACHE_PATH", tmp_path / "geo.json")
    monkeypatch.setattr(geocode.config, "USE_GOOGLE_GEOCODE", False)
    monkeypatch.setattr(geocode.config, "USE_OVERPASS_FALLBACK", False)
    monkeypatch.setattr(geocode.config, "USE_NOMINATIM_FALLBACK", False)


def test_a_bare_known_street_places_at_street_confidence(monkeypatch, tmp_path):
    """A street whose POLYLINE we hold is never really unknown, but with no house number
    nothing local used to answer it and the externals could not match the gershayim
    spelling: `רחוב רמב״ם` came back (None, None) while `streets.canonical` answered
    ('רמב"ם', 'exact'). Measured 2026-08-03, that was the last of 322 listings naming a
    resolvable street with no location — and under the drop rule of the same day an
    unlocated listing can be deleted outright.

    Street level, never precise: a street is a line, so the answer says "somewhere on
    רמב״ם" and the boundary/edge caution in `pipeline._classify` still applies."""
    _offline(monkeypatch, tmp_path)
    import streets
    for spelling in ('רחוב רמב״ם', 'רחוב רמב"ם', 'רמב״ם'):
        pt, src = geocode.geocode_detailed(spelling)
        assert pt is not None, spelling
        assert src == "street_geom", (spelling, src)
        assert geocode.confidence(src) == "street", spelling
        assert geocode.has_location(src), spelling
        assert not geocode.is_precise_source(src), spelling
        # the 200 m off-street guard: the point is ON the street it claims
        assert geocode._off_street_m('רמב"ם', pt[0], pt[1]) <= geocode.MAX_ANCHOR_OFFSET_M
        assert geocode._in_beer_sheva(*pt), spelling
    # and the no-network viewers draw the same dot, or the pipeline would place a
    # listing the dashboard renders nothing for
    assert geocode.geocode_cached('רחוב רמב״ם') is not None
    assert streets.known('רמב"ם')


def test_the_street_point_is_inside_the_polyline_never_its_end(monkeypatch, tmp_path):
    """"Never clamp past the end of a street's polyline" — `_point_on_axis` answers the
    last vertex for any target beyond it, which is how seven אלכסנדר ינאי numbers all got
    one identical point graded `high`. The midpoint of the street's own extent is
    strictly between its first and last vertex, so it cannot be that clamped endpoint."""
    _offline(monkeypatch, tmp_path)
    pts, idx = geocode._street_axis('רמב"ם')
    pt = geocode.street_point('רמב"ם')
    assert pt is not None
    assert pts[0][idx] < pt[idx] < pts[-1][idx]


def test_a_street_midpoint_never_answers_a_house_number(monkeypatch, tmp_path):
    """"אברהם אבינו 38" can't be answered by the street's midpoint — that is how a
    red-end address read as green, and the whole reason house-number interpolation
    exists. The new tier refuses a number outright rather than competing with it."""
    _offline(monkeypatch, tmp_path)
    assert geocode._bare_street_point('רמב"ם 5') is None
    assert geocode.geocode_detailed('רמב"ם 5')[1] != "street_geom"
    assert geocode._bare_street_point("אברהם אבינו 38") is None


def test_a_name_covering_two_far_apart_roads_gets_no_point(monkeypatch, tmp_path):
    """The 200 m off-street guard, doing real work. A name in the index can cover two
    roads that are not one road: the axis midpoint of `לימונית` lands 4.9 km from any
    לימונית vertex, in the desert between two neighbourhoods. Measured over all 1,172
    named streets with geometry the midpoint is a median 11 m from the nearest vertex and
    33 fail this check — the multi-kilometre error class the guard was written for."""
    _offline(monkeypatch, tmp_path)
    assert geocode.street_point("לימונית") is None
    assert geocode.geocode_detailed("לימונית") == (None, None)


def test_a_cached_miss_does_not_hide_a_street_we_hold(monkeypatch, tmp_path):
    """A negative cache entry is about the GEOCODERS, not about us. The miss is written
    the moment Overpass and Nominatim fail, so from the second lookup onward the street
    tier would never be reached — the fix would work once and then stop, which is the
    hardest kind of regression to see."""
    from datetime import datetime
    _offline(monkeypatch, tmp_path)
    fresh = {"m": datetime.now().isoformat(timespec="seconds"),
             "v": geocode.GEOCODE_LOGIC_VERSION}
    monkeypatch.setattr(geocode, "_cache", {'רחוב רמב"ם': dict(fresh),
                                            "כתובת שאינה קיימת": dict(fresh)})
    assert geocode._cache_lookup('רחוב רמב"ם')[0] == "miss"      # the miss is really there
    assert geocode.geocode_detailed('רחוב רמב״ם')[1] == "street_geom"
    # …and a string that names no street stays missed
    assert geocode.geocode_detailed("כתובת שאינה קיימת") == (None, None)


def test_an_institution_never_gets_a_street_point(monkeypatch, tmp_path):
    """NOBODY LIVES ON THE CAMPUS (2026-08-01). `_named_street` reads the RAW text and an
    institution's own words look like a street to the index — `אוניברסיטת בן גוריון`
    yields the boulevard `שדרות בן גוריון` — so without the proximity guard this tier
    would hand a place nobody rents a confident street-level dot."""
    _offline(monkeypatch, tmp_path)
    import geocode as g
    assert g._named_street("אוניברסיטת בן גוריון") is not None   # the trap is real
    for addr in ("אוניברסיטת בן גוריון", "ליד האוניברסיטה", "מול שער האוניברסיטה",
                 "קמפוס", "סורוקה"):
        assert geocode.geocode_detailed(addr) == (None, None), addr
    # a street that merely mentions the campus keeps its placement
    assert geocode.has_location(geocode.geocode_detailed("רינגלבלום ליד האוניברסיטה")[1])
