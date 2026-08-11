---
name: dead-ends
description: >
  Ideas that were tried, measured, and rejected — with the numbers. Load BEFORE proposing
  any of: batching LLM calls to save quota, a cheap pre-LLM text gate, local Ollama
  triage, Yad2 or another second source, building-count interpolation, govmap POI/landmark
  lookups, order-insensitive street matching, deriving an area's size from street
  centroids, copying an address between a landlord's listings, or rebalancing the scrape
  schedule to cut detection lag.
---

# Measured dead ends — do not retry without NEW evidence

Each of these looks like a good idea. Each was built or measured, and each cost more than
it saved. The numbers are here so the question does not get reopened on a hunch.

## Saving LLM quota

**Batching (`llm.extract_many`) is built and MEASURED TO HARM — it stays OFF**
(`LLM_BATCH_SIZE = 1`). 5 posts per request would have cut ~865 calls/day to ~175, and it
still fails its accuracy gate:

| field | single vs single (noise floor) | batched 5 |
|---|---|---|
| `is_apartment_ad` | 100% | 100% |
| `price_per_room_ils` | 100% | **80%** |
| `available_rooms_count` | 100% | **70%** |
| `street_address_or_neighborhood` | 85% | **70%** |

Price and rooms agree PERFECTLY call-to-call, so their drop is batching, not model
variance — and they are the fields the hard filters run on. Retrying at 2 or 3 trades a
smaller saving for the same class of loss.

**A CHEAP PRE-LLM TEXT GATE IS A MEASURED DEAD END — do not retry** (2026-08-06, over all
6,939 archived posts):

| rule | would skip | MATCH/NEEDS_DATA LOST |
|---|---|---|
| text < 40 chars | 192 (2.8%) | **35** |
| text < 120 chars | 863 (12.4%) | **92** |
| says `מחפש/ת דירה` (a wanted ad) | 761 (11.0%) | **19** |
| no housing word at all | 267 (3.8%) | **47** |

**The cause is the OCR path, and it is structural.** 57–68% of the listings each gate
would lose are IMAGE posts — the ad text is in the picture, so `raw_text` is short and
keyword-free *by definition*. Any gate that judges a post by its TEXT throws those away.

**A local Ollama "is this an ad" triage is a MEASURED DEAD END — do not retry.**
`gemma2:9b` is 11/12 correct but **25.4 s median per post** (≈106 min added per run);
`gemma2:2b` is 6.6 s but **7/12 correct**, i.e. it discards real listings.

None of it is needed: the model ladder took capacity to ~1,000/day against ~700–870
demand.

## Geocoding

- **Building-COUNT interpolation does not work** — 19.0 m vs 18.4 m, worse as the
  tolerance loosens. Sheds and stairwells are footprints too.
- **MEASURED DEAD END, DO NOT REPEAT: deriving an area's size from the STREET CENTROIDS
  that co-occur with it in addresses** gave ~680 m for `הבלוק`, which is really 85 × 96 m.
  `אברהם אבינו` is long and its midpoint lies well outside the part inside הבלוק. **Only a
  survey answers this.**
- **POI/landmark lookups are a TRAP — measured, do not retry.** govmap "resolved" 30 of 41
  landmark strings, almost all fuzzy substitutions to a different place:
  `בניין הסטודנטים-אוקספורד` → a building-supplies shop. With no house number there is
  nothing to validate against.
- **The LLM is not losing house numbers** — 0 of the imprecise listings had one
  recoverable from the archived post text. **Post text / comments carry no recoverable
  house number**: 0 from comments over all 196 listings without one.
- **Nominatim has no house numbers here** (`addresstype=road`, `place_rank=26` for every
  numbered address, so grading its hits `street` is correct).
- **order-insensitive street matching recovers 1 listing**, not worth the ambiguity risk.
- **copying an address between one landlord's listings would inject errors** — a single
  broker had flats on four different streets.
- `צקלג`/`פארן`/`יוטבתה`/`יודפת` are **absent from OSM entirely**, so no free source will
  ever place them.
- **Free alternatives to govmap: all checked 2026-08-01, all dead.** Nominatim / Photon /
  LocationIQ / Geoapify / Pelias serve the same OSM data already in our PBF.
- **122 of the 227 imprecise listings give no house number at all.** Do not invent
  positions for them.

## Sources

**Facebook is the ONLY source.** Yad2 was evaluated and rejected: every endpoint sits
behind Radware Bot Manager, so the only ways in are CAPTCHA-solving or detection evasion —
forbidden — and it would risk the **home IP the FB scraper depends on**, for Yad2's
whole-flat/broker inventory rather than the שותפים market this bot targets. If a second
source is ever revisited, the legitimate route is Yad2's own saved-search **emails**.

## Scheduling

**The lag is lost runs, not cadence. Do not rebalance the schedule.** 20 of 42 scheduled
full runs completed in the 7 days to 08-05, with 17 slots LOST to a held lock — and the
lock repairs landed *after* almost all of that data. A slot that never runs cannot be
fixed by moving the slots around. Re-measure over clean days first.

## Refusing the mixed-parity fallback in `_anchors_for` (tried and reverted 2026-08-11)

**The bug is real. Both attempts to detect it were wrong, and the cure was broader than
the disease.**

`_anchors_for` prefers same-parity anchors and falls back to ALL anchors when they do not
bracket the number. On `שמעון בר גיורא` the odd numbers sit ~200 m from the even ones, and
number **26** has no even anchor above 24 — so it fell back, was bracketed between odd
**25 and 27** on the far arm, and landed 200 m out. That turned a **RED flat GREEN**, which
is the one error class this project treats as worse than not placing at all. Even **24**
was two numbers away and **6 m** from the truth.

Two detectors were written and both failed, in ways worth not repeating:

1. **Centroid distance between the parities** flagged **75 of 347 streets**, including
   `ארלוזורוב` (56 anchors), which is one ordinary road. A long street commonly carries
   odds 1..99 and evens 2..40, so the centroids sit hundreds of metres apart ALONG the
   road while being metres apart across it.
2. **Perpendicular separation only** (using `_street_axis`'s along-coordinate to pick the
   across-coordinate) cut it to 39 of 347 — and **did not flag `שמעון בר גיורא` at all**,
   the street it was written for, because that street's two arms diverge ALONG the axis
   rather than across it.

It also took the test suite from **64 s to 169 s**: declining to interpolate pushes
addresses down to extrapolation, `_nearest_anchor_point` and the external tiers.

**Reverted.** Changing placement on 39 streets to fix 2 addresses of 250, on a metric
redesigned twice in one sitting, is not a trade worth shipping.

**The narrow direction was then taken, and it worked** (`geocode._same_parity_neighbour`,
same day). When the same-parity anchors cannot bracket `n` but one sits within
`NEIGHBOUR_MAX_NUMBERS`, that anchor answers — graded `anchor_neighbour` → "street",
because it is the house next door and the confidence should say so. It needs no threshold
and no notion of "two roads": it asks only whether the right side of the street can answer
without help. Measured over the pinned 250:

    p50 12m -> 10m     p90 101m -> 66m     max 436m     wrong tier or unplaced 29 -> 27

Both RED → GREEN errors gone. 32 addresses better, 14 worse — the 14 are cases where
mixing parities happened to be fine (`חיים ברלב 4`: 0 m → 50 m), which is the accepted
cost of never bracketing across two arms of a road.

**It had to go above `place_house`, not inside `interpolate_house`.** Making interpolation
decline just hands the number to the extrapolation branch below it, which projects from
`anchors[0]`/`anchors[-1]` across BOTH parities — with odds running to 39 it would have
projected number 26 from anchor 1.

## Deliberate no-changes

Not dead ends — decisions to leave something alone, recorded so they are not reopened on
the same evidence:

- **`MIN_ALERT_SCORE` (75) was audited 2026-08-05 and deliberately left alone.** It lets
  the top **45%** of MATCH rows through. The distribution is **smooth across 75**, so
  there is no valley to snap to and any new value would be as arbitrary. The only evidence
  that could justify moving it is which flats the group actually stars — and that is
  **n=3** against 191 MATCHes. **Do not retune this on the score shape alone.**

## Before adding to this list

Read `evidence-rules`. A dead end is only worth recording with the measurement, its date,
and the sample size that makes it binding.
