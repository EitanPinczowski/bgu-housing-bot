---
name: rebuild-data
description: >
  Rebuild a generated data file — street/house anchors, buildings, neighborhoods, area
  features, amenities, landmarks, the green zone. Use for "regenerate house_anchors",
  "rebuild buildings.json", "reload the amenities", "the zone changed", "run the loaders",
  "Overpass is down", or when a *.json in the repo root is stale or missing.
---

# Rebuilding a generated data file

Ten loaders, ten outputs. **Each has a precondition that is not checked for you** — a
loader whose source is unavailable can write a thin or empty file over a good one.

| loader | writes | needs |
|---|---|---|
| `load_osm_addresses.py` | `house_anchors.json` | the **PBF** at `C:\osrm\israel-and-palestine-latest.osm.pbf` |
| `load_house_numbers.py` | `house_anchors.json` | Overpass — **the fallback, not the source** |
| `load_osm_buildings.py` | `buildings.json` | the same PBF |
| `load_area_features.py` | `area_features.json` | Overpass |
| `load_neighborhoods.py` | `neighborhoods.json` | Overpass — ⚠️ see below |
| `load_map_neighborhoods.py` | `map_neighborhoods.json` | Overpass |
| `load_boundary_streets.py` | `boundary_streets.json` | Overpass |
| `load_amenities.py` | `amenities.json` | MOT **GTFS** feed + Overpass |
| `load_landmarks_from_kmz.py` | `landmarks.json` | a fresh **KMZ** export from My Maps |
| `load_zone_from_kmz.py` | `green_zone.json` | a fresh **KMZ** export from My Maps |

## ⚠️ The trap: two neighborhood files that are not interchangeable

`zones.in_allowed_neighborhood` passes a point inside **any** polygon in
`neighborhoods.json`. Adding a quarter there to *label the map* would silently widen the
ב/ג/ד gate and let flats through that the rules mean to drop.

**Display-only outlines go in `map_neighborhoods.json`.** A guard in `test_zones.py`
proves the two stay separate. Do not merge them to save a file.

## The PBF is the source; Overpass is the fallback

`load_osm_addresses.py` reads the extract **already on disk for OSRM**. Overpass was down
on all four mirrors for a whole day, which is why the anchors were thin — that is the
lost dependency this exists to remove.

The old Overpass query required `addr:street`, so it structurally missed the 99 Be'er
Sheva buildings tagged with a house number and no street. Those are now bound to the
nearest centreline within 40 m and **dropped beyond it** rather than guessed. 811 → 998
anchors, 97 → 115 usable streets.

**Measured caveat:** this converted only **1** of the 105 stranded listings — the extra
anchors landed on streets that already had them. Keep it for the data and the lost
dependency, not as the fix. To place stranded listings, use the `fix-location` skill.

## After a rebuild

    python geo_accuracy.py      # anchors/buildings changed -> re-measure (geo-verify skill)
    python doctor.py            # the `data files` row checks they all exist and parse
    python replay.py            # preview; then --apply (apply-replay skill)

**Restart `serve_dashboard.py`.** It pins its data at process start — one had been up
22 h serving anchors loaded before `govmap_anchors.json` existed, while looking healthy.

## Things that will not help — measured

- **Building-COUNT interpolation does not work**: 19.0 m vs 18.4 m, and worse as the
  tolerance loosens. Sheds and stairwells are footprints too. Don't rebuild it.
- `load_area_features.py` **still imports railway features**, which is how
  `תחנת רכבת צפון - אוניברסיטה` got into the street index and made `האוניברסיטה`
  canonicalise to a station 783 m away. `streets._NOT_STREETS` names it explicitly; the
  durable fix is to stop importing them here, and it needs an Overpass run.
- Amenity data is **display-only and never scored** — an explicit user decision. Do not
  quietly turn a walk time into a scoring factor.
