---
name: fix-location
description: >
  Place a listing that has no dot, or correct one sitting in the wrong spot. Use for "this
  flat is on the wrong street", "the dot is in the middle of nowhere", "why is this
  listing unplaced", "pin this address", "add a landmark", "seed anchors", or working
  through the unknown-locations / imprecise queue.
---

# Fixing where a listing sits

Five mechanisms, in **strict precedence**. Pick by what the post actually gives you —
using a broader one when a narrower fits is how a flat ends up on an area centroid.

## The precedence, and why it is this way

`_load_anchors()` merges: **OSM survey → govmap fills only MISSING keys → user pins
override both.** govmap can never degrade a surveyed point or a 📍 fix.

| the post gives you | use | result |
|---|---|---|
| a house number on a known street | nothing — arithmetic already places it | ~13 m |
| a house number, street has <2 anchors | `seed_anchors.py --exact` | ~5.4 m |
| a street we cannot resolve at all | `seed_anchors.py --unresolved` | placed 11 of 14 |
| a named building / quarter | a surveyed landmark in `landmarks.json` | graded by its drawn extent |
| nothing resolvable | 📍 pin on the dashboard | authoritative, survives replay |

## 1. Seed anchors from govmap (bulk, and the usual answer)

    python seed_anchors.py --exact
    python seed_anchors.py --unresolved

**The anchors are bought once and then owned.** After a run the live pipeline never
touches govmap again — every future listing on those streets is placed by local
arithmetic. One run measured 549 requests, 9 min, 838 anchors on 127 streets.

- **Select by unplaceable LISTINGS, not anchor count** (`stranded_streets`). Two anchors
  in the wrong place buy nothing: `אלכסנדר ינאי` was anchored 8–14 with every listing at
  17–32.
- **`--exact` beats interpolating**: 5.4 m vs 13 m. For the ~142 addresses this bot has
  listings at there is no reason to compute what can be looked up.
- **`main.py` / `pipeline.py` / `replay.py` must NEVER import `govmap`** — it is the
  site's own internal endpoint, undocumented and free to change.
- **POI/landmark lookups are a measured trap.** govmap "resolved" 30 of 41 landmark
  strings but almost all are fuzzy substitutions to a different place:
  `בניין הסטודנטים-אוקספורד` → a building-supplies shop. With no house number there is
  nothing to validate against.

## 2. A 📍 pin on the dashboard (one flat, or one address)

Run `serve_dashboard.py` — pinning needs the live server, not the published snapshot.

- **"this listing"** → `storage.manual_locations`, keyed by dedup_key, preferred by
  `pipeline._classify`, so it survives `replay --apply`. Registered as `exact`.
- **"this address"** → `geocode.add_pin` + `uncache`, which fixes every current and
  future listing there.
- **A pin on a numbered address becomes a street ANCHOR** (`user_anchors.json`) and fixes
  the whole street. It is the only mechanism that can ever place a house on the ~18
  streets with no OSM addresses — the numbering origin is not derivable from free data
  ("low numbers nearer the centre" holds for only 64% of streets, so guessing lands at
  the wrong END). Refused past 200 m from the street it claims.
- **🎯 guided pinning**: govmap PROPOSES, a person decides. The candidate draws as a
  dashed ring, never a dot. **Never auto-accept** — govmap substitutes silently
  (`בני אור 999` comes back as `בני אור 13`).

## 3. A surveyed landmark

Draw it in My Maps, export the KMZ, then `python load_landmarks_from_kmz.py`.

**A landmark is as precise as its survey says** — graded from the DRAWN EXTENT:

| measured diagonal | grade | example |
|---|---|---|
| ≤150 m | `static` (precise) | `הבלוק` 123, `מגדלי דוד` 115 |
| ≤400 m | `static_street` | `אביסרור` 299 |
| >400 m or unsurveyed | `static_area` | `שכונה ד` (2,375 m) |

**Only a survey answers this.** Deriving an area's size from the street centroids that
co-occur with it gave ~680 m for `הבלוק` (really 85 × 96 m) — that proxy is invalid.

## 4. The static table

`geocode.STATIC_TABLE` holds hand-pinned coordinates and grades them `exact`. Frequent
unresolvable names surface via `storage.unknown_locations` and the daily DM digest.

## When the honest answer is "nothing can place this"

- **122 of 227 imprecise listings give no house number at all.** Nothing can place those
  to a building. Do not invent a position.
- `צקלג` / `פארן` / `יוטבתה` / `יודפת` are **absent from OSM entirely**.
- **"Near X" is not an address.** `ליד האוניברסיטה` resolves to nothing, deliberately —
  the listing stays in the list and in search, it just stops claiming a position.
- **Nobody lives on the campus or in the hospital.** A coordinate landing inside those
  polygons is a data error every time and is rejected to NEEDS_DATA. Hand-placed points
  are exempt: if a human says a flat is on campus, they meant it.

## Measured dead ends — do not retry

- The **LLM is not losing house numbers** (0 recoverable from archived post text).
- **Post text and comments carry no recoverable house number** — 0 from comments over all
  196 listings without one.
- **Nominatim has no house numbers here** (`addresstype=road` for every numbered address).
- **Copying an address between one landlord's listings would inject errors** — a single
  broker had flats on four different streets.

Afterwards: run the `geo-verify` skill, then `replay.py`. Reference notes on how
placement works internally are in `geocoding-notes`.
