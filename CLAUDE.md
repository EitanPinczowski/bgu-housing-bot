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
- **Filters** (`config.py`): ≤2000 ILS/room, ≥2 rooms free, ≤4 total roommates.
- Missing critical fields → kept as **NEEDS_DATA**, never silently dropped.

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
- `load_map_neighborhoods.py` / `map_neighborhoods.json` — **display-only** neighborhood
  outlines (א–יא, רמות, …). Deliberately NOT `neighborhoods.json`:
  `zones.in_allowed_neighborhood` passes a point inside **any** polygon in that file, so
  adding שכונה ו there to label the map would silently widen the ב/ג/ד gate. A
  `test_zones.py` guard proves it doesn't.
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
