# BGU Housing Bot — project context

Personal tool to find apartment-share listings near Ben-Gurion University
(Be'er Sheva) from Hebrew Facebook group posts, filter them against fixed rules,
check they're within a hand-drawn walkable zone, and alert on Telegram.

## Current status — BUILT, TESTED, and running

The full pipeline (parse → deterministic cleanups → hard filters → geocode →
green/walk-time zone tier → fit score → SQLite + optional Google Sheets +
Telegram) and the Facebook auto-scraper are built, covered by an offline pytest
suite, and scheduled via Windows Task Scheduler. `manual.py` is the risk-free
paste-a-post entry point; `python main.py --live` runs the scraper (dry-run
without `--live`). Alerts route to a shared Telegram group with ⭐/🗑 vote buttons
(`bot_listener.py`) that feed the ranking; morning/evening top-N and a DM digest
are scheduled. Introspection with no browser: `stats.py` (funnel) and
`replay.py [--apply]` (re-test config/zone/score changes against the archived
post history, and optionally write the results back). The repo is on GitHub at
`github.com/EitanPinczowski/bgu-housing-bot`. See `README.md` for full setup,
scheduling, and every tunable.

## Pipeline

`post text → Gemini (Hebrew NLP) → deterministic cleanups → hard filters →
geocode → zone tier (green polygon / 20-min walk to a gate) → fit score →
SQLite + optional Google Sheets + Telegram alert`

## Key decisions (do not silently reverse these)

- **LLM = Google Gemini free tier** (`gemini-flash-lite-latest`, chosen for the
  largest free daily quota on this key), behind a small interface in `llm.py` so
  it can swap to an OpenAI-compatible endpoint (Ollama/Groq). Guaranteed
  structured output + a Hebrew prompt whose core rule is *return null, never
  guess*. On quota (429) or repeated errors it falls back to a local Ollama model
  for the rest of the run; a client-side min-interval paces Gemini under the RPM cap.
- **Output = local SQLite + Telegram, plus an OPTIONAL Google Sheets sink**
  (`sheets.py`, service account; silent no-op until `GOOGLE_SHEET_ID` + creds
  exist). The sheet is a browsable/sortable mirror; SQLite stays the fast local
  dedup/cache and source of truth.
- **In-range = the user's hand-drawn green zone** (`green_zone.json`, from a
  Google My Maps KMZ), graded by `zones.classify_location` / `classify_effective`:
  - `GREEN` inside the polygon → preferred match (✅)
  - `AMBER` = outside the polygon but within **`MAX_WALK_MINUTES` (20) walk of a
    campus gate** → acceptable, not preferred (🟡)
  - `RED` beyond that (or inside a `no_amber_zones.json` area like שכונה ד' but
    outside green) → dropped
  - `UNKNOWN` couldn't geocode → NEEDS_DATA
- **Amenity/transit proximity is DISPLAY-ONLY** (explicit user decision, 2026-07-29):
  alerts show the walk to line 669 on רגר (both directions), to a bus heading for
  רכבת באר שבע מרכז with its frequency, and to the gym at קניון עזריאלי הנגב — but
  none of it enters `fit.score`. Frequency requires the MOT **GTFS** feed (OSM knows
  where stops are, not how often buses run). Rebuild with `python load_amenities.py`.
  Do not quietly turn these into scoring factors.
- **OSRM gives the amber walk time** for real listings (min over gates); when it's
  down, and for the whole-area map, a calibrated straight-line estimate is used —
  so the bot still classifies without OSRM running. (`BUFFER_METERS` is deprecated.)
- **Blacklist** (`config.BLACKLIST_NEIGHBORHOODS`: Ramot, Neve Zeev, Nahal
  Ashan, Pelach 7) is a separate hard instant-drop applied before geocoding.
- **Dedup identity = phone + NUMBERED ADDRESS**, not the phone alone
  (`storage.make_dedup_key` / `is_duplicate`). The phone survives reposts, but a phone
  is not a flat: measured 2026-07-29, **42 numbers advertise more than one numbered
  address (one posts 32)**, and the old phone-only key collapsed **101 distinct flats**
  into single rows — every flat after a landlord's first was dropped as "already seen"
  and never alerted. Fixing it took 288→309 listings and 79→91 MATCH. Do not "simplify"
  this back to the phone. The asymmetry is deliberate: a read with **no** house number
  still collapses on the phone alone, because a vague re-read can't be told from a new
  flat and a wrong duplicate alert is the worse failure. No phone → content hash.
- **Facebook is the ONLY source** (user decision, 2026-07-29). Yad2 was evaluated and
  rejected: every endpoint sits behind Radware Bot Manager, so the only ways in are
  CAPTCHA-solving or detection evasion — forbidden below, and here it would also risk
  the **home IP the FB scraper depends on**, for Yad2's whole-flat/broker inventory
  rather than the שותפים market this bot targets. If a second source is ever revisited,
  the legitimate route is Yad2's own saved-search **emails**, parsed from an inbox.
- **Broker detection is data-driven, from the POST ARCHIVE** — a contact with ≥
  `config.BROKER_MIN_LISTINGS` (4) distinct numbered addresses is an agency
  (`storage.phone_listing_count`): labelled in the alert and −`BROKER_PENALTY`, never
  dropped. Never match on "תיווך" (many brokers never write it; many posts mention it
  about someone else). Count from the archive, NOT the listings table: an agency whose
  flats are mostly out of zone otherwise looks like a private landlord — that one
  choice moved it from 1 contact flagged to 7 of 136.
- **THE GEMINI DAILY QUOTA RESETS AT 10:00 ISRAEL TIME**, not local midnight — it is
  midnight US Pacific. Measured 2026-08-03: the 08:00 run was `RESOURCE_EXHAUSTED`
  while the 11:09 run did 233 fresh posts on Gemini. **The 08:00 run therefore always
  spends the PREVIOUS day's leftovers**, which the previous evening's runs drained.
  - Anything counting calls must key on `dates.quota_window`, never `date.today()`: a
    midnight-reset counter hands the 08:00 run a budget it does not have and reports
    healthy right up to the failure it exists to prevent.
  - **The damage is lost runs, not slowness.** That morning the run fell through to
    Ollama at ~63 s/post, ground 186 posts, took **5h12m**, held the scraper lock, and
    the 10:00 and 12:00 runs both logged `SKIP another scraper session is running`.
    Three scheduled runs, one completion — and the locked-out 10:00 run is the one that
    would have had fresh quota. `LOCAL_FALLBACK_MAX_POSTS_PER_RUN` (40) ends a run
    before it can do that again; unread posts are never marked seen, so the next run
    takes them.
  - `LLM_DAILY_BUDGET` (900) stops us *before* Google does, taking the same code path
    as a real 429 so the run-cap fires next. `doctor`'s `llm budget` row shows it.
  - **Why calls grew**: 302 fresh posts/day on 07-30 → **1,184** on 08-02, mostly real
    post volume (August is peak season; per-run fresh went 51–93 → 233–347). The four
    pre-LLM gates already absorb ~27%, so the worst day was ~865 actual calls.
  - **A local Ollama "is this an ad" triage is a MEASURED DEAD END — do not retry.**
    Timed on 12 real archived posts: `gemma2:9b` is 11/12 correct but **25.4 s median
    per post** (≈106 min added per run, to save the ~20% of calls that are NOT_AD);
    `gemma2:2b` is 6.6 s but **7/12 correct**, i.e. it discards real listings. Both
    trades are worse than the problem.
  - **Batching (`llm.extract_many`) is built and MEASURED TO HARM — it stays OFF**
    (`LLM_BATCH_SIZE = 1`). The free tier meters REQUESTS and posts are tiny (p50 316
    chars, p90 602, max 1,784), so 5 per request would have cut ~865 calls/day to ~175.
    It does not survive its accuracy gate (`python batch_ab.py 5 --batch 5`):
    | field | single vs single (noise floor) | batched 5 |
    |---|---|---|
    | `is_apartment_ad` | 100% | 100% |
    | `price_per_room_ils` | 100% | **80%** |
    | `available_rooms_count` | 100% | **70%** |
    | `street_address_or_neighborhood` | 85% | **70%** |
    Price and rooms agree PERFECTLY call-to-call, so their drop is batching, not model
    variance — and they are the fields the hard filters run on. 3 MATCH-eligible posts
    lost a price or a room count outright (one lost `price_per_room_ils=2800`). n=20, so
    the percentages are wide, but the losses are concrete.
    - **The control must be a single Gemini call in the SAME SESSION — NOT the archived
      `parsed_json`**, which two models (the Ollama fallback) and older prompts wrote;
      measured that way the address field "disagrees" 80% of the time and says nothing.
    - **Run the harness at `--batch N`, never at `config.LLM_BATCH_SIZE`.** It used to
      chunk by the config knob, which is 1 while batching is disabled — so it compared a
      single call against a single call and printed PASS on both gates without batching
      anything. A test that reads the switch it is gating can only agree with itself.
    - The accident was still useful: it is where the noise floor above comes from.
    - Retrying at 2 or 3 would trade a smaller saving for the same class of loss, and the
      quota pressure that motivated this is already handled by `LLM_DAILY_BUDGET` and
      `LOCAL_FALLBACK_MAX_POSTS_PER_RUN`. Don't re-enable without new evidence.
- **Filters** (`config.py`): ≤2000 ILS/room, ≥2 rooms free, ≤4 total roommates.
- Missing critical fields → kept as **NEEDS_DATA**, never silently dropped.
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

## Files

- `config.py` — all thresholds, gates, blacklist, `FB_GROUPS`, provider + scraper settings.
- `models.py` — `ListingExtract` (LLM schema, incl. `floor`) and `PipelineResult`.
- `llm.py` — Gemini extraction + Ollama fallback (provider-abstracted); rate-limit;
  optional bounded OCR of image-only posts (one image, Gemini-only, capped per run).
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
- `osrm.py` — local foot routing; min over gates (drives the 20-min amber boundary).
- `zones.py` — green polygon + no-amber (ד') polygons; walk-time tier classification.
- `amenities.py` / `load_amenities.py` — walk times to the bus/gym that matter
  (`config.AMENITY_TARGETS`), from MOT GTFS + Overpass. **Display only, never scored.**
- `fit.py` — 0–100 fit score → ⭐1–5 (zone, walk, price, rooms, freshness, entry date).
- `storage.py` — SQLite: dedup, listings, votes/marks, unknown-locations, fingerprints, post archive.
- `sheets.py` — optional Google Sheets sink (append, batch reconcile, sort, rebuild).
- `notifier.py` — Telegram MarkdownV2 alerts; group-vs-DM routing; albums; vote buttons.
  - **A VOTE BUTTON CARRIES A TOKEN, NEVER THE dedup_key.** Telegram caps `callback_data`
    at **64 BYTES** and answers BUTTON_DATA_INVALID by rejecting the **whole message**,
    not the offending button. A dedup_key is `phone|address` and Hebrew costs 2 bytes a
    character, so a descriptive address overflows — measured 2026-08-02, 16 of 417 keys,
    the longest 93 bytes. Because alerts are **batched**, one long address took a whole
    batch down: that run delivered **4 of 16** and the rest were lost with one line of log.
    `storage.callback_token` mints a stable 12-hex-char stand-in into `callback_tokens`;
    `bot_listener._resolve` reverses it and **falls back to a raw key**, because buttons
    posted before the change live in the chat forever. `_update_tally` must match through
    `_resolve` too — comparing `save|{key}` silently stops the counts updating.
    `_fit_callbacks` drops anything still over 64 bytes so a regression costs a button,
    not the alert.
- `pipeline.py` — `process_post(...)` funnel; `_classify(...)` reused by replay.
- `scraper.py` / `login.py` / `main.py` — Playwright reader, one-time login, orchestrator.
- `manual.py` — paste-a-post CLI (risk-free entry point).
- `top_listings.py` / `digest.py` / `dm_digest.py` — morning/evening top-N, recaps, DM digest.
- `bot_listener.py` / `watchdog.py` — vote-button listener + DM-only `/search`
  (`query.py`) and `/status` commands; dependency health check.
- `query.py` — parse a free Hebrew/English search into filters; ranked SQLite search.
- `replay.py` / `stats.py` — offline re-classify (+`--apply`) and funnel stats.
- `dashboard.py` / `serve_dashboard.py` — the browse-by-hand view. The table and the
  map dots are rendered IN THE BROWSER from one JSON payload, so filtering moves the
  dots too; the backdrop (zone/gates/amenity pins) comes from `map_listings.build_base_svg`
  and both sides project through the same `xy_from` params. `dashboard.py` writes a
  self-contained offline `data/dashboard.html`; `serve_dashboard.py` serves the same page
  live from SQLite, which is the only way to poll, vote, or open it on a phone.
  - **A token is required on every route** — the page shows landlords' phone numbers and
    addresses. `DASHBOARD_TOKEN` in `.env`, else generated into
    `data/dashboard_token.txt`. Don't add an unauthenticated mode.
  - **The live server is a HOME tool; the published snapshot is the phone.** Tailscale was
    removed at the user's request (2026-08-02). Live = this PC or the same Wi-Fi, and it
    is where writes happen (votes, notes, 📍 pinning) because they need the DB. Away from
    home there is deliberately no live route — no tunnel, no VPN. The published page is
    the answer and it is a **PWA**: `dashboard.build_share` emits `manifest.webmanifest`,
    `sw.js` and `icon.svg` beside the snapshot and `publish.py` ships them, so it installs
    to a home screen and opens with no signal. The worker is **network-first** with a
    per-publish cache stamp — a cache-first PWA would be the 22-hour stale-server trap
    again, silently and on a phone. **The live page emits neither** (`_pwa_head(snapshot)`
    returns "" when live) for exactly that reason.
  - The image proxy serves **only URLs already in the DB**, keyed by hash — never a URL
    from the request, or it becomes an open relay. It caches to `data/images/` because
    Facebook URLs expire (only 8 of 350 listings have permanent Telegram file_ids).
  - Untrusted post text rides inside a `<script>` block, so it goes through
    `dashboard._json_for_script` (escapes `<`); `json.dumps` alone would let
    `</script>` in an address break out.
  - Viewers use `geocode.geocode_cached` — never the network. Going through the full
    geocoder took **211 s** to render 350 rows.
  - **The map is the page**, the table is behind a `<details>`. Zoom/pan animate the
    `viewBox`; strokes are non-scaling and dots/labels counter-scale, so only the
    geography grows. Zoom is **Ctrl/⌘+wheel only** — a plain wheel must scroll the
    page, or you can't reach the list under a 78vh map. Dots within ~19 px collapse
    into a counted badge (`clusterOf`) rebuilt on zoom change, not on pan.
  - **`python dashboard.py --share`** writes a dated `data/dashboard-YYYY-MM-DD.html`
    for the people flat-hunting with the user. `window.__SNAPSHOT__` **removes** every
    write control (a disabled button that alerts "unavailable" is worse) and the
    `/img` tags, and adds a dated banner. Contacts and WhatsApp links **stay** — the
    partners need to call the landlord (user's decision) — which is why
    `notifier.send_document` defaults to the **group**, never `all`. `--send` posts it;
    `BGU Dashboard Share` runs it daily at 21:00.
  - `POST /api/walk` (not `GET /api/walk/<key>`): a `dedup_key` is `phone|address`, so
    a GET would put a landlord's phone number in the URL and the access log. Same for
    `POST /api/locate`. Draws the real OSRM path (`osrm.foot_geometry`, GeoJSON — no
    polyline decoding). **No straight-line fallback**: there is no honest estimate of
    *which way* you walk. One `table_minutes` call picks the gate, then ONE geometry
    call — it used to be one geometry call per gate, i.e. 4× 15 s of timeouts to
    discover the router was down. Optional `dest` routes to an amenity instead.
  - **Touch: one finger pans, two pinch.** `touch-action` must be on `.map svg`, not
    just `.map` — it does NOT inherit, and the pointer handlers live on the svg. With
    `pan-y` there the browser scrolled the page *while* we panned the map: the
    "glitchy on phone" bug. Card buttons are 40px under `@media (pointer:coarse)`.
  - **`click` must always be bound.** It lived in the `else` of `if (CAN_HOVER)`, so on
    every desktop and many touch laptops clicking a dot did nothing — the "I can't press
    on a listing" report. Hover is an addition for pointer devices, never the only way
    in. Each dot also carries an invisible `.hit` circle sized in **real screen pixels**
    (`HIT_RADIUS_PX * unitsPerPx`): a dot is 4px, and sizing the target in viewBox units
    looked right but measured 8px, because the map renders ~0.36px per unit.
  - **A badge must be as easy to hit as a dot, and stacks open themselves.** 223 of 229
    listings sat inside a badge and the badges had **zero** hit targets between them
    (4.1 px circles) while every single dot got `HIT_RADIUS_PX` of padding — that is
    "clusters aren't clickable". Pressing one also started a pan and captured the
    pointer, which retargets the following `click` to the `<svg>` where `closest('.cl')`
    is null, so `.cl` now joins `.dot` in the pointerdown exclusion. Past
    `AUTO_FAN_ZOOM` (6×) every `sameSpot` stack **in the viewport** fans open by itself —
    viewport-limited because 38 stacks × 19 dots would otherwise be rebuilt on every
    redraw and `drawDots` has no culling. A group too tight for max zoom to split
    (`__tight`) fans instead of zooming, because `fitTo` clamps at 8× while `applyView`
    allows 12× and the badge could otherwise be clicked forever doing nothing. Measured
    in-viewport: at 4× 80% of visible listings are still badged, at **6× it is 3%**, at
    8× zero.
  - **PANNING MUST NOT DO ZOOM WORK.** `applyView` ran on every pointermove and
    re-queried + rewrote every `.dot` radius and all 264 `.slabel` font-sizes — all of
    which divide by the zoom factor, which a pan does not change. It now sits behind the
    existing `lastZoom` guard in `rescaleForZoom`, and the label NodeList is cached
    (static backdrop markup). Measured: pan **0.24 → 0.005 ms** at 1×, **0.38 → 0.093 ms**
    at 8×. `drawDots` also culls to the viewport + 0.6 screens, redrawing only when the
    window leaves the box it was drawn for: **260 → 108** nodes at 8×, and panning the
    full width in 28 steps costs 9 redraws with 0 groups left unrendered.
    - **Culling must never change what the page CLAIMS, only what it draws.** Counting in
      both the total loop and the render loop made the counter read "556 על המפה" against
      456 listings. One accumulator, over `allGroups`; there is a test.
    - **A map laid out at ZERO width cannot be clustered** — every px size divides by it
      and the `|| 1` fallbacks make one cluster cell 19,000 world units, collapsing all
      312 listings into a single badge that reads like data loss. `drawDots` keeps the
      last good drawing and waits. Nothing listened for `resize` at all before, so cluster
      cells, hit targets, halos and the scale bar were stale after a phone rotation.
  - **The dot carries two extra channels and only two** — ⭐ saved as a gold ring,
    📵 contacted at 40% opacity. It already encodes tier as fill and precision as
    hollow/solid; at ~4px on a phone, more is mud. Broker/stale/notes/photos have filters
    and card lines and stay there.
    - **A BADGE carries the ring too.** At 1×, **253 of 312** listings sit inside a badge,
      so a ring on lone dots alone helps in 19% of cases and is missing from exactly the
      flats you cannot pick out by eye.
    - **Sized in SCREEN px, not viewBox units** — the `mkHit` trap again. In world units
      the ring measured 7px around a 4px dot at 375px wide while looking fine on a
      desktop. Now 14px (dot) / 20px (badge) on every screen.
    - `.dot.hi` must not set `stroke:#111`: it repainted an approx dot's tier-coloured
      outline and destroyed the precision signal at the moment you were looking closely.
  - **The map NAMES what it is not showing.** A listing with no coordinate used to just
    vanish, and the two counters disagreed silently — 84 of 396, 21%. The counter now
    reads `N מתוך M על המפה · K ללא מיקום`, and K opens a sheet listing them, each row
    opening its card. 🎯 there starts the pin flow via `pin_worklist(unplaced_only=True)`.
    - `fixes` is still counted over EVERY imprecise listing: a pin on בזל fixes the
      placed-but-vague ones too, so narrowing the rows before counting would understate
      each pin. The queue narrows; the arithmetic does not.
    - The button says **61**, not 83 — the other 22 have no address for govmap to answer.
  - **GREEN|AMBER|RED is drawn as a field** (`map_listings.tier_field_svg`), sampled from
    `zones.classify_effective` itself. NOT an analytic mask of gate circles minus green
    minus no-amber: `classify_effective` also reds out anything outside ב/ג/ד, so a mask
    would paint AMBER where a listing is classified RED. Sampling the classifier cannot
    disagree with it; a test asserts pixel identity over every sample cell.
    - **Run-length merging is what let it ship on by default**: 2,643 rects per-cell →
      **159** merged (−94%), 186 rects / 12.4 KB / 0.9% of the snapshot. The gate was
      ≤600 nodes / 40 KB. Cells are uniform and axis-aligned so the merge is exact.
    - It goes **above the background and below the streets** — it is a wash. The green
      polygon's own 10% fill drops to 0 while it is on (two washes are mud).
    - `latlon_from` (the Python twin of the page's `unproject`) exists because the field
      must cover the WHOLE canvas: `_bounds_of` excludes the pad and `scale = min(...)`
      leaves slack on one axis, so a grid over those bounds leaves an unpainted frame.
    - **The band is the straight-line estimate; a dot's own tier uses OSRM.** Near the
      edge they can disagree, and the dot is the more accurate one. Said in the legend.
  - **The שכונה ד' carve-out is outlined** (`.noamber`, from `zones._no_amber_polys`). It
    is a RULE, not a walk-time consequence, and was drawn nowhere in either map — so a
    flat 7 minutes from a gate could be RED with nothing on screen to explain it. `קדש 26`
    flipped MATCH→DROP for exactly this reason in the 2026-08-02 replay.
  - **The map has a dark theme** (`prefers-color-scheme` in `STREET_CSS`). It had none at
    all while the page has had one since D4, so at night it was a white slab in a dark
    page. The background is a CLASS so CSS beats the presentation attribute; tier hues are
    **dimmed, not inverted** — at .19 opacity the light ones glow on dark, so dark lifts
    them to .26.
  - **Gates are a layer** with a name revealed at 2.6×. They were a bare ★ with no class:
    the only untoggleable thing on the map and the only landmark with no name, despite
    every AMBER verdict being measured to one.
  - **A scale bar** (`drawScale`), because no zoom level said whether two dots were 100 m
    or 1 km apart. Reuses the page's own projection so it cannot drift from what is drawn,
    and rides the zoom guard — a bar is a function of scale, and a pan does not change
    scale. Measured against haversine: within **0.5%** at 1×/4×/8×. `units_per_metre` is
    shared with `walk_rings_svg`, which had been recomputing it inside the radius loop.
  - **📍 אני is client-only.** `watchPosition` → a marker plus an accuracy ring in REAL
    METRES (a 40 m fix draws 40 m), and on the first fix the list orders by distance from
    you, restoring the previous sort on the way out. The position is never sent, never
    stored — which is also why it works on the published snapshot. **Geolocation needs a
    secure context**: the https page and 127.0.0.1 qualify, the plain-http LAN URL does
    not, and the error says so.
  - **The view is remembered and shareable** (`bgu.view` + a `#v=x,y,w` hash). Layers have
    persisted since F4; the view never did, so the PWA reopened on the whole city. The
    hash wins over localStorage — it is what somebody deliberately sent you.
  - **A dot says how much to trust it.** `geocode_source` → `geocode.confidence()`;
    `street`/`none` draw HOLLOW. Only 45% of listings have a house number; 41% are a
    street/neighbourhood centroid. That is why 282 mapped listings sit on **105
    distinct coordinates** — so a badge over a *stack* (one shared point, up to 19
    flats) **fans out** rather than zooming, because zoom can never separate identical
    coordinates. A badge over a genuine spread still zooms. Don't "simplify" these
    back into one behaviour.
  - **📍 place-mode corrects a location, and it is authoritative.**
    `storage.manual_locations` (keyed by dedup_key) is preferred by
    `pipeline._classify` — the same funnel `replay.py` re-runs — so a correction
    survives `replay --apply`. `"manual"` is registered in `geocode._PRECISE_SOURCES`
    as `exact`. Saving re-grades the listing through the real classifier and reports
    tier/walk/score before→after: moving a dot changes the verdict, and that must not
    be silent. Scope "this address" instead calls the existing `geocode.add_pin` +
    `uncache`, which fixes every current and future listing there.
  - **🎯 guided pinning: govmap PROPOSES, a person decides.** `pin_worklist()` orders the
    imprecise listings by how many flats one pin would fix (a numbered pin becomes a
    street anchor); `POST /api/propose` → `propose_location()` asks govmap and returns a
    candidate, writing NOTHING. The candidate draws as a dashed ring, never a dot, so an
    unconfirmed guess cannot be mistaken for a saved location. Accept commits through the
    existing `relocate()`; "סימון ידני" falls through to tap-to-place; skip moves on.
    - **Never auto-accept, and show govmap's OWN wording** (`govmap.address_detail`
      returns the matched text with the point). govmap substitutes silently — `בני אור 999`
      comes back as `בני אור 13` — and a person can only judge an answer they can read.
      The measured substitutions are rejected before display, and a candidate inside the
      no-housing mask is refused outright.
    - **The queue advances in `commitPlace`, not in `acceptPin`**, so correcting by hand
      moves on exactly like accepting does. Wired the other way, the manual path left the
      flow stuck on a flat it had just placed.
  - **The viewing-route planner** is the UI for `POST /api/route`, which had none for
    weeks: tick flats in compare → order, per-leg minutes, total, and the real OSRM path.
    Router down = "the order is an estimate" and **nothing drawn**; a straight line here
    crosses the railway, the same lie `drawWalk` already refuses to tell.
  - **Amenity pins are PER-LISTING.** The map-wide layer can only carry fixed
    landmarks; "a stop with a bus to the train station" matches 428 stops, so it is
    skipped by the `_MAX_PINS_PER_TARGET` guard and has no useful map-wide form.
    Opening a card pins that flat's own stops via `amenities.locate`, which resolves
    coordinates by (name, direction_id) out of `amenities.json` — matching on name
    alone put both directions of a junction on one pin. Still display-only.
- `publish.py` — pushes the dated snapshot to **GitHub Pages** so a URL exists when the
  PC is off (Tailscale and tunnels all need it awake; the scraper does too, so the
  hosted copy is a SNAPSHOT, never live). `SITE_REPO_URL` in `.env`, read at call time
  because `.env` is loaded per entry point. **It refuses to publish into the code repo**
  — that repo is public and git history is permanent, so ~350 landlords' phone numbers
  could never be taken back. One always-amended commit, force-pushed: a ~1 MB generated
  page four times a day would otherwise add ~365 MB/year of unread history. The page is
  public and unauthenticated by the user's explicit decision (2026-07-31);
  `PUBLISH_NOINDEX=1` only hides it from search. Unset `SITE_REPO_URL` = silent no-op.
- `map_listings.py` — the shared map renderer (dashboard + `area_map.py`): projection
  (`_projector`/`xy_from`), street geometry as **4 combined `<path>`s** not ~2,800
  polylines, zoom-revealed street labels (`data-minzoom`), landmarks, display-only
  neighborhood outlines, amenity pins, and `walk_rings_svg` (5/10/15/`MAX_WALK_MINUTES`
  from each gate, using the same arithmetic as `zones.est_walk_to_gate_min`).
  Layer switches are one class on the `<svg>`; each layer needs its **own** marker
  class (`st-l`, `nbhd`/`nbhd-abc`, `amen`) — a `:not()` chain once hid the campus label.
  - **A STREET'S PROMINENCE IS THE TOTAL POLYLINE IT DRAWS**, not the distance between one
    segment's ends, and not its longest segment. Both were wrong and each hid different
    streets: measuring end-to-end understates a curved road (`חיבת ציון`, 13.8 px against
    84.5 px of real geometry, so never named at any zoom — 22 drawn streets had ≥90 px of
    line but <90 px end to end), and judging by the longest *piece* loses a roundabout
    (`כיכר האבות` is six segments, none over 4.9 px, ~20 px in total — and it is the
    densest listing cluster on the map). Where the name is PLACED still comes from the
    longest single piece. Green/amber streets ever named **81 → 96 of 98**, at 1× **31 →
    49**. The 2 left (`אלפרד רוסי`, `נרבוני`) are genuinely under the 12 px floor.
  - **In-zone streets outrank `_LABEL_CAP`** — sorting on length alone spends the budget
    on long roads out in the desert that no flat is on. `_LABEL_CAP` itself (450) is
    headroom, not the fix: on the dashboard's viewport only 197 labels are emitted so it
    does not bind there at all; it binds on a wider one (~412 streets drawn).
  - `_MAX_LABEL_ZOOM` (8.0) **cannot currently fire** — the 12 px floor caps the ratio at
    90/12 = 7.5. It stays as the coupling between the label rule and the map's 12× zoom
    ceiling (`dashboard.applyView`), so a later change to the floor or the target cannot
    silently emit a label nobody can ever zoom to. A test holds the two ends together.
- `load_map_neighborhoods.py` / `map_neighborhoods.json` — **display-only** neighborhood
  outlines (א–יא, רמות, …). Deliberately NOT `neighborhoods.json`:
  `zones.in_allowed_neighborhood` passes a point inside **any** polygon in that file, so
  adding שכונה ו there to label the map would silently widen the ב/ג/ד gate. A
  `test_zones.py` guard proves it doesn't.
- **A CRASHED run wedges the day differently from a HUNG one** (2026-08-04). The
  self-watchdog aborts a run that stops making PROGRESS; it does nothing for a run that
  finished scraping and then died in CLEANUP. Playwright's node subprocess went down with
  `EPIPE` at group 11/15, `context.close()` never returned, and the python process sat
  alive holding the lock — which is an OS file lock, so only that process exiting frees
  it. The 17:00 hot pass and the 00:46 full run both logged "another scraper session is
  running", and the 00:46 launcher then found the holder unkillable and gave up.
  `main._bounded_teardown` gives `context.close()` and `p.stop()` a thread and a 30 s
  deadline each, so `release_lock()` is always reached. **A hang is not catchable** — a
  bare try/except around close() would have sailed straight into the same permanent wait.
  Abandoning a half-closed browser is the cheap side of the trade: `reap_orphan_browsers`
  clears it next run, while a held lock costs every scheduled run until someone notices.
- **A hung run must not block the day.** `scraper.start_self_watchdog()` (started by
  `main.py` right after the lock) aborts a run that makes no progress for
  `STALL_MINUTES`. Before it existed, `is_wedged()` was only consulted by the NEXT run,
  so a run that hung at group 4 of 15 held the lock for **six hours** and every
  scheduled run logged "another scraper session is running" — 3 starts, 0 completions
  in a day. Recovery has three parts, all needed:
  - `_kill()` does **not** use `taskkill /T` (walking a Chromium tree blew past the
    30 s budget, and the timeout was treated as total failure) and judges success by
    whether the pid is actually gone.
  - `reap_orphan_browsers()` closes browsers a dead run left behind. **Scoped by the
    profile path on the command line, never by process name** — most `chrome.exe` on
    this machine is the user's own browser (36 of 39 when measured).
  - Windows can leave a process `TerminateProcess` accepts but never reaps; those are
    unkillable until a reboot. Measured: the profile still opens with them present, so
    the reaper says so and continues rather than refusing to run.
  - **A STALE HEARTBEAT ONLY MEANS SOMETHING WHILE A RUN IS LIVE.** The file is never
    cleared on exit, so between scheduled runs its age just keeps growing. `is_wedged()`
    is safe because `_clear_wedged_holder` calls it only when a live process HOLDS the
    lock; `doctor`'s `scraper progress` row had no such guard and FAILed on an idle
    machine — 2026-08-03 13:30, "no progress for 31 min", the 08:00 run finished cleanly
    at 13:11, no `main.py` process anywhere, and the same report's `last run` row said
    PASS 0.5h ago. It now consults `scraper.run_in_progress()`, which matches the
    heartbeat's pid against **that pid's own command line** (`main.py`) — never a
    process name, same rule as `reap_orphan_browsers`, because `python.exe` says nothing
    about whose script it is. "Couldn't ask the OS" returns None and reports WARN: a
    failed query is not evidence of a hang.
- `setup_always_on.cmd` — run ONCE as Administrator. The `BGU *` tasks ship with
  "wake the computer" OFF, so a run due while the PC sleeps is silently skipped —
  the real cause of "why didn't it run". Also fixes battery wake timers/sleep.
  `doctor`'s `wake timers` row makes the failure visible.
- `load_zone_from_kmz.py` — regenerate `green_zone.json` from a new My Maps export.
- `green_zone.json` / `no_amber_zones.json` — the walkable polygon + no-amber (ד') areas.
- `README.md` — full Windows setup (Python, Docker OSRM Israel extract, Telegram bot, .env).

## Environment

Windows. Python 3.11+. Docker Desktop for a **self-hosted** OSRM foot server on
`localhost:5000` (Israel extract; see README). Secrets in `.env`
(`GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) — never commit,
never hardcode. `auth/`, `data/`, `.env` are git-ignored.

## Verify the base before building on it

- OSRM: `curl.exe "http://localhost:5000/route/v1/foot/34.79,31.25;34.8015,31.2622?overview=false"` → expect `"code":"Ok"` + a duration.
- Pipeline: `python manual.py`, paste a real post, type `END`.

## SAFETY CONSTRAINTS (must hold for the scraper)

The user has **only their personal Facebook account** (no burner). Automated
reading of FB groups violates FB's ToS and risks account suspension. Therefore
the scraper MUST be conservative and the user must stay in control:

- Non-headless, **persistent real browser profile** (log in once manually), NOT
  headless cookie injection.
- Long randomized delays, +up to 25 min jitter per scheduled run so it isn't
  clockwork; daytime only, no night runs. Volume has been raised repeatedly at the
  user's request — currently **`SCRAPER_SCAN_ALL_GROUPS=True` (all 14 groups every
  run, after 3 zero-match groups were pruned), `MIN_POSTS_PER_GROUP=20`,
  `MAX_SCROLLS=15`/`SCROLL_CAP=25`, 7×/day** (08–20 every 2h). This is a high volume
  on the user's only FB account, chosen after a clear high-risk assessment. What
  keeps each run's footprint down is the **early-stop**: the feed is newest-first, so
  a group stops scrolling once it turns up no more *fresh* posts — where "fresh" means
  within `SCRAPER_MAX_POST_AGE_HOURS` (24h) AND not already processed in an earlier
  run (a live run passes `already_seen` into `scrape_group`). So the 2nd–7th runs of a
  day are shallow (mostly seen posts → bail per group), which is what makes 7×/day
  comparable in total work to the old 4×/day deep scans. The real protections (real
  logged-in profile, home IP, read-only, human-like pacing, checkpoint-abort) are
  unchanged. Do not raise volume/cadence further without an explicit, informed request.
- **2026-07-27 — yield-scaled depth + a `--hot` pass (net volume DOWN ~21%).** Requested
  explicitly by the user (to be first to contact a new listing), knowing it touches this
  constraint. Two paired changes, deliberately budget-neutral:
  1. `GROUP_YIELD_SCALING`: every group is still visited, but depth now follows its
     measured MATCH-per-post rate (`main._group_depths`) — productive groups keep
     `MIN_POSTS_PER_GROUP=20`, ~1% groups drop to `GROUP_MIN_POSTS_FLOOR=8`.
     **300 → 220 post-reads per run** (2100 → 1540/day).
  2. `main.py --hot`: a shallow pass over only the `HOT_GROUP_COUNT=3` best groups
     (`HOT_MIN_POSTS=10`), cutting detection lag from ~2h25m to ~30–40 min.
     4 hot runs/day ≈ **120 reads/day**.
  **2026-07-30 correction — the hot pass had never actually been scheduled.** Task
  Scheduler had one scraper task running `run_scraper.cmd` with no arguments, so the
  "4 hot runs/day" in the budget above was fiction and real volume was 7 × 251 =
  **1757 reads/day**. Measured consequence: median time-to-detect **8.4 h (n=44)**,
  only 7 of 44 listings seen within an hour — the exact problem `--hot` was built for.
  `update_schedule.cmd` fixes it and pays for it: **6 full runs (08/10/14/16/18/20,
  dropping the dead noon slot) + 4 hot runs (12/15/17/19) = 1626 reads/day, −7.5%**.
  Between 14:00 and 20:00 — where 45 of 63 timed posts land — something now runs every
  hour instead of every two. Keep the invariant: **hot runs must be paid for out of
  full runs**, and re-check with `python group_report.py` and `python stats.py`
  (which now prints time-to-detect and runs/day, both with their n). All other safety rules are untouched (dry-run
  default, jitter, daytime only, read-only, checkpoint-abort).
- **Dry-run by default** — print what it *would* process; only commit/notify
  when explicitly run with `--live`.
- Read-only: it never posts, comments, messages, or interacts. Only scrolls/reads.
- Do not add CAPTCHA-solving or detection-evasion beyond human-like pacing.

## Working notes

- **Tuning workflow:** after changing the green zone, `MAX_WALK_MINUTES`, `fit.py`,
  or a threshold, run `python replay.py` to preview which stored listings flip,
  then `python replay.py --apply` to write it (updates the DB + rebuilds the
  Sheet, no Telegram). `stats.py` shows the funnel.
- **`save_listing` ENRICHES, never replaces:** every nullable column is written as
  `COALESCE(new, old)`, so a thinner later read (the LLM missed the price this time)
  can only add detail, never blank a field. The recomputed verdict
  (status/tier/score/walk) still overwrites — that part is meant to be fresh. It also
  no longer resets `first_seen`, which `INSERT OR REPLACE` silently did on every
  `replay --apply`, resetting the staleness clock on the whole table.
- **Measured dead ends — don't re-try these without new evidence.** All checked
  2026-07-31 against the real data: the **LLM is not losing house numbers** (0 of the
  imprecise listings had one recoverable from the archived post text); **Nominatim has
  no house numbers here** (`addresstype=road`, `place_rank=26` for every numbered
  address, so grading its hits `street` is correct); **govmap.gov.il is not a usable
  API** (serves HTML, its search host fails TLS); **order-insensitive street matching
  recovers 1 listing**, not worth the ambiguity risk; **copying an address between one
  landlord's listings would inject errors** (a single broker had flats on four different
  streets); and `צקלג`/`פארן`/`יוטבתה`/`יודפת` are **absent from OSM entirely**, so no
  free source will ever place them.
- **The floor:** 122 of the 227 imprecise listings give no house number at all. Nothing
  can place those to a building — the honest remedies are the 📍 manual pin or a Google
  key (`GOOGLE_MAPS_API_KEY`; the tier already exists). Do not invent positions for them.
- **Geocoding gaps:** listings whose location the LLM extracted but geocoding
  couldn't map are logged (`unknown_locations`) and surfaced by the daily DM
  digest — pin the frequent ones into `geocode.STATIC_TABLE`. The zone can be
  regenerated from a My Maps KMZ via `load_zone_from_kmz.py`.
- **FB DOM is unstable:** all selectors live in the FRAGILE block of `scraper.py`
  with a multi-selector fallback chain; expect periodic tuning. `FacebookBlock`
  detection aborts a run on a checkpoint/login wall (never retries).
- **Docs drift:** the code is the source of truth for thresholds — keep this file
  and `README.md` in sync when key decisions change.
