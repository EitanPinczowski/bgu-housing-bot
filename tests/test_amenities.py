"""Amenity/transit proximity: the GTFS loader's rules and the runtime lookup.

Fully offline — a tiny synthetic GTFS zip built in-memory stands in for the ~100 MB
national feed, and OSRM is stubbed. Two things are load-bearing here:
  1. the DIRECTION rule (a bus that passes the station BEFORE your stop doesn't take
     you there), which would otherwise fail silently and plausibly, and
  2. that every failure path degrades to {} — this feature is display-only and must
     never be able to break a run.
"""
import json
import zipfile

import pytest

import amenities
import config
import load_amenities as la


# --- a synthetic feed -------------------------------------------------------------
# Four stops on a line, all inside the Be'er Sheva box. S_STATION is the train station.
_STOPS = [
    ("s1", "רגר/בן גוריון", 31.2600, 34.7990),
    ("s2", "רגר/יצחק רגר", 31.2550, 34.7985),
    ("st", "רכבת באר שבע מרכז", 31.2430, 34.7981),
    ("s3", "אחרי הרכבת", 31.2350, 34.7975),
]


def _csv(rows) -> str:
    return "\n".join(",".join(str(c) for c in r) for r in rows) + "\n"


def _feed(tmp_path, trips, stop_times):
    """Write a minimal GTFS zip. `trips` = [(trip_id, route_id, direction_id)],
    `stop_times` = [(trip_id, stop_id, sequence, departure)]."""
    path = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("stops.txt", _csv(
            [("stop_id", "stop_name", "stop_lat", "stop_lon")] + _STOPS))
        z.writestr("routes.txt", _csv([
            ("route_id", "route_short_name"), ("r669", "669"), ("r12", "12")]))
        z.writestr("calendar.txt", _csv([
            ("service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday", "start_date", "end_date"),
            ("wk", 1, 1, 1, 1, 0, 0, 0, "20200101", "20991231"),
            ("expired", 1, 1, 1, 1, 0, 0, 0, "20200101", "20200102")]))
        z.writestr("trips.txt", _csv(
            [("route_id", "service_id", "trip_id", "direction_id")]
            + [(rid, svc, tid, d) for tid, rid, d, svc in trips]))
        z.writestr("stop_times.txt", _csv(
            [("trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence")]
            + [(t, dep, dep, s, seq) for t, s, seq, dep in stop_times]))
    return path


@pytest.fixture
def feed(tmp_path, monkeypatch):
    """A feed with: two 669 trips (one per direction) and one line-12 trip that runs
    s1 -> s2 -> station -> s3."""
    trips = [("t669a", "r669", 0, "wk"), ("t669b", "r669", 1, "wk"),
             ("t12", "r12", 0, "wk")]
    times = []
    for h in range(8, 18):                       # 10 departures inside the 07–22 window
        times += [("t669a", "s1", 1, f"{h}:00:00"), ("t669a", "s2", 2, f"{h}:05:00")]
    times += [("t669b", "s2", 1, "09:00:00"), ("t669b", "s1", 2, "09:06:00")]
    times += [("t12", "s1", 1, "07:00:00"), ("t12", "s2", 2, "07:05:00"),
              ("t12", "st", 3, "07:12:00"), ("t12", "s3", 4, "07:20:00")]
    path = _feed(tmp_path, trips, times)
    monkeypatch.setattr(config, "GTFS_CACHE_PATH", path)
    return path


def _built(feed):
    with zipfile.ZipFile(feed) as z:
        services = la._active_services(z)
        stops = la._bs_stops(z)
        station = la._station_stop_ids(stops)
        meta, by_trip = la._scan_trips(z, stops, services)
    return stops, station, meta, by_trip


def test_expired_service_is_excluded(feed):
    with zipfile.ZipFile(feed) as z:
        assert la._active_services(z) == {"wk"}      # the 2020-ending one is dropped


def test_station_found_by_name(feed):
    stops, station, _, _ = _built(feed)
    assert station == {"st"}


def test_bus_route_target_keeps_both_directions(feed):
    stops, _station, meta, by_trip = _built(feed)
    spec = {"key": "bus669", "kind": "bus_route", "route": "669", "street": "רגר",
            "label": "669 מרגר"}
    t = la.build_route_target(spec, stops, meta, by_trip)
    # the street filter keeps only the two רגר stops, and both directions survive
    assert {r["name"] for r in t["stops"]} == {"רגר/בן גוריון", "רגר/יצחק רגר"}
    assert {r["direction_id"] for r in t["stops"]} == {"0", "1"}


def test_headway_from_departure_count(feed):
    stops, _station, meta, by_trip = _built(feed)
    t = la.build_route_target({"key": "b", "kind": "bus_route", "route": "669"},
                              stops, meta, by_trip)
    # s1 direction 0: 10 departures across the 15-hour window -> 900/10 = 90 min
    row = next(r for r in t["stops"] if r["name"].endswith("בן גוריון")
               and r["direction_id"] == "0")
    assert row["headway_min"] == 90


def test_toward_the_station_excludes_stops_after_it(feed):
    """THE rule this module lives or dies on: the line-12 trip reaches s3 only AFTER
    the station, so boarding at s3 does NOT take you to the train."""
    stops, station, meta, by_trip = _built(feed)
    t = la.build_toward_target({"key": "train", "kind": "bus_toward"},
                               stops, meta, by_trip, station)
    names = {r["name"] for r in t["stops"]}
    assert "אחרי הרכבת" not in names
    assert names == {"רגר/בן גוריון", "רגר/יצחק רגר"}
    assert all(r["route"] == "12" for r in t["stops"])


def test_station_itself_is_not_listed_as_a_stop_toward_it(feed):
    stops, station, meta, by_trip = _built(feed)
    t = la.build_toward_target({"key": "train", "kind": "bus_toward"},
                               stops, meta, by_trip, station)
    assert "רכבת באר שבע מרכז" not in {r["name"] for r in t["stops"]}


def test_gtfs_hours_past_midnight(feed):
    assert la._dep_hour("25:10:00") == 1          # GTFS runs past 24 for late trips
    assert la._dep_hour("07:00:00") == 7
    assert la._dep_hour("bogus") is None


def test_an_empty_result_never_overwrites_good_data(feed, monkeypatch, capsys):
    """A transient Overpass outage once wiped the resolved gym out of amenities.json.
    Builders must OMIT a target they couldn't resolve, so main()'s merge keeps the
    previous entry instead of replacing it with an empty list."""
    monkeypatch.setattr(la, "_overpass", lambda q: None)          # every mirror down
    assert la.build_poi_targets() == {}
    assert "keeping whatever" in capsys.readouterr().out
    # and the same for a transit line that resolves to nothing (e.g. renumbered)
    monkeypatch.setattr(config, "AMENITY_TARGETS",
                        [{"key": "ghost", "kind": "bus_route", "route": "99999"}])
    assert la.build_transit_targets() == {}


# --- runtime lookup ---------------------------------------------------------------
_NEAR = 31.2610, 34.7995                          # a few hundred metres from s1/s2


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """amenities.py pointed at a small data file, with OSRM stubbed and the caches
    reset so tests don't leak into each other."""
    data = {"targets": {
        "bus669": {"label": "669 מרגר", "icon": "🚌", "kind": "bus_route", "stops": [
            {"name": "רגר/בן גוריון", "lat": 31.2600, "lon": 34.7990,
             "direction_id": "0", "route": "669", "headway_min": 20},
            {"name": "רגר/יצחק רגר", "lat": 31.2550, "lon": 34.7985,
             "direction_id": "1", "route": "669", "headway_min": 30}]},
        "gym": {"label": "חדר כושר עזריאלי", "icon": "🏋️", "kind": "poi", "points": [
            {"name": "קניון עזריאלי הנגב", "lat": 31.2605, "lon": 34.7999}]}}}
    path = tmp_path / "amenities.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "AMENITIES_PATH", path)
    monkeypatch.setattr(amenities, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(amenities, "_data", None)
    monkeypatch.setattr(amenities, "_cache", None)
    monkeypatch.setattr(amenities.osrm, "alive", lambda: True)
    # minutes in the order the candidates were collected
    monkeypatch.setattr(amenities.osrm, "table_minutes",
                        lambda lat, lon, dests, **k: [4.0, 9.0, 13.0][:len(dests)])
    return path


def test_nearby_reports_both_directions_and_the_poi(wired):
    am = amenities.nearby(*_NEAR)
    assert list(am) == ["bus669", "gym"]                # config order, not distance
    assert len(am["bus669"]["options"]) == 2            # one per direction
    assert am["bus669"]["options"][0]["minutes"] == 4.0
    assert am["gym"]["options"][0]["name"] == "קניון עזריאלי הנגב"


def test_a_slightly_longer_walk_wins_if_the_bus_is_much_more_frequent(monkeypatch, tmp_path):
    """Real regression: at רגר 100 the nearest stop had a bus every 36 minutes and a
    stop 6 metres further had one every 7. Nearest-only reported the 36."""
    data = {"targets": {"train": {"label": "לרכבת מרכז", "icon": "🚆",
                                  "kind": "bus_toward", "stops": [
        {"name": "איטי", "lat": 31.2600, "lon": 34.7990, "route": "332", "headway_min": 36},
        {"name": "תכוף", "lat": 31.2601, "lon": 34.7991, "route": "62", "headway_min": 7}]}}}
    path = tmp_path / "a.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "AMENITIES_PATH", path)
    monkeypatch.setattr(amenities, "_CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(amenities, "_data", None)
    monkeypatch.setattr(amenities, "_cache", {})
    monkeypatch.setattr(amenities.osrm, "alive", lambda: True)
    monkeypatch.setattr(amenities.osrm, "table_minutes", lambda *a, **k: [2.0, 3.0])
    opt = amenities.nearby(31.2600, 34.7990)["train"]["options"][0]
    assert opt["route"] == "62"                      # the frequent one, one minute further

    # …but only within the detour budget: a far-away frequent stop must not win
    monkeypatch.setattr(amenities, "_cache", {})
    monkeypatch.setattr(amenities.osrm, "table_minutes", lambda *a, **k: [2.0, 20.0])
    assert amenities.nearby(31.2600, 34.7990)["train"]["options"][0]["route"] == "332"


def test_a_target_can_widen_its_own_radius(monkeypatch, tmp_path):
    """The single gym sits 2-3 km out; on the shared 1500 m radius it never appeared
    on any listing at all, so a target may raise its own limit."""
    far = {"targets": {"gym": {"label": "חדר כושר", "icon": "🏋️", "kind": "poi",
                               "points": [{"name": "קניון", "lat": 31.2437, "lon": 34.7951}]}}}
    path = tmp_path / "a.json"
    path.write_text(json.dumps(far, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "AMENITIES_PATH", path)
    monkeypatch.setattr(amenities, "_CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(amenities, "_data", None)
    monkeypatch.setattr(amenities, "_cache", {})
    monkeypatch.setattr(amenities.osrm, "alive", lambda: True)
    monkeypatch.setattr(amenities.osrm, "table_minutes", lambda *a, **k: [27.0])
    here = (31.2594, 34.7961)                        # ~1.75 km away: outside the default
    monkeypatch.setattr(config, "AMENITY_TARGETS",
                        [{"key": "gym", "kind": "poi", "query": "x"}])
    assert amenities.nearby(*here) == {}
    monkeypatch.setattr(amenities, "_cache", {})
    monkeypatch.setattr(config, "AMENITY_TARGETS",
                        [{"key": "gym", "kind": "poi", "query": "x", "max_meters": 4000}])
    assert amenities.nearby(*here)["gym"]["options"][0]["minutes"] == 27.0


def test_nearby_is_cached_per_coordinate(wired, monkeypatch):
    amenities.nearby(*_NEAR)
    calls = []
    monkeypatch.setattr(amenities.osrm, "table_minutes",
                        lambda *a, **k: calls.append(1) or [1.0, 2.0, 3.0])
    amenities.nearby(*_NEAR)
    assert calls == []                                  # served from the cache


def test_degrades_to_empty_and_never_raises(wired, monkeypatch, tmp_path):
    # OSRM down -> {} (and NOT cached, so it recovers when the container comes back)
    monkeypatch.setattr(amenities, "_cache", {})
    monkeypatch.setattr(amenities.osrm, "alive", lambda: False)
    assert amenities.nearby(*_NEAR) == {}
    assert amenities._cache == {}
    # OSRM answers with nothing -> {}
    monkeypatch.setattr(amenities.osrm, "alive", lambda: True)
    monkeypatch.setattr(amenities.osrm, "table_minutes", lambda *a, **k: None)
    assert amenities.nearby(*_NEAR) == {}
    # nothing in range -> {}
    monkeypatch.setattr(amenities, "_cache", {})
    assert amenities.nearby(32.5, 35.5) == {}
    # no data file at all -> {}
    monkeypatch.setattr(config, "AMENITIES_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(amenities, "_data", None)
    monkeypatch.setattr(amenities, "_cache", {})
    assert amenities.nearby(*_NEAR) == {}
    # a coordinate we never geocoded
    assert amenities.nearby(None, None) == {}
    # and an outright broken payload can't take a run down
    monkeypatch.setattr(amenities, "_data", {"targets": {"x": None}})
    assert amenities.nearby(*_NEAR) == {}


def test_describe_renders_one_fragment_per_target(wired):
    frags = amenities.describe(amenities.nearby(*_NEAR))
    assert frags[0].startswith("🚌 669 מרגר")
    assert "↔" in frags[0] and "כל ~20 דק׳" in frags[0]   # both directions + frequency
    assert frags[1] == "🏋️ חדר כושר עזריאלי · 13 דק׳"


def test_describe_of_nothing_is_nothing():
    assert amenities.describe(None) == [] and amenities.describe({}) == []
