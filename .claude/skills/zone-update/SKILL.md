---
name: zone-update
description: >
  Change what counts as in-range — the hand-drawn green zone, the no-amber (שכונה ד')
  carve-outs, MAX_WALK_MINUTES, or the campus gates. Use for "update the zone", "I redrew
  the map", "import the KMZ", "change the walk limit", "this area should count", "widen
  the search area".
---

# Changing the zone

In-range is graded by `zones.classify_location` / `classify_effective`:

- **GREEN** — inside the hand-drawn polygon → preferred match (✅)
- **AMBER** — outside it but within `MAX_WALK_MINUTES` (20) walk of a **campus gate** → acceptable (🟡)
- **RED** — beyond that, or inside a `no_amber_zones.json` area like שכונה ד' but outside green → dropped
- **UNKNOWN** — couldn't geocode → NEEDS_DATA

## Redrawing the polygon

1. Edit the map in Google My Maps, export the **KMZ**.
2. `python load_zone_from_kmz.py` → rewrites `green_zone.json`.
3. `python replay.py` — read the diff.
4. `python replay.py --apply` — see the `apply-replay` skill for the two preconditions.

`no_amber_zones.json` holds the carve-outs. It is a **RULE, not a walk-time consequence**:
`קדש 26` flipped MATCH→DROP for exactly this reason in the 2026-08-02 replay. The dashboard
outlines it (`.noamber`) because otherwise a flat 7 minutes from a gate is RED with nothing
on screen to explain why.

## ⚠️ OSRM must be up before you apply

The AMBER boundary **is** a walk time. With OSRM down, a replay silently substitutes the
calibrated straight-line estimate and bakes it into every tier and score. `guard.py`
blocks `--apply` for this. If it is down, use the `osrm-docker` skill.

Note the two are allowed to disagree near the edge: the map's tier **band** is the
straight-line estimate, while a **dot's own tier uses OSRM** and is the more accurate one.
The legend says so.

## Changing MAX_WALK_MINUTES

One value in `config.py`, but it moves the AMBER boundary for every listing, and
`map_listings.walk_rings_svg` draws rings at 5/10/15/`MAX_WALK_MINUTES` from each gate
using the same arithmetic as `zones.est_walk_to_gate_min`. Change the config, then replay.

`BUFFER_METERS` is **deprecated** — the boundary is a walk time, not a radius.

## ⛔ Do not widen the gate by editing the wrong file

`zones.in_allowed_neighborhood` passes a point inside **any** polygon in
`neighborhoods.json`. Adding a quarter there to make it show on the map would silently
widen the ב/ג/ד gate.

Display-only outlines belong in `map_neighborhoods.json`
(`load_map_neighborhoods.py`). A guard in `test_zones.py` proves the two stay separate.

## Related rules that a zone change interacts with

- **Blacklisted neighborhoods** (Ramot, Neve Zeev, Nahal Ashan, Pelach 7) are a separate
  hard instant-drop applied **before** geocoding. Widening the zone does not un-blacklist
  them.
- **Nobody lives on the campus or in the hospital** — `zones.no_housing_here` rejects a
  coordinate landing inside those polygons. **The mask is safe because the polygon is
  tight**: measured, no street geometry runs inside the campus, and real perimeter
  addresses like `רגר 104` fall outside it. **Don't widen it to a bounding box.**
- A flat with **no location and nothing to recommend it** is dropped, and that gate is
  independent of the zone — see `MIN_SCORE_WITHOUT_ADDRESS` (50), which must stay below
  `MIN_ALERT_SCORE` (75) or every placeless listing good enough to alert about would be
  deleted before it could be.

## Afterwards

    python stats.py            # how did the funnel move?
    python area_map.py         # redraw the whole-area map

Restart `serve_dashboard.py` — it pins zone data at process start.
