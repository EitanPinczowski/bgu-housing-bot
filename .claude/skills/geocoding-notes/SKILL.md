---
name: geocoding-notes
description: >
  Reference notes on placing a listing: the static table, house-number interpolation and
  projection, street pooling, anchors, govmap seeding, the no-housing mask, landmark
  grading, and what may be accepted from an external geocoder. Load before editing
  geocode.py, streets.py, seed_anchors.py, govmap.py, or the load_osm_* loaders.
---

# Geocoding Notes

How a Hebrew address becomes a coordinate, and every rule learned by getting it wrong. To FIX one listing's location use `fix-location`; to MEASURE a change use `geo-verify`.

> Moved verbatim from `CLAUDE.md`. Do not reword in place — see the
> `write-a-note` skill.

- **A FLAT WITH NO LOCATION AND NOTHING TO RECOMMEND IT IS DROPPED** (user, 2026-08-03).
  Kept-not-lost is right for a flat we merely cannot place; one that names no street AND
  scores poorly is not a lead, it just sits in the list forever unactionable. Two gates in
  `pipeline._classify`, both inside `if not geocode.has_location(geo_source)`:
  - `score <= config.MIN_SCORE_WITHOUT_ADDRESS` (50) → DROP. 91 of 422 rows.
  - **a bearing off a landmark → DROP at ANY score.** `ליד האוניברסיטה`,
    `מול שער האוניברסיטה` are relationships, not places. 21 rows, of which exactly **one**
    (`באר שבע, קרוב לאוניברסיטת בן גוריון וסורוקה`, score 55) needed this rule; the rest
    were already going on score.
  - **"Has a location" is `geocode.has_location` — do NOT add a fifth definition.** The
    codebase already held four overlapping notions of address quality; the one matching
    the user's three statements exactly is `confidence()`, already persisted in
    `listings.geocode_source`: `exact|high|street` → located ("**a street is okay**"),
    `area|none` → not ("only a neighbourhood is not an address", "university is not"). It
    keys on the SOURCE, not the text: a street-name test was tried first and put the
    hand-pinned `מגדלי דוד` in the drop list.
  - **`names_only_a_landmark` MUST stay inside the `has_location` branch.** The same text
    test answers True for `מגדלי דוד, סורוקה` — a real building the user pinned — which
    survives only because the static table answers it several tiers *inside* the geocoder.
    From the text alone the two are indistinguishable; the geocoder's verdict tells them
    apart. Hoisting the check out deletes the landmark.
  - **The RAW score, not the voted one** (user's choice): the verdict must be reproducible
    from the post alone, so a replay gives the same answer whatever the group has clicked.
    A ⭐ therefore cannot rescue a placeless flat — accepted and deliberate. 0 starred rows
    were affected. `MIN_SCORE_WITHOUT_ADDRESS` must stay below `MIN_ALERT_SCORE`, or
    every placeless listing good enough to alert about would be deleted before it could be.
  - **a bare QUARTER → DROP at ANY score** (user, 2026-08-04: *"keep them only if in a
    known location like הבלוק"*). `שכונה ד` is 2,375 m across, so its centroid is a dot in
    the middle of thousands of flats — 16 dots sat on area centroids, 14 of them nothing
    but a quarter, one scoring **97**, all kept by the score gate below. Now 16 → 1.
    - `geocode.names_only_a_neighbourhood` is a **TEXT test and must NOT reuse
      `is_bare_neighborhood`**, which answers True for `אלעזר בן יאיר שכונה ד` — an
      address that names a street. That predicate answers a different question ("cap this
      to amber?") and reusing it here deletes the flats the user's own *"a street is
      okay"* rule protects.
    - **Anything left after the quarter is removed counts, EVEN IF UNPLACEABLE.**
      `אנדלה אמבלו, שכונה ד` names a street missing from OSM; failing to geocode a street
      is our limitation, not the post's. It is the one area-centroid dot that remains.
    - `הבלוק` never reaches this branch — surveyed at 123 m, it grades `static`.
- **A LANDMARK IS AS PRECISE AS ITS SURVEY SAYS** (`landmarks.json`, from the user's
  hand-drawn KMZ via `load_landmarks_from_kmz.py`). `geocode._static_source` grades from
  the DRAWN EXTENT, not from a hand-kept list, because guessing is wrong both ways:
  | measured diagonal | grade | example |
  |---|---|---|
  | ≤150 m | `static` (precise) | `הבלוק` 123, `מגדלי דוד` 115, `מרכז הנגב` 135 |
  | ≤400 m | `static_street` | `אביסרור` 299 |
  | >400 m or unsurveyed | `static_area` | `שכונה ד` (2,375 m) |
  - **`הבלוק` IS A PLACE — 85 × 96 m.** It sat in `_AREA_KEYS` as "the whole student
    quarter, several streets across" and so was graded `area`, i.e. not a location at all.
    **MEASURED DEAD END, DO NOT REPEAT: deriving an area's size from the STREET CENTROIDS
    that co-occur with it in addresses gave ~680 m and made הבלוק look no better than
    `שכונה ג`.** That proxy is invalid — `אברהם אבינו` is long and its midpoint lies well
    outside the part of it inside הבלוק. Only a survey answers this.
  - The polygon centroid beats the pin it was first placed with (`הבלוק` 67 m,
    `אביסרור` 89 m). A key with no polygon behaves exactly as before.
  - **A HOUSE NUMBER STILL BEATS EVERY LANDMARK** — ~13 m vs the tightest 115 m. The
    stand-aside rule tests `k in landmarks()`, NOT the `static_area` grade: surveying
    הבלוק took it out of that grade and silently re-broke `רגר 137, הבלוק`.
- **A NAMED STREET BEATS A CO-OCCURRING NEIGHBOURHOOD** (user's rule: "a street is okay").
  The area key stood aside for a house number only, so 13 listings drew 364–1,070 m from
  the street their own post names (`שלמה המלך, שכונה ג` worst). Now 0 m–144 m; unplaced
  32 → 18. Two traps in that gate:
  - **`_names_a_street` has TWO conditions and BOTH are load-bearing** — each was learned
    by breaking the other, so don't simplify it to one:
    1. the token must appear **VERBATIM** in the address. `_candidate_tokens` also emits
       its own corrected spellings, which is a fuzzy step this function cannot see: for
       `ליד האוני` it offers `הגאונים`, and `streets.canonical` then answers `exact`.
       Without this check "near the university" reads as a street address and
       `גר בשכונה ג ליד האוני` resolves to **nothing at all**.
    2. a **fuzzy** match must clear `_STREET_FUZZY_MIN` (0.90). Demanding non-fuzzy was
       too strict: `יוסף בן מתתיהו` is verbatim but resolves fuzzy (one letter from OSM's
       `יוסף בן מתיתיהו`) while its corrected twin resolves exact but isn't verbatim —
       each failed a different half, so a street we know was called no street, the flat
       drew on `שכונה ד`, and the quarter rule above would then have deleted it.
    Measured, and the threshold sits between them: `יוסף בן מתתיהו`→`יוסף בן מתיתיהו`
    **0.966** (want), `האוני`→`הגאונים` **0.833** (refuse), bare `בן מתתיהו` 0.750
    (already recorded unresolvable, stays refused).
  - **It records NO fallback.** If the named street cannot be resolved the honest answer
    is NEEDS_DATA, not the centroid just rejected — otherwise `שלמה המלך, שכונה ג` quietly
    returns to being 1,070 m wrong and a nonsense address answers with the first `שכונה`.
- **"NEAR X" IS NOT "AT X", AND A POST NAMES BOTH** (`_near_governs`; user, 2026-08-03).
  The static table answers several tiers before `_is_bare_proximity` runs, so
  `ליד מגדלי דוד` returned the building's own point graded `static`. 0 of 321 listings say
  "near" today — this exists because grading הבלוק precise turns tomorrow's `ליד הבלוק`
  into a confident wrong dot.
  - **A key the flat is AT beats one it is only NEAR, whatever the word order.**
    `ליד הבלוק, מגדלי דוד` is NEAR הבלוק and AT מגדלי דוד; ranking by position answered
    with הבלוק and graded the lot `area`, discarding the address the post gave.
  - **The lookback stops at a separator.** A plain 14-char window before `מגדלי דוד`
    reached over the comma, found הבלוק's `ליד`, and marked both as bearings.
- **Hand-pinned landmark coordinates the user supplied** (`geocode.STATIC_TABLE`, and they
  grade `exact`, so the rules above keep them): `מגדלי דוד` (31.255349, 34.803121),
  `אביסרור` (31.254823, 34.798264), `מרכז הנגב` (31.259132, 34.795781). **`מרכז הנגב` is
  NOT `מרכז אורן`** — measured **1,186 m** apart, and the street index matched the bare
  word `מרכז` to the street `מרכז אורן` until that was refused.
- **A BUILDING WITH NO HOUSE NUMBER IS NOT EXACT — UNLESS IT IS A LANDMARK** (user's rule,
  2026-08-12). `_static_source` grades the KEY, so a street sitting in `STATIC_TABLE`
  graded `static` exactly like a hand-pinned place: `רינגלבלום` with no number came back
  **`exact`**, putting 14 listings on one point each claiming a specific building.
  **38 of the 66 `static` listings** were in that state — `רינגלבלום`, `וינגייט`,
  `כיכר האבות`, `ביאליק`. They now grade `static_street`.
  - **The exception is narrow and real:** a SURVEYED landmark has a drawn outline, so
    `הבלוק` (123 m) on its own genuinely is exact and `_landmark_grade` has already sized
    it. Membership of `landmarks()` is the test — not "looks like a place".
  - **THIS IS NOT COSMETIC, and that is the reason to be careful with it.** `static` is in
    `_PRECISE_SOURCES`, so it also bought `edge_grace` (AMBER→GREEN within 40 m) and
    SKIPPED the boundary-street caution in `pipeline._classify` — the checks that exist
    precisely for street-level points. The affected streets are the ones that need them
    most: measured from the green-zone edge, `וינגייט` sits **10 m** out, `רינגלבלום`
    89 m, `כיכר האבות` 150 m. A bare `וינגייט` cannot tell green from red at 10 m.
  - **Predicted effect on stored verdicts: 36 of the 66 become `edge_uncertain` →
    NEEDS_DATA**, about 21 of them GREEN/MATCH today. Nothing is dropped — they move to
    the review queue with "קרוב לגבול האזור — המיקום לא מדויק מספיק". A code change alone
    does NOT move them; that needs `replay.py --apply`.
  - Two older tests asserted `src == "static"` for a bare street. Both were really about
    PLACEMENT — which key wins, and that a point survives at all — so both keep those
    assertions and only the grade moved.
- **THE MIRROR LIST IS SHORTER THAN IT LOOKS, AND THE ALL-DEAD RESET WAS THE EXPENSIVE
  PART** (2026-08-13). `config.OVERPASS_URLS` holds 4 URLs resolving to **3 machines** —
  `overpass.kumi.systems` and `overpass.private.coffee` are both `193.219.97.30` — so one
  dead host burned two timeouts. `_overpass_mirrors()` dedupes by resolved host, lazily and
  **failing open**: an unresolvable name is not evidence that two mirrors are the same, and
  it must never do DNS at import.
  - `_dead_mirrors` was a SET that was CLEARED the moment every mirror was dead, which
    retried them all for the very next address. With the mirrors down that cost
    3-4 × (1 s pace + 8 s timeout) ≈ **36 s per uncached address**, and a cache warm that
    should take minutes looked like an hours-long job. It is now `{url: retry-after}` with
    a 120 s cooldown, and when every mirror is cooling the tier is SKIPPED rather than
    retried — the answer is the same, the caller still treats it as transient (never cached
    as a miss), and they recover on their own.
  - Measured side effect: the test suite went **107s → 76s**, because tests resolving many
    addresses stopped re-attempting the same dead mirrors.
- **THE TWO LOOKUP LOOPS HAD DRIFTED, AND ONLY THE MAP WAS WRONG** (2026-08-12).
  `geocode_cached` (local-only, and the function EVERY map dot goes through —
  `dashboard.py:85`) is supposed to mirror `_resolve_detailed`. It did not: the
  `or k in landmarks()` clause that makes a landmark step aside for a house number was
  added to the detailed loop and **never to the cached twin**. So the pipeline placed
  `רגר 137, הבלוק` at house 137 and stored `osm_addr`, while the map drew it on the
  `הבלוק` pin **624 m away** — under a confidence badge reading `exact`, because
  `dashboard.py` grades the STORED source but fetches the coordinate from the cached loop.
  **15 listings on one pin; p90 222 m, max 626 m.** The 26-listing/18-address pile on
  `הבלוק` was the single worst artefact on the map.
  - **A CENSUS FOUND EXACTLY ONE REAL PILE.** 74 of 212 points held more than one
    listing, which looks alarming until each class is checked: ~10 were adjacent numbers
    inside the documented p90 66 m interpolation floor (`רגר 111`+`114`), 3 were an
    unanchored street falling to `overpass` and correctly graded `street` (`בן מתתיהו`
    12/32/34), 15 were honest area centroids, 12 were multi-unit towers. Only `הבלוק` was
    a defect. **Do not "fix" the others** — after the repair, map-vs-pipeline is
    **median/p90/max 0.0 m** over all 318 numbered listings.
  - **The docstring claiming parity is what hid it for months.** When you change
    precedence in either loop, change both, and extend
    `tests/test_geocode.py::test_the_two_lookup_loops_agree_on_every_address_shape` —
    a corpus-based drift guard that only became possible once the suite went offline.
  - `geocode_cached` also never applied `landmark_point`, so even a correct landmark
    answer used the hand-dropped pin: **`הבלוק` 67.1 m** off its surveyed centroid,
    `מגדלי דוד` 7.7 m, `מרכז הנגב` 5.2 m.
  - **STILL DIVERGENT, DELIBERATELY:** `geocode_cached` has no `_near_governs` concept, so
    for a post naming two landmarks with a bearing word — `ליד הבלוק, מגדלי דוד` — the two
    loops would pick different keys. It fires on **nothing in the current data**; recorded
    so the next person measures it rather than assuming parity is now complete.
- **A USER PIN IS A SUBSTRING RULE, NOT AN ENTRY, AND IT BEATS A REAL ADDRESS.** Both
  lookup loops match pin keys with `norm.find(k)` — any location text CONTAINING the key —
  and return before `_not_on_campus`, above the house-number interpolation. Simulated
  2026-08-12: one pin on `אוניברסיטה` moves **6 of 9** university-ish names onto the campus
  point, including `רגר 5, ליד האוניברסיטה`, where a numbered address loses to a bearing.
  - The mask bypass is DELIBERATE and must stay — a 📍 pin is a human saying "this is
    where it is", and the campus rule (nobody rents on the lawn) is exactly what a human
    should be able to overrule. What is unsafe is not the bypass but the KEY: a pin named
    after a landmark applies to every address that mentions it.
  - **So a "pin these" report must never offer a bearing**, however unplaceable it is —
    unplaceable is not the same as pinnable. `geocode.pinnable_unknowns` filters
    `storage.unknown_locations` on `still_unplaceable` AND `names_only_a_landmark` for
    exactly this reason; `/unknowns` was showing `אוניברסיטה` and `שער האוניברסיטה` as rows
    1 and 2 of 6, each with an armed 📌, because that list sorts by frequency.
  - The counter-example that keeps the rule honest: `מגדלי דוד` is a landmark the user
    pinned BY HAND and it must keep working. The difference is not "landmark vs not" — it
    is whether the name is a place or a bearing, which is `names_only_a_landmark`'s job.


- `geocode.py` — static name table (primary) → house-number placement → cache →
  optional Google → Overpass → Nominatim.
  - **`place_house()` projects, `interpolate_house()` only interpolates.** Between two
    anchors → `interpolated`. Past them → `extrapolated`; on a single-anchor street
    (using the city's measured 11.2 m per house number) → `projected`. Both are capped
    at 150 m and clamped to the street's real geometry. It exists because refusing sent
    the address to Overpass/Nominatim, and those measured **3,528 m** out (ההגנה 89).
  - **`extrapolated` is graded `high` but is NOT in `_PRECISE_SOURCES`** — it's a
    projection, not a survey, so `pipeline._classify` keeps its boundary-street and
    near-edge caution. Don't "tidy" it into the precise set.
  - **`projected` (one anchor) is graded `street`, not `high`** — that anchor fixes
    where a number is, not which way the numbers run, so the direction is a guess.
    Same coordinate, honest label.
  - **Never clamp past the end of a street's polyline.** `_point_on_axis` clamps to the
    last vertex, so seven `אלכסנדר ינאי` numbers (17,19,21,23,28,30,32) all answered with
    ONE point graded `high`. Both its anchors sit past that end, 16 m apart across 6
    numbers = 2.7 m/number against the measured 11.2. Now: refuse when the target leaves
    the street's extent, and fall back to the city spacing when the gradient is
    degenerate. Same pathology `_point_on_axis` was written to fix, same street — the
    interpolated interior was fixed and the extrapolated tail was not.
  - **`static_area` (a `שכונה …` key, or the slang `הבלוק`) is graded `area`** and is NOT
    a precise source. `_CONFIDENCE["static"]` was `exact` whatever the key was, so 19
    listings whose post said only `שכונה ד` drew as solid precise dots on one point — the
    biggest pile on the map. Siblings already did this right (`static_street`, `landmark`).
  - **A STREET WE HOLD THE POLYLINE FOR IS A LOCATION, even with no house number**
    (`street_point` / `_bare_street_point`, graded `street_geom` → `street`, never
    precise). `רחוב רמב״ם` returned `(None, None)` while `streets.canonical` answered it
    `exact` — the externals never matched the gershayim spelling, and they were never
    needed. The last of 322 listings naming a resolvable street with no location, which
    under the 2026-08-03 drop rule can be deleted outright. Measured over every stored
    address: **5 gain a point, 0 change, 0 lose one**.
    - It is the degenerate `place_house`: the midpoint of the street's extent through the
      same `_point_on_axis`, a target strictly BETWEEN the first and last vertex, so it
      can never be the clamped endpoint. **A number is never answered here** — that is
      how a red-end address read as green.
    - **The 200 m off-street guard does real work**: one index name can cover two roads
      that are not one road, and `לימונית`'s midpoint sits **4.9 km** from any לימונית
      vertex, in the desert between two neighbourhoods. Median 11 m over 1,172 streets,
      33 refused.
    - **A CACHED MISS IS ABOUT THE GEOCODERS, NOT ABOUT US.** The miss is written the
      moment Overpass and Nominatim fail, so `_cache_lookup`'s miss branch has to consult
      the tier too — wired only at the end, the fix works once and then stops forever.
      The tier still records the miss (`_remember_miss`) so a street we answer locally
      does not re-query every dead mirror on every run.
    - **The proximity guard is load-bearing.** `_named_street` reads the RAW text and an
      institution's own words look like a street — `אוניברסיטת בן גוריון` yields the
      boulevard — so without it a place nobody rents gets a confident street-level dot.
  - **Interpolation is 2D and parity-aware, and keeps the setback** (2026-08-01).
    Position along the street still follows the polyline, but the anchors' offset from
    the centreline is interpolated alongside it and added back (`_axis_offset`), and
    `_anchors_for` prefers anchors of the same parity as the number. The two halves
    only work TOGETHER — on 705 held-out addresses: old 44.9 m, parity only 31.3 m,
    setback only **46.8 m (worse)**, both 18.7 m. Setback alone averages the odd and
    even sides and points back at the road. End to end the geocoder went p50 38 → 14 m,
    p90 146 → 99 m.
  - **A computed address is snapped to the nearest building within 25 m**
    (`snap_to_building`, `buildings.json`). 25 m is swept, not guessed: it is the only
    radius that improves the median AND the tail. A number we hold an anchor FOR is
    exempt — that point is evidence, and snapping it answered a hand-placed pin 20 m
    from where the person put it.
  - **Building-COUNT interpolation does not work** — measured 19.0 m vs 18.4 m, worse
    as the tolerance loosens. Sheds and stairwells are footprints too. Don't rebuild it.
  - **A dashboard pin on a numbered address becomes an ANCHOR** (`add_anchor` →
    `user_anchors.json`, which wins over OSM and survives a PBF rebuild). It fixes the
    whole street, not one flat — the only mechanism that can ever place a house on the
    ~18 streets with no OSM addresses, because the numbering origin is not derivable
    from free data ("low numbers nearer the centre" holds for only 64% of streets, so
    guessing it lands at the wrong END). Refused past 200 m from the street it claims.
- **ONE ROAD = ONE POOL, of geometry AND of anchors** (`streets._pools`, `streets.aliases`,
  and the pooling loop at the end of `geocode._load_anchors`). OSM splits a single road
  across two index entries two ways, and each fragment then keeps its own geometry *and*
  its own house numbers:
  - **word ORDER** — `ביאליק חיים נחמן` held 135 m, `חיים נחמן ביאליק` held 2,849 m of the
    same street, nearest vertices **0 m** apart. 12 name-sets. House 122 then failed the
    200 m check against a 135 m stub, and six ביאליק listings shared one dot.
  - **a leading road-TYPE word** (`דרך`/`רחוב`/`שדרות`/`סמטת`/`כיכר`…) — `דרך מצדה` held
    5 points and **1** anchor, `מצדה` held 225 and **21**. `דרך מצדה 69` projected off that
    single anchor and came out **585 m** from the street it names. 10 pairs.
  21 pools in total. **Pooling the geometry is only half the repair** — anchors, pins and
  caches are keyed by street NAME, so the numbers stay split until `_load_anchors` unions
  them too. Where both spellings already carry the same number, each keeps its own survey.
  **The gate is that the fragments TOUCH** (`_MERGE_TOUCH_M = 50 m`). `כיכר האבות`/`האבות`
  and `כיכר המדע`/`המדע` share a word bag and lie **2.5 km** apart; welding those is
  exactly the multi-kilometre error the 200 m off-street guard exists to catch, and it
  must not be introduced here to satisfy that guard elsewhere.
- `load_osm_buildings.py` — `buildings.json`: 19,110 footprint CENTRES on a coarse grid
  index, from the same PBF. Only 3.7% of them carry a house number, which is why nothing
  in the pipeline had ever seen a building.
- **NOBODY LIVES ON THE CAMPUS OR IN THE HOSPITAL** (`zones.no_housing_here`, from the
  `university`/`hospital` polygons already in `area_features.json`). A coordinate landing
  there is a data error every time, and they were arriving from three directions at once:
  the `אוניברסיטה` landmark point **was** the campus centre (8 listings would have got a
  dot in the middle of the university on the next replay), **13 house-number anchors** sat
  on institutional buildings — `יוסף בן מתיתיהו 97` dragged number 90 onto the lawn — and
  an external geocoder can always answer with a lecture hall.
  - `_load_anchors()` drops masked anchors; `seed_anchors._accept` refuses them at source;
    `geocode_detailed` rejects any masked result → NEEDS_DATA, where a person sees it.
  - **Hand-placed points are exempt** (`_NO_MASK_SOURCES`): the static table is curated and
    a 📍 pin is deliberate, so if a human says a flat is on campus, they meant it.
  - **The mask is safe because the polygon is tight**: measured, NO street geometry runs
    inside the campus (0 of 23 vertices on `יוסף בן מתיתיהו`, 0 of 231 on `רגר`), and real
    perimeter addresses like `רגר 104` fall outside it. Don't widen it to a bounding box.
  - Only kinds we hold a surveyed outline for. Guessing the extent of the mall or the
    industrial zone would be inventing, which is what this exists to stop.
  - `ליד האוניברסיטה` / `בסמוך לסורוקה` now resolve to **nothing**. "Near the university"
    is not a location (user's decision, 2026-08-01); the listing stays in the list and in
    search, it just stops claiming a position. `הבלוק` remains a landmark — it is a real
    residential quarter — though the static tier answers it first, as `static_area`.
- **"NEAR X" IS NOT AN ADDRESS, AND NO EXTERNAL GEOCODER MAY ANSWER ONE**
  (`geocode._is_bare_proximity`, consulted before the Google/Overpass/Nominatim tier).
  Dropping the university from `_LANDMARKS` was only HALF the 2026-08-01 decision: the
  phrase then fell through to **Overpass**, which answered with a point OUTSIDE the campus
  polygon — so `no_housing_here` missed it too — and two listings came back as AMBER
  MATCHes in the next replay. `_plausible_external` cannot cover this: it ABSTAINS when
  there is no street to measure against, which is exactly this case. A `_NEAR_RE` word +
  no house number + no street we can name → NEEDS_DATA, where a person sees it.
  `_descriptive_landmark` still runs after, so `ליד הבלוק` keeps working.
  - Underneath it was a DATA fault: `תחנת רכבת צפון - אוניברסיטה` — the railway station —
    sat in the STREET index, and `_words_index` matches a unique word run, so
    `האוניברסיטה` canonicalised straight to it. That is the 783 m mismatch noted above,
    at its source. Measured: exactly **1 of 1,174** entries, so `streets._NOT_STREETS`
    names it explicitly rather than pattern-matching `תחנת`. The durable fix is for
    `load_area_features.py` to stop importing railway features (needs an Overpass run).
  - **Only a replay diff caught this.** Always read `python replay.py` before `--apply`.
- **The geocode cache CANNOT be shrunk by more than half** (`_save_cache`). It was wiped
  from ~300 entries to 1 twice in one day: any process holding a small `_cache` — a
  hold-out harness using it as scratch, a long-lived server whose copy predates a rebuild
  — saves once and the small dict lands on disk. Recovery is a 35-minute re-geocode and
  until someone notices the map loses two thirds of its dots. The cache is a pure
  accelerator, so a refused write costs time and an allowed one costs data. Don't remove.
- `warm_cache.py` — rebuild the cache for the ~340 LISTING addresses in minutes instead
  of `replay.py`'s hour over 3,680 archived posts. Fills the cache only; `replay --apply`
  is still the only thing that rewrites verdicts.
- **A long-lived server pins the code at process start.** `serve_dashboard.py` had been up
  22 h, so the phone page was serving the previous evening's `dashboard.py`, `geocode.py`
  and an `_anchors` loaded before `govmap_anchors.json` existed — a whole day of work
  invisible while the process looked healthy. `doctor.py`'s `dashboard` row compares the
  process start against those files' mtimes and FAILs when it is older. **Restart the
  server after any change.**
- `govmap.py` / `seed_anchors.py` — **the anchors are BOUGHT ONCE and then owned.**
  199 of the 237 GREEN/AMBER-relevant streets had <2 OSM anchors, so `interpolate_house`
  could not run and every flat on them collapsed onto one street centroid — the real
  content of "most of the points are clusters". `POST govmap.gov.il/api/search-service/
  autocomplete {"searchText": …}` is free, keyless and Israeli-government; measured
  **median 5.4 m** against 8 surveyed OSM addresses. One run: **549 requests, 9 min, 838
  anchors on 127 streets**, written to `govmap_anchors.json`. After that the live pipeline
  never touches it again — every future listing on those streets is placed by local
  arithmetic. **`main.py`/`pipeline.py`/`replay.py` must never import `govmap`**: it is
  the site's own internal endpoint, undocumented and free to change.
  - **It substitutes silently, so nothing is trusted.** `בני אור 999` answers
    `בני אור 13`; a nonsense street answers with an address in **רמלה**; it renames
    (`סמטת קדש` → `קדש`). A result is accepted only if it is `type=address`, in Be'er
    Sheva, carries the number asked for, canonicalises to the same street, sits within
    200 m of that street's geometry, and the street's numbers **trend** monotonically
    along it by RANK CORRELATION. A step-by-step monotonic test threw away all nine
    correct `בני אור` anchors — odd and even sit on opposite sides so the sequence
    wobbles locally while trending cleanly.
  - **Asking for a number that does not exist is the efficient move**: govmap answers
    with a spread of real neighbours, so `בני אור 200` harvests nine anchors in one
    request. Numbers real listings use are also asked for explicitly — the harvest ranks
    low (1–28) while the listings were at 50 and 64.
  - Merge order in `_load_anchors()`: **OSM survey → govmap fills only MISSING keys →
    user pins override both.** govmap can never degrade a surveyed point or a 📍 fix.
  - **`--exact` beats interpolating.** Arithmetic is p50 13 m; asking govmap for the
    address itself is 5.4 m. For the ~142 addresses this bot has listings at there is no
    reason to compute what can be looked up — 69 of 89 missing ones became exact anchors.
  - **Select by unplaceable LISTINGS, not anchor count** (`stranded_streets`). Two anchors
    in the wrong place buy nothing: `אלכסנדר ינאי` was anchored 8–14 with every listing at
    17–32, `ביאליק` anchored 1–4 with listings at 11–139 — both skipped by the old
    "<2 anchors" test.
  - **Reject a street only when its anchors split into clumps >300 m apart** (`_one_road`).
    The earlier axis-ordering test discarded **29 streets of good data** (רוטנברג's 9
    anchors were all within 49 m) because OSM often holds a FRAGMENT: רוטנברג has 147 m of
    geometry but real numbers past 65, so projecting onto that fragment scrambles the
    order. The per-anchor 200 m offset test is the real guard.
  - **`--unresolved` pins the addresses whose STREET we cannot resolve at all** — govmap
    takes a whole string without our index. This placed `יוטבתה 6` and `פארן 11`, which
    the note below records as absent from OSM entirely. 11 of 14.
  - **POI/landmark lookups are a TRAP — measured, do not retry.** govmap "resolved" 30 of
    41 landmark strings but almost all are fuzzy substitutions to a different place:
    `אגם תבור`→`הר תבור 17`, `אוקספורד`→`בת שבע`, `בניין הסטודנטים-אוקספורד`→`חומרי בניין`
    (a building-supplies shop). With no house number there is nothing to validate against,
    so it cannot be gated the way the address path is.
  - **Post text / comments carry no recoverable house number** — measured over all 196
    listings without one: **0** from comments, 4 from post text of which most are regex
    false positives. Confirms the existing note; don't build an extractor.
- **Free alternatives to govmap: all checked 2026-08-01, all dead.** Nominatim / Photon /
  LocationIQ / Geoapify / Pelias serve the same OSM data already in our PBF.
  `api.govmap.gov.il` is an HTML portal and the guessed ArcGIS paths 404. OpenAddresses
  `sources/il/countrywide-hebrew.json` is `"skip": true` with a 404 upstream. The Be'er
  Sheva municipality `כתובות` ArcGIS layer is public with exactly our bbox but its origin
  refuses all connections and its proxy answers `CONT_0044 Error generating token`.
- `unique_report.py` — **scores the objective**: how many distinct (street, number)
  addresses have a point to THEMSELVES. 85 → **111 of 142** after the seed. The ceiling is
  not the listing count: 196 of 410 listings give no house number and can never be
  unique honestly.
- `load_osm_addresses.py` — builds `house_anchors.json` from
  `C:\osrm\israel-and-palestine-latest.osm.pbf`, **the extract already on disk for OSRM**.
  Overpass (`load_house_numbers.py`) is the fallback, not the source: it was down on all
  four mirrors all day, which is why the anchors were thin. The old query required
  `addr:street`, so it structurally missed the 99 BS buildings tagged with a house number
  and no street; those are bound here to the nearest centreline within 40 m and **dropped
  beyond it** rather than guessed. 811 → 998 anchors, 97 → 115 usable streets.
  Measured caveat: this converted only **1** of the 105 stranded listings — the extra
  anchors landed on streets that already had them. Keep it for the data and the lost
  dependency, not as the fix.
  - **An anchor must be NEAR the street it names** (`MAX_ANCHOR_OFFSET_M`). Street names
    repeat inside the bbox: binding by name alone gave `ההגנה` anchors 10 m from its
    geometry *and* anchors 2,887 m away from a different street of the same name, so
    `ההגנה 89` interpolated between two unrelated streets and landed 3.5 km out. **Five
    anchors out of 998 were the entire multi-kilometre error tail.** Don't relax this.
  - **A way's anchor is the MEAN of its footprint, not `nodes[0]`** — the first vertex is
    a corner, median 12.3 m off centre, and it was 68% of the anchor set. On its own this
    changed nothing (the old interpolation snapped to the centreline and threw the
    cross-street half away); it is what makes the setback fix above meaningful.
- **What we accept from Overpass/Nominatim** (`_plausible_external`, `_NOMINATIM_OK_CLASSES`):
  a point >250 m from the street the address names is a blunder, not imprecision
  (`audit_geocode` measures the median at 6 m), and Nominatim must return somewhere to
  LIVE — it answered `ליד האוניברסיטה` with the railway station 783 m away, and `אצ"ל 6`
  with a **stadium**. Both gates ABSTAIN when the street is unknown: no opinion beats a
  wrong rejection. Rejecting sends the listing to NEEDS_DATA, where a human sees it —
  strictly better than a silent wrong tier and walk time.
- `geo_accuracy.py` — **the only thing that makes "more accurate" a fact.** Holds out
  each of N addresses OSM knows exactly, hides its anchor, asks the geocoder, and reports
  error in metres per tier. Without the hold-out it grades itself against its own answer
  key. Baseline → after extrapolation: p50 **52→43 m**, p90 **192→170 m**, worst
  **3528→2840 m**, imprecise tier **34→15**. Re-run it after any geocoding change.
  Complements `audit_geocode.py`, which asks a different question (is the point ON its
  street?).
