---
name: geo-verify
description: >
  Measure whether a geocoding change actually made placement better. Use after editing
  geocode.py, streets.py, the anchors, interpolation, snapping, or the static table — or
  for "did that improve accuracy", "check the geocoder", "run geo_accuracy", "audit the
  geocode", "are the dots in the right place".
---

# Verifying a geocoding change

Three tools, three different questions. Run the first after **any** geocoding change.

## 1. How far off are we? — `geo_accuracy.py`

    python geo_accuracy.py

**The only thing that makes "more accurate" a fact.** It holds out each of N addresses
OSM knows exactly, hides its anchor, asks the geocoder, and reports error in metres per
tier.

**Without the hold-out it grades itself against its own answer key.**

Reference numbers — baseline → after extrapolation: p50 **52→43 m**, p90 **192→170 m**,
worst **3528→2840 m**, imprecise tier **34→15**. End to end the geocoder has gone p50
38 → 14 m, p90 146 → 99 m.

## 2. Is the point ON its street? — `audit_geocode.py`

    python audit_geocode.py

A different question from "how far off". Median offset should be ~6 m. This is what
catches a point that is precise but on the wrong road — the failure `_plausible_external`
exists for (a point >250 m from the street the address names is a blunder, not
imprecision).

## 3. Did it help the objective? — `unique_report.py`

    python unique_report.py

Scores what the work is actually for: how many distinct (street, number) addresses have a
point **to themselves**. 85 → 111 of 142 after the govmap seed.

**The ceiling is not the listing count.** 196 of 410 listings give no house number and can
never be unique honestly.

## Then re-classify

    python replay.py            # preview the diff — read it
    python replay.py --apply    # see the apply-replay skill for the preconditions

## Traps that these tools exist because of

- **The cache cannot be shrunk by more than half** (`_save_cache`). It was wiped from ~300
  entries to 1 twice in one day: any process holding a small `_cache` — a hold-out harness
  using it as scratch, a long-lived server whose copy predates a rebuild — saves once and
  the small dict lands on disk. Recovery is a 35-minute re-geocode. **A geo_accuracy run
  is exactly such a process**, which is why the guard is there.
- **Use `warm_cache.py`, not `replay.py`, to rebuild the cache** — minutes over ~340
  listing addresses instead of an hour over 3,680 archived posts.
- **Restart `serve_dashboard.py` afterwards.** A long-lived server pins the code and the
  anchors at process start; one had been up 22 h serving the previous day's `geocode.py`
  while looking healthy. `doctor`'s `dashboard` row FAILs on this.
- **Only a replay diff catches a regression** like "near the university" resolving to the
  railway station 783 m away. Always read it before `--apply`.

The reference notes on how placement actually works — anchors, pooling, interpolation,
the no-housing mask, landmark grading — are in the `geocoding-notes` skill.
