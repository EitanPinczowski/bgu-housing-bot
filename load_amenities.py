"""
Precompute `amenities.json` — the transit stops and places we report next to each
listing (see config.AMENITY_TARGETS). Run occasionally; the bot itself then needs
no network for this.

    python load_amenities.py                 # download + rebuild everything
    python load_amenities.py --skip-download # reuse the cached GTFS zip
    python load_amenities.py --poi-only      # refresh only the Overpass places

Two free, official sources:
  • TRANSIT — Israel Ministry of Transport GTFS (config.GTFS_URL). OpenStreetMap
    knows where stops are but not reliably which lines serve them and never how
    OFTEN they run, so frequency forces GTFS. The feed is ~100 MB zipped and
    stop_times.txt is ~1 GB raw, so it is streamed straight out of the zip and
    never extracted to disk.
  • PLACES — Overpass (same free mirrors used by geocode.py / load_area_features.py).

Everything here is DISPLAY-ONLY data: it never touches the fit score.
"""
from __future__ import annotations
import argparse
import csv
import io
import json
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime, date
from typing import Optional

import requests

import config
import geocode

OUT_PATH = config.AMENITIES_PATH
_WEEKDAY_COL = ["monday", "tuesday", "wednesday", "thursday", "friday",
                "saturday", "sunday"]


# ---------------------------------------------------------------------------
# Overpass places
# ---------------------------------------------------------------------------
def _overpass(query: str):
    """First mirror that answers wins (OSM data is identical across them).
    Same pattern as load_area_features.py."""
    timeout = max(getattr(config, "OVERPASS_TIMEOUT_SEC", 15), 40)
    for url in config.OVERPASS_URLS:
        try:
            time.sleep(1.0)
            r = requests.post(url, data={"data": query},
                              headers={"User-Agent": config.NOMINATIM_USER_AGENT},
                              timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            print(f"  mirror {url.split('/')[2]} failed ({type(exc).__name__})")
    return None


def _poi_points(spec: dict) -> list:
    """Coordinates for a named place inside the Be'er Sheva box, via Overpass.

    We resolve the MALL rather than hunting for the gym inside it: OSM rarely maps
    the fitness centre as its own node, and the walk to the building is the walk to
    the gym. `out center` collapses ways/relations to one representative point.

    `spec["match_names"]` is tried in order and the first that hits wins — OSM lags
    real-world rebrands (this mall is still tagged with its previous name), so a
    single hardcoded name would silently resolve to nothing."""
    la0, lo0, la1, lo1 = geocode._bs_bounds()
    bbox = f"{la0},{lo0},{la1},{lo1}"
    for pattern in spec.get("match_names") or [spec["query"]]:
        data = _overpass(f'[out:json][timeout:40];'
                         f'nwr["name"~"{pattern}"]({bbox});out center 30;')
        out = []
        for el in (data or {}).get("elements", []):
            tags = el.get("tags", {})
            lat = el.get("lat", (el.get("center") or {}).get("lat"))
            lon = el.get("lon", (el.get("center") or {}).get("lon"))
            if lat is None or lon is None:
                continue
            # A mall/fitness venue beats an unrelated shop sharing the word.
            rank = 0 if (tags.get("shop") == "mall"
                         or tags.get("leisure") == "fitness_centre") else 1
            out.append((rank, {"name": tags.get("name", ""), "lat": lat, "lon": lon}))
        if out:
            # If we found the venue itself, drop the near-misses entirely — the bus
            # stops named AFTER this mall also match the name and would otherwise be
            # reported as "the gym".
            best = min(r for r, _ in out)
            if pattern != (spec.get("match_names") or [None])[0]:
                print(f"    (matched OSM name {pattern!r}, not {spec['query']!r})")
            return [p for r, p in out if r == best][:5]
    return []


# ---------------------------------------------------------------------------
# GTFS
# ---------------------------------------------------------------------------
def _download_gtfs() -> bool:
    """Stream the feed to config.GTFS_CACHE_PATH. True on success."""
    print(f"downloading GTFS from {config.GTFS_URL} …")
    try:
        with requests.get(config.GTFS_URL, stream=True, timeout=120,
                          headers={"User-Agent": config.NOMINATIM_USER_AGENT}) as r:
            r.raise_for_status()
            total = 0
            with open(config.GTFS_CACHE_PATH, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
                    total += len(chunk)
                    print(f"\r  {total / 1e6:.0f} MB", end="", flush=True)
        print(f"\r  {total / 1e6:.0f} MB — saved to {config.GTFS_CACHE_PATH}")
        return True
    except Exception as exc:
        print(f"\n  download failed: {type(exc).__name__}: {exc}")
        return False


def _rows(zf: zipfile.ZipFile, name: str):
    """Stream one GTFS table out of the zip as dicts, without extracting it."""
    with zf.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


def _dep_hour(hhmmss: str) -> Optional[int]:
    """GTFS hours run past 24 for after-midnight trips ("25:10:00" = 01:10)."""
    try:
        h = int(hhmmss.split(":", 1)[0])
    except Exception:
        return None
    return h - 24 if h >= 24 else h


def _headway_min(departure_hours: list) -> Optional[int]:
    """Average minutes between buses inside the daytime window, or None if the stop
    isn't served in it. Reported as "a bus every ~N minutes"."""
    start, end = config.AMENITY_HEADWAY_WINDOW
    n = sum(1 for h in departure_hours if h is not None and start <= h < end)
    if n <= 0:
        return None
    return max(1, round((end - start) * 60 / n))


def _active_services(zf: zipfile.ZipFile) -> set:
    """service_ids running on config.GTFS_WEEKDAY and not already expired. The feed
    is republished weekly and carries past periods, so the end_date check keeps a
    retired timetable from inflating the frequencies."""
    if "calendar.txt" not in zf.namelist():
        # Some GTFS publishers use only calendar_dates.txt. Rather than crash, count
        # every service — frequencies then read a little high, which is far better
        # than the loader failing outright.
        print("  ! no calendar.txt in the feed — counting all services")
        return {t["service_id"] for t in _rows(zf, "trips.txt")}
    col = _WEEKDAY_COL[config.GTFS_WEEKDAY % 7]
    today = date.today()
    out = set()
    for row in _rows(zf, "calendar.txt"):
        if row.get(col) != "1":
            continue
        try:
            if datetime.strptime(row["end_date"], "%Y%m%d").date() < today:
                continue
        except Exception:
            pass                                   # unparseable date -> keep it
        out.add(row["service_id"])
    return out


def _bs_stops(zf: zipfile.ZipFile) -> dict:
    """{stop_id: {name, lat, lon}} for stops inside the Be'er Sheva box. Listings are
    all in that box, so a stop outside it can never be the nearest one — filtering
    here is what keeps the stop_times pass cheap."""
    la0, lo0, la1, lo1 = geocode._bs_bounds()
    out = {}
    for row in _rows(zf, "stops.txt"):
        try:
            lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
        except Exception:
            continue
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            out[row["stop_id"]] = {"name": row.get("stop_name", ""), "lat": lat, "lon": lon}
    return out


def _station_anchor(stops: dict):
    """(lat, lon) of the train station, taken from the FEED (the stop named
    config.TRAIN_STATION["name_match"]) and falling back to the configured
    coordinate. Using the feed's own point avoids a hand-typed coordinate quietly
    skewing which stops count as "at the station"."""
    st = config.TRAIN_STATION
    match = st.get("name_match")
    hits = [s for s in stops.values() if match and match in s["name"]]
    if hits:
        return hits[0]["lat"], hits[0]["lon"], hits[0]["name"]
    return st["lat"], st["lon"], st["name"] + " (configured)"


def _station_stop_ids(stops: dict) -> set:
    """Stop ids that count as "the train station".

    It has to be a radius, not just the rail stop: buses don't serve the railway
    platform itself, they serve the adjacent central bus station. But the radius is
    kept tight (config.TRAIN_STATION_RADIUS_M) and centred on the feed's real rail
    coordinate — a loose one starts swallowing unrelated stops nearby (the mall's
    stop is ~300 m away) and would credit buses that never go near the train."""
    lat, lon, _name = _station_anchor(stops)
    return {sid for sid, s in stops.items()
            if geocode._haversine_m(s["lat"], s["lon"], lat, lon)
            <= config.TRAIN_STATION_RADIUS_M}


def _scan_trips(zf: zipfile.ZipFile, stops: dict, services: set):
    """One streaming pass over stop_times.txt, keeping only rows at Be'er Sheva stops
    on trips that actually run on the chosen weekday.

    Returns (trip_meta, by_trip) where
      trip_meta[trip_id] = (route_short_name, direction_id)
      by_trip[trip_id]   = [(stop_sequence, stop_id, departure_hour), …]
    """
    route_name = {r["route_id"]: (r.get("route_short_name") or "").strip()
                  for r in _rows(zf, "routes.txt")}
    trip_meta = {}
    for t in _rows(zf, "trips.txt"):
        if t["service_id"] in services:
            trip_meta[t["trip_id"]] = (route_name.get(t["route_id"], ""),
                                       t.get("direction_id", "0"))
    print(f"  {len(trip_meta):,} trips run on {_WEEKDAY_COL[config.GTFS_WEEKDAY % 7]}")

    by_trip = defaultdict(list)
    kept = seen = 0
    with zf.open("stop_times.txt") as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        header = next(reader)
        i_trip = header.index("trip_id")
        i_stop = header.index("stop_id")
        i_seq = header.index("stop_sequence")
        i_dep = header.index("departure_time")
        for row in reader:
            seen += 1
            if seen % 2_000_000 == 0:
                print(f"\r  stop_times: {seen:,} rows scanned, {kept:,} kept",
                      end="", flush=True)
            try:
                sid = row[i_stop]
                if sid not in stops:
                    continue
                tid = row[i_trip]
                if tid not in trip_meta:
                    continue
                by_trip[tid].append((int(row[i_seq]), sid, _dep_hour(row[i_dep])))
                kept += 1
            except Exception:
                continue
    print(f"\r  stop_times: {seen:,} rows scanned, {kept:,} kept at BS stops")
    return trip_meta, by_trip


def build_route_target(spec: dict, stops: dict, trip_meta: dict, by_trip: dict) -> dict:
    """kind "bus_route": every BS stop served by the named line, PER DIRECTION, with
    how often it runs there. Both directions are kept deliberately — a stop that only
    takes you one way is half a bus line."""
    want = spec["route"]
    hours = defaultdict(list)                       # (stop_id, direction) -> [hour, …]
    for tid, (short, direction) in trip_meta.items():
        if short != want:
            continue
        for _seq, sid, hour in by_trip.get(tid, ()):
            hours[(sid, direction)].append(hour)

    rows = []
    for (sid, direction), hh in hours.items():
        s = stops[sid]
        rows.append({"name": s["name"], "lat": s["lat"], "lon": s["lon"],
                     "direction_id": direction, "route": want,
                     "headway_min": _headway_min(hh)})
    # Prefer the street the user actually uses; fall back to every stop on the line
    # rather than reporting nothing if the stop names are spelled differently.
    street = spec.get("street")
    if street:
        on_street = [r for r in rows if street in r["name"]]
        if on_street:
            rows = on_street
        else:
            print(f"  ! no {want} stop name contains {street!r} — keeping all {len(rows)}")
    return {"label": spec.get("label", want), "icon": spec.get("icon", ""),
            "kind": spec["kind"], "stops": rows}


def build_toward_target(spec: dict, stops: dict, trip_meta: dict, by_trip: dict,
                        station_ids: set) -> dict:
    """kind "bus_toward": stops from which a bus actually HEADS FOR the station.

    The direction rule: within a trip, the station appears at some stop_sequence k;
    only stops earlier in that same trip (sequence < k) are "toward" it. Without this
    the result would also list stops the bus reaches *after* the station, which point
    the wrong way."""
    hours = defaultdict(list)                       # (stop_id, route) -> [hour, …]
    for tid, (short, _direction) in trip_meta.items():
        seq_rows = by_trip.get(tid)
        if not seq_rows or not short:
            continue
        station_seq = min((seq for seq, sid, _h in seq_rows if sid in station_ids),
                          default=None)
        if station_seq is None:
            continue
        for seq, sid, hour in seq_rows:
            if seq < station_seq and sid not in station_ids:
                hours[(sid, short)].append(hour)

    # One row per stop: the line that serves it most often is the one worth naming.
    best: dict = {}
    for (sid, route), hh in hours.items():
        hw = _headway_min(hh)
        if hw is None:
            continue
        cur = best.get(sid)
        if cur is None or hw < cur["headway_min"]:
            s = stops[sid]
            best[sid] = {"name": s["name"], "lat": s["lat"], "lon": s["lon"],
                         "route": route, "headway_min": hw}
    return {"label": spec.get("label", "לרכבת"), "icon": spec.get("icon", ""),
            "kind": spec["kind"], "stops": list(best.values())}


def build_transit_targets() -> dict:
    """All GTFS-derived targets in config.AMENITY_TARGETS, from the cached zip."""
    if not config.GTFS_CACHE_PATH.exists():
        print(f"  no GTFS zip at {config.GTFS_CACHE_PATH} — skipping transit targets")
        return {}
    specs = [s for s in config.AMENITY_TARGETS if s["kind"] in ("bus_route", "bus_toward")]
    if not specs:
        return {}
    with zipfile.ZipFile(config.GTFS_CACHE_PATH) as zf:
        services = _active_services(zf)
        stops = _bs_stops(zf)
        print(f"  {len(stops):,} stops inside the Be'er Sheva box")
        alat, alon, aname = _station_anchor(stops)
        station_ids = _station_stop_ids(stops)
        print(f"  station anchor: {aname} @ {alat:.5f},{alon:.5f} — "
              f"{len(station_ids)} stop(s) within {config.TRAIN_STATION_RADIUS_M} m")
        trip_meta, by_trip = _scan_trips(zf, stops, services)

    out = {}
    for spec in specs:
        if spec["kind"] == "bus_route":
            t = build_route_target(spec, stops, trip_meta, by_trip)
            dirs = {r["direction_id"] for r in t["stops"]}
            print(f"  {spec['key']}: {len(t['stops'])} stop(s), {len(dirs)} direction(s)")
        else:
            if not station_ids:
                print(f"  ! {spec['key']}: the train station wasn't found in the feed")
                continue
            t = build_toward_target(spec, stops, trip_meta, by_trip, station_ids)
            print(f"  {spec['key']}: {len(t['stops'])} stop(s) with a bus to the station")
        if not t["stops"]:
            # Same rule as the places: never overwrite good data with an empty result
            # (a renamed line or a truncated feed shouldn't erase what we had).
            print(f"  ! {spec['key']}: resolved nothing — keeping the existing entry")
            continue
        out[spec["key"]] = t
    return out


def build_poi_targets() -> dict:
    """Only targets we actually resolved. A target that came back empty is OMITTED,
    never returned with an empty point list — main() merges this over the existing
    file, so emitting {} would let one bad Overpass day silently wipe good data."""
    out = {}
    for spec in config.AMENITY_TARGETS:
        if spec["kind"] != "poi":
            continue
        pts = _poi_points(spec)
        if not pts:
            print(f"  ! {spec['key']}: nothing resolved for {spec['query']!r} "
                  f"— keeping whatever amenities.json already has")
            continue
        print(f"  {spec['key']}: {len(pts)} place(s) for {spec['query']!r}"
              f" — {pts[0]['name']}")
        out[spec["key"]] = {"label": spec.get("label", spec["query"]),
                            "icon": spec.get("icon", ""), "kind": "poi", "points": pts}
    return out


def _load_existing() -> dict:
    try:
        return json.loads(OUT_PATH.read_text(encoding="utf-8")).get("targets", {})
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-download", action="store_true",
                    help="reuse the cached GTFS zip instead of downloading it again")
    ap.add_argument("--poi-only", action="store_true",
                    help="refresh only the Overpass places (no GTFS at all)")
    args = ap.parse_args()

    targets = _load_existing()                     # keep whatever we're not rebuilding

    if not args.poi_only:
        if not args.skip_download and not _download_gtfs():
            if not config.GTFS_CACHE_PATH.exists():
                print("no GTFS data available — transit targets left unchanged")
            else:
                print("using the previously cached zip")
        print("building transit targets …")
        targets.update(build_transit_targets())

    print("resolving places via Overpass …")
    targets.update(build_poi_targets())

    if not targets:
        print("nothing resolved — leaving amenities.json alone")
        return 1
    OUT_PATH.write_text(json.dumps({"generated": datetime.now().isoformat(timespec="seconds"),
                                    "targets": targets}, ensure_ascii=False),
                        encoding="utf-8")
    print(f"wrote {OUT_PATH}: " + ", ".join(
        f"{k}={len(v.get('stops') or v.get('points') or [])}" for k, v in targets.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
