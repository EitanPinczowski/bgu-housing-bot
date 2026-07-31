"""
"What is daily life like at this address?" — walking time from a listing to the
transit and places that matter (config.AMENITY_TARGETS), using the precomputed
`amenities.json` (see load_amenities.py) plus the local OSRM foot router.

DISPLAY ONLY. Nothing here feeds the fit score; it is extra context on an alert.
That is also why every failure path returns {} instead of raising: a missing data
file, a stopped OSRM container, or a listing with nothing in range must leave the
rest of the pipeline behaving exactly as it did before this module existed.
"""
from __future__ import annotations
import json
from typing import Optional

import config
import osrm
from zones import _haversine_m

_CACHE_PATH = config.DATA_DIR / "amenity_cache.json"
_data: Optional[dict] = None
_cache: Optional[dict] = None


def _load_data() -> dict:
    """amenities.json, read once per process. Missing/corrupt -> {} (feature off)."""
    global _data
    if _data is None:
        try:
            _data = json.loads(config.AMENITIES_PATH.read_text(encoding="utf-8"))
        except Exception:
            _data = {}
    return _data


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        _CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _points(target: dict) -> list:
    """The candidate destinations of a target, whatever its kind."""
    return target.get("stops") or target.get("points") or []


def nearby(lat: Optional[float], lon: Optional[float]) -> dict:
    """{target_key: {label, icon, kind, options: [...]}} for whatever resolved.

    `options` is normally one entry (the nearest stop/place); for a bus_route target
    it is the nearest stop PER DIRECTION, because a stop that only takes you one way
    is half a bus line. Each option carries at least {minutes, name} and, for transit,
    {route, headway_min}.

    Returns {} — never raises — when the data file is missing, OSRM is down, or
    nothing is within config.AMENITY_MAX_METERS.
    """
    try:
        return _nearby(lat, lon)
    except Exception:
        return {}


def _pick(opts: list) -> dict:
    """One winner from the candidates in a slot.

    Not simply the closest: among stops within config.AMENITY_DETOUR_MINUTES of the
    nearest, the most FREQUENT one wins. Two minutes of extra walking for a bus every
    10 minutes instead of every 36 is the answer a person actually wants — and the
    nearest-only rule really did report the 36-minute line when the good one was six
    metres further away. Options with no frequency (places) sort as infinite, so for
    them this degrades to plain nearest."""
    nearest = min(o["minutes"] for o in opts)
    close = [o for o in opts if o["minutes"] <= nearest + config.AMENITY_DETOUR_MINUTES]
    return min(close, key=lambda o: (o.get("headway_min") or float("inf"), o["minutes"]))


def _nearby(lat, lon) -> dict:
    if lat is None or lon is None:
        return {}
    targets = _load_data().get("targets") or {}
    if not targets:
        return {}

    cache = _load_cache()
    key = f"{round(lat, 4)},{round(lon, 4)}"
    if key in cache:
        return cache[key]

    # Straight-line pre-filter first: it costs nothing and keeps the routing table
    # small (a whole city of stops would otherwise go into one OSRM request). The
    # radius is per-target — a bus stop 3 km away is irrelevant, the one gym is not.
    specs = {s["key"]: s for s in config.AMENITY_TARGETS}
    cands = []                                     # (target_key, point)
    for tkey, target in targets.items():
        limit = specs.get(tkey, {}).get("max_meters", config.AMENITY_MAX_METERS)
        for p in _points(target):
            if _haversine_m(lat, lon, p["lat"], p["lon"]) <= limit:
                cands.append((tkey, p))
    if not cands:
        return {}
    if not osrm.alive():
        return {}                                   # don't cache — OSRM may come back

    mins = osrm.table_minutes(lat, lon, [(p["lat"], p["lon"]) for _, p in cands])
    if not mins:
        return {}                                   # server couldn't answer — don't cache

    # Group by (target, direction) — direction is None for anything but a bus_route,
    # so those collapse to a single slot — then pick one winner per slot.
    slots: dict = {}
    for (tkey, p), minutes in zip(cands, mins):
        if minutes is None:
            continue
        kind = targets[tkey].get("kind")
        slot = (tkey, p.get("direction_id") if kind == "bus_route" else None)
        opt = {"minutes": round(minutes, 1), "name": p.get("name", "")}
        for extra in ("route", "headway_min", "direction_id"):
            if p.get(extra) is not None:
                opt[extra] = p[extra]
        slots.setdefault(slot, []).append(opt)

    best = {slot: _pick(opts) for slot, opts in slots.items()}
    out: dict = {}
    for (tkey, _direction), opt in sorted(best.items(), key=lambda kv: kv[1]["minutes"]):
        t = targets[tkey]
        out.setdefault(tkey, {"label": t.get("label", tkey), "icon": t.get("icon", ""),
                              "kind": t.get("kind", ""), "options": []})
        out[tkey]["options"].append(opt)
    # Keep config order so the line reads the same way every time.
    ordered = {s["key"]: out[s["key"]] for s in config.AMENITY_TARGETS if s["key"] in out}

    cache[key] = ordered
    _save_cache()
    return ordered


def locate(am: Optional[dict], lat: Optional[float], lon: Optional[float]) -> list:
    """Where a listing's OWN amenities are, as
    [{icon, label, name, lat, lon, minutes, route, headway_min}, …].

    The stored per-listing amenity blob keeps the stop NAME and the walk time but not
    the coordinate, so the map had nothing to point at. Rather than recomputing 292
    listings (an OSRM table call each) just to add two numbers, the name is looked up
    against amenities.json here — the same file the blob came from. Where a name is
    shared by several stops (both directions of one junction), the one nearest the
    listing is the one it was measured against.

    Returns [] on anything missing, exactly like nearby()."""
    if not am or lat is None or lon is None:
        return []
    try:
        targets = _load_data().get("targets") or {}
        out = []
        for tkey, t in am.items():
            pts = _points(targets.get(tkey) or {})
            by_name = {}
            for p in pts:
                by_name.setdefault(p.get("name", ""), []).append(p)
            for opt in t.get("options") or []:
                cands = by_name.get(opt.get("name", ""))
                if not cands:
                    continue
                # Both directions of a junction share one name, so name alone would
                # put the "there" and "back" pins on the same spot — which is exactly
                # the distinction a bus_route target exists to make.
                if opt.get("direction_id") is not None:
                    same_dir = [q for q in cands
                                if q.get("direction_id") == opt["direction_id"]]
                    cands = same_dir or cands
                p = min(cands, key=lambda q: _haversine_m(lat, lon, q["lat"], q["lon"]))
                out.append({"icon": t.get("icon", ""), "label": t.get("label", tkey),
                            "name": opt.get("name", ""), "lat": p["lat"], "lon": p["lon"],
                            "minutes": opt.get("minutes"), "route": opt.get("route"),
                            "headway_min": opt.get("headway_min")})
        return out
    except Exception:
        return []


def describe(am: Optional[dict]) -> list:
    """Plain-text fragments for display, e.g.
        ["🚌 669 מרגר · 6 דק׳ (כל ~20 דק׳) ↔ 8 דק׳",
         "🚆 לרכבת מרכז · קו 12 · 4 דק׳ (כל ~15 דק׳)",
         "🏋️ חדר כושר עזריאלי · 14 דק׳"]
    Unescaped on purpose — the caller owns its own escaping (MarkdownV2 in notifier).
    Empty/absent data yields [] so nothing is printed at all: no "unknown" noise."""
    if not am:
        return []
    out = []
    for t in am.values():
        opts = t.get("options") or []
        if not opts:
            continue
        icon = (t.get("icon") or "").strip()
        head = f"{icon} {t['label']}".strip()
        route = opts[0].get("route")
        if route and t.get("kind") == "bus_toward":
            head += f" · קו {route}"
        legs = []
        for o in opts:
            leg = f"{o['minutes']:.0f} דק׳"
            if o.get("headway_min"):
                leg += f" (כל ~{o['headway_min']} דק׳)"
            legs.append(leg)
        out.append(f"{head} · " + " ↔ ".join(legs))
    return out
