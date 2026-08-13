# BGU Housing Bot — project context

Personal tool to find apartment-share listings near Ben-Gurion University
(Be'er Sheva) from Hebrew Facebook group posts, filter them against fixed rules,
check they're within a hand-drawn walkable zone, and alert on Telegram.

## OPEN RIGHT NOW — read this first (2026-08-13)

**The geocoding accuracy targets are answered, and one of the three is not reachable
from free sources. Nothing is blocking.**

Targets were p50 ≤ 10 m, p90 ≤ 50 m, max ≤ 500 m on `geo_accuracy`'s hold-out. After a day
of govmap seeding (~1,500 requests) and two new seed filters, measured deterministically
against the pinned `data/truth_merged_20260810.json`:

| | pre-seeding | now | target |
|---|---|---|---|
| p50 | 17 m | **10 m** | ≤ 10 — **met** |
| p90 | 79 m | **66 m** | ≤ 50 — **not reachable free** |
| max | 436 m | **436 m** | ≤ 500 — **met** |
| never placed | 30 | **26** | — |
| **wrong zone tier + unplaced** | **31** | **27** | the number that matters |

Two of the three targets are met. The last ~30 m of p90 is the method's floor, not a bug.
`_same_parity_neighbour` is what closed most of the gap: **the house next door beats
arithmetic that has to cross the street.** When a street's same-parity anchors cannot
bracket a number but one sits within two of it, that anchor answers instead of an
interpolation bracketing across two arms of a road — p50 12→10, p90 101→66, and both
RED → GREEN errors gone. It sits ABOVE `place_house`, because making `interpolate_house`
decline only hands the number to an extrapolation that mixes parities too.

- **p90 ≤ 50 IS BELOW THE FLOOR OF THE METHOD, so stop buying anchors for it.** Two
  independent measurements agree: addresses whose neighbours bracket them within 50 m —
  the best case — measure p90 **43–46 m**, and govmap's own error against surveyed ground
  truth is p90 **36 m** (n=7, small). The hold-out removes an address's own anchor, so a
  dense street brackets it between ADJACENT seeds and the answer inherits THEIR error.
  That is why seeding moved p50 down and p90 **up**. Only a rooftop-accurate source
  (`_google_geocode`, already written, `config.USE_GOOGLE_GEOCODE=False`) clears 50 m.
- **JUDGE AN ANCHOR SET BY THE ZONE VERDICT, NOT BY PERCENTILES** (`.claude/tools/geo_tiers.py`).
  Seeding trades p50 against p90 and the percentiles cannot say whether that is good. The
  tier can: **31 → 29** wrong-or-unplaced of 250. The 2 regressions are both
  `שמעון בר גיורא` and both **RED → GREEN**, the one error class this project treats as
  worse than not placing at all — cause and the narrow fix are in `dead-ends`.
- **THE HARNESS WAS MEASURING A COIN FLIP.** The same anchors run twice moved 4 addresses
  (one by 122 m, one from placed to UNPLACED) because a different Overpass mirror
  answered. `max` and `coverage` — two of the gate's three criteria — were being read off
  that. A 715 m max was reported as a seeding regression on 08-11 and was nothing of the
  kind. **Always measure with `geo_dump --local-only`**, which silences the external tiers
  and is verified deterministic (identical anchors, two runs, 0 addresses changed).
- **A GOVMAP ANSWER CAN BE ON THE RIGHT STREET AND IN THE WRONG PLACE.** `_accept` only
  checked geometry, so `שדרות יצחק רגר 163` was accepted 409 m along רגר. Two filters now:
  `seed_conflict` (against the survey) and `order_outliers` (against the street's own
  monotonic order — needed because **74% of seeds sit on streets with no surveyed number
  at all**, where the survey can judge nothing). 194 anchors dropped across both.
- **`--outward` costs ~3× what its stopping rule implies.** govmap HARVESTS: ask for a
  number that does not exist and it answers with a neighbouring real address, so
  `OUTWARD_STOP_AFTER_MISSES` almost never fires and a run walks its full 10 numbers.
  700 requests → 665 accepts → **206 distinct new anchors**.

~~**Nothing is blocking. Two things are OWED, both cheap:**~~ — **Nothing is blocking and
nothing is owed** (2026-08-10). The items below are closed; they are kept struck through
rather than deleted because each was closed in a way that contradicts how it was framed,
and because a claim nobody can find again is a claim nobody can check.

1. ~~**The prompt's PRICE DIVISION RULE is the real open problem.** The n=100 A/B (run
   2026-08-07, `data/model_ab/`) showed **neither model reliably divides a total rent by
   the number of residents** — the rule `_SYSTEM_HE` states. 3.5 usually answers null;
   3.1 sometimes answers the whole flat's rent as the per-room price. Both lose flats,
   3.1's more quietly. Tightening that one prompt line is worth more than any model
   swap, and it is testable with `model_ab.py` against a fixed sample.~~
   **CLOSED 2026-08-10, AND THE FRAMING WAS WRONG: TIGHTENING THE PROMPT LINE MADE IT
   WORSE.** Teaching `_SYSTEM_HE` to divide was tried and reverted the same morning —
   "divide, but return null when the resident count is unknown" made the model stop
   ASSERTING resident counts at all: **20 of 100 posts lost `available_rooms_count`**,
   which feeds the ≥2-rooms-free gate, and 10 CORRECT divisions became null. The model
   resolved the tension by going quiet. The real fix was arithmetic, not language — see
   item 3. Recorded in the `dead-ends` skill.
   **Never run the harness while a scrape is running** — pacing is per PROCESS, the RPM
   limit is per project, so two writers issue ~27/min against a limit of 15. `guard.py`
   now enforces this.
2. ~~`prune_orphan_listings` is BROKEN~~ — **FIXED 2026-08-06**, see the note under
   `storage.py` in the `storage-notes` skill. It would have deleted 21 rows, 11 of them
   real; it now removes 2.
3. **The division was never missing — it was dividing by the wrong number** (2026-08-10,
   `pipeline._recover_price_per_room`). `rooms - 1` from the text won over
   `total_roommates_in_apt`, and the proxy is wrong in **5 of the 6** whole-flat totals
   over the pinned 100: `דירת 4 חדרים ל2 שותפים ... 2,800` derived 933 where the ad says
   2 residents and the answer is 1400. A bad proxy also used to ABORT the division instead
   of falling through, dropping a flat that divides to 1,100. Over all 8,920 archived
   posts with a price the change is strictly additive: **4 rescued, 0 prices changed, 0
   lost**. It reverses a test that called the proxy "the established convention".
4. **A MODEL-VS-MODEL A/B CANNOT SEE AN ERROR BOTH MODELS MAKE.** The n=100 comparison
   found 2 whole-flat totals because it only looked at DISAGREEMENTS. Checking each model
   against the post text instead found **6 for 3.1 and 1 for 3.5** — a truer size for the
   problem, and the strongest evidence yet for keeping 3.5 at the front of the ladder.
   `.claude/tools/prompt_ab.py` does that check; it compares two PROMPTS on one model,
   which `model_ab.py` cannot.

> **Live state is measured, not written here.** Listing counts, quota used, whether OSRM
> is up and whether a scrape is running are printed at session start by
> `.claude/hooks/session_start.py`. The numbers below were true when typed and are not
> maintained — this section is for open DECISIONS. That hook also warns when this
> section's date falls behind the newest commit, which is how the drift was noticed.

Done 2026-08-13: the map's landmark pile fixed, the "no number, no exact" rule, a
reproducible replay, and a full `--apply` (535 → **579 listings, 302 → 310 MATCH**, 18
rescued, 79 duplicates merged, sheet rebuilt). Suite GREEN (695), `ruff` clean.
- **EVERY FAILURE THAT NIGHT WAS THE MACHINE SLEEPING, AND EACH LOOKED LIKE SOMETHING
  ELSE.** A 30-minute replay stall with idle CPU and one parked TCP connection was
  diagnosed as a dead Overpass mirror; it was S0 standby. The 20:02 scrape that died in 6s
  with `net::ERR_NAME_NOT_RESOLVED` on all 15 groups was a slot firing seconds after wake,
  before DNS. **Before calling a stall a hung remote, ask whether the machine was awake** —
  the two are indistinguishable from CPU and socket state alone.
- **A HOLLOW `END` COUNTS AS A COMPLETED RUN.** That 6-second scrape logged `END … posts=0
  groups_ok=0/15`, and `stats.py` scores an END as a success, so a slot that did nothing
  made the reliability row read healthier. Third variant of "a run that did not happen must
  never count as one", after the wedged lock and the START with no END. `main` now resolves
  two names first and logs `SKIP network down (no DNS)` instead.
- **A REPLAY THAT CALLS THE NETWORK PRODUCES A SAMPLE, NOT AN ANSWER.** Two passes minutes
  apart over the same 10,565 posts disagreed on **1,144 rows** — 736 `street_geom →
  overpass` — while only 116 were the code change under test. `--apply` writes those, so an
  un-frozen apply bakes one roll of the dice into the DB. `replay.py --frozen` is
  byte-for-byte reproducible and, after `warm_cache --archive`, loses nothing: local-only
  places 2,425 of 2,683 archive addresses and the other 258 fail with the network too.
  **`full_replay.py` is the one command that warms then freezes, in that order.**
- **COUNT WHAT NEEDS THE NETWORK, NOT WHAT IS MISSING FROM THE CACHE FILE.** The warming
  backlog was estimated at 2,148 addresses / ~12 hours from `geocode_cache.json`
  membership. It was **521 / 36 minutes**: `geocode_cached` also answers from the static
  table, anchors, interpolation and street geometry.
- **`_static_source` GRADES THE KEY, AND BOTH BRANCHES BELIEVED IT.** A street in
  `STATIC_TABLE` grades `static` → `exact` → `is_precise_source`, so a bare `רינגלבלום`
  claimed a specific building (14 listings on one point), and the skipped-key fallback did
  the same for a number it had FAILED to place. Both now yield to `static_street`; a
  surveyed landmark keeps `exact`, which is why that fallback needed the KEY and not just
  the grade. 38 of 66 `static` listings were affected — details in `geocoding-notes`.
- A `--apply` gate that guesses from the clock gets worked around. `full_replay.py`'s
  refused 07:00–21:00 and blocked a demonstrably safe 90-minute window; it now asks Task
  Scheduler for the real next run, and fails OPEN if it cannot read it.

Done 2026-08-12: the "pin these" re-check finished across all four reports that build one
(`stats`, `dm_digest`, `bot_listener`, `weekly_digest`) on `geocode.pinnable_unknowns`; the
test suite made offline and reproducible. Suite GREEN (672), `ruff` clean.
- **A USER PIN IS A SUBSTRING RULE, AND IT BEATS A REAL ADDRESS.** `geocode_cached` matches
  pin keys with `norm.find(k)` and returns BEFORE `_not_on_campus`, above the house-number
  interpolation. Simulated 2026-08-12: one pin on `אוניברסיטה` moves **6 of 9**
  university-ish names onto the campus point — `ליד האוניברסיטה`, `שער האוניברסיטה`, and
  `רגר 5, ליד האוניברסיטה`, a NUMBERED address losing to a bearing. `/unknowns` was
  offering exactly those names with an armed 📌 (rows 1 and 2 of 6, because the list sorts
  by frequency). The bypass is correct for a hand-placed point and catastrophic for a pin
  named after a landmark; `names_only_a_landmark` is what keeps the two apart.
- **MEASURE EACH CALLER — A SHARED BUG DOES NOT MEAN A SHARED SIZE.** The same stale-log
  fix was worth wildly different amounts per window, and copying it across without checking
  would have hidden that. At `stats`'s full history the staleness filter removes 98 of 182;
  at `dm_digest`'s `days=1` default it removed **0 of 4**, while the landmark filter removed
  **3 of 4**. A name resolves only once somebody ACTS on the digest, so a one-day window has
  nothing stale in it yet — the two filters earn oppositely at the two ends.
- **THE SUITE WAS GEOCODING AGAINST THE LIVE INTERNET, AND CACHING THE DISAGREEMENT.** Two
  tests called real Overpass/Nominatim: 3 of 7 `pytest-randomly` seeds failed, and the same
  seed could differ between runs. Worse, `geocode_detailed` CACHES, so the answers landed in
  `data/geocode_cache.json` — `אברהם אבינו, שכונה ד` from overpass and `אברהם אבינו` from
  nominatim, **711 m apart against a 300 m assertion** — which turned the flake permanent and
  wrote test data into the file the live pipeline reads. Three autouse fixtures now enforce
  it, proved in `tests/test_offline_guards.py`; the rules are in `testing-conventions`.
  A constant runtime across seeds is the evidence that nothing is dialling out.

Done 2026-08-10: the price divisor fix above; `street_geom` merged (a street whose
POLYLINE we hold is a location even with no house number — `רחוב רמב״ם` was the last of
322 listings naming a resolvable street with no dot); a full `replay.py --apply`
(**70 rescued to MATCH, 4 dropped, 102 duplicates merged**, sheet rebuilt); and the
Claude tooling described under "Where the rest lives". The suite is GREEN (616 passing,
`ruff` clean) and `doctor.py` is all-green.
- **MOST OF A REPLAY'S DIFF IS NOT YOUR CHANGE.** That apply moved 255 posts, of which
  the divisor fix accounted for 4. The rest was weeks of code improvements that had never
  been replayed into the stored verdicts. Read the `changed:` count as accumulated drift,
  not as the effect of what you just did.
- Docker was broken on 08-05 and is **fixed** — see the orphaned-socket note under
  "Verify the base", and the `osrm-docker` skill.
- `pytest -q | tail` discards pytest's exit code. Read the count, or drop the pipe.
  `guard.py` now blocks ANY pipe from pytest — and the same trap exists on `replay.py`,
  where `| tail` cost two full 26-minute previews by truncating the `changed:` summary
  the whole preview is run for. The lesson is wider than the one command it is enforced on.
- **THE LADDER IS NOT LEAVING 3.1's QUOTA UNUSED** (checked 2026-08-10 after the usage
  dashboard showed 3.1 at 221 against ~500). It climbed correctly on 08-09 — 3.5 to its
  480 cap, then 3.1 for 221 more, **zero Ollama fallbacks** — and stopped because demand
  stopped at 701 calls, inside the ~700–870/day the budget note below documents. The one
  "spent on every model" event (08-08 08:00) belongs to the PREVIOUS window: an 08:00 run
  draws on the window that opened at 10:00 the day before. Unverifiable directly, because
  `llm_budget.json` keeps only the current window — worth retaining a few.
- **THE HARD PART OF AN `--apply` IS FINDING A GAP.** A run starts on the hour all day,
  and on 08-05 the 18:00 full run held the lock for 90+ minutes at ~2 min/post because it
  had fallen through to the local model. Waiting politely for a free lock failed twice.
  What worked: **disable the `BGU Housing Scraper*` tasks, apply, re-enable.** (`BGU
  Housing Scraper Hot` needs an elevated shell — "Access is denied" otherwise.) The check
  is `python -c "import scraper; print(scraper.run_in_progress())"`. Both preconditions
  are also in the tuning-workflow note further down.

Still unverified, and recorded as unverified rather than assumed:
- ~~`LOCAL_FALLBACK_MAX_POSTS_PER_RUN` has never fired.~~ **IT HAS — VERIFIED 2026-08-04,
  twice, and it worked.** The 08:00 and 10:00 runs both logged `local fallback cap reached
  (40 posts) — ending the run so the next one can start` and stopped at exactly 40 local
  posts (129 posts / 7 of 15 groups, then 51 / 2 of 15) instead of grinding for hours.
  Compare the pre-cap runs that had nothing to stop them: **49** local posts on 08-01 and
  **43** on 07-24. Unread posts are never marked seen, so the next run took them. This was
  carried as "unverified" long after the evidence existed — check `grep "local fallback
  cap reached" data/scraper_runs.log` before repeating that claim.
- **`LLM_DAILY_BUDGET` IS 480, AND THE NUMBER COMES FROM GOOGLE** (2026-08-06). It was
  900, "under the ~1,000/day observed ceiling" — read off the usage dashboard, which
  shows where you have BEEN and never where the cap IS. The real limit was **500**, so
  the budget sat above it and could never bind; Google refused first, which is precisely
  what exiles a run to Ollama.
  - The refusal states it outright and is now parsed and stored:
    `limit: 500, model: gemini-3.5-flash-lite`,
    `quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: '500'`.
    `doctor` **FAILs** when `LLM_DAILY_BUDGET` exceeds the limit Google last stated.
  - **THE CEILING IS NOT STABLE AND IS PER MODEL.** On 08-04 the same model served
    ~687 requests with 2 errors — impossible under a 500 cap — so Google *lowered* the
    allowance mid-week. The AI Studio **Rate Limit** page is the only authoritative
    source (the docs decline to publish it): 3.5-flash-lite **RPM 15 / TPM 250K / RPD
    500**, 3.1-flash-lite the same, 2.5-flash-lite RPD **20** and 404 to new users.
    Never hard-code a limit again; follow what a refusal states.
  - **Earlier refusals that "kept climbing past" (252 → 259, 389 → 393) were per-minute
    blips**, and reading them as the daily ceiling would have argued for cutting the
    budget to ~250. That test is still right — a daily exhaustion is terminal for the
    window — but it only tells you a refusal is NOT daily. For the actual number, read
    `limit:`.
  - **The kind is computed from the FULL error and stored** (`refused_kind`). Re-deriving
    it from the truncated copy lost it: `quotaId: …PerDay…` sits past the cut while
    `limit: 500` sits early, so a genuine daily refusal read back as `unknown`.
  - **`_spend_budget` carries the WHOLE record through.** Writing a bare
    `{window, calls}` erased the refusal on the next call; preserving only `refused_at`
    repeated the bug one field later and blanked the diagnosis.

Two plan files, **neither auto-loaded** (unlike this one):
- `~/.claude/plans/spicy-sparking-crystal.md` — **all six parts closed.** 2 and 4 ended in
  a deliberate *no change* and 5 is closed by the dashboard reading above; all three
  reasons are recorded here so they are not relitigated on a hunch.
- `~/.claude/plans/velvet-spinning-fountain.md` — Part A (retry a transient 429/503
  instead of exiling the run to Ollama) is **DONE**; Part B is the one command at the top
  of this section, waiting on the 10:00 quota window.

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

The deep rules for these moved into skills, which load only when the work matches. Each
is named below under **Where the rest lives**; nothing was reworded on the way.

- the **model ladder, the quota window and the budget** → `llm-notes` skill
- the **dedup identity** (phone + numbered address) → `storage-notes` skill
- **what counts as having a location**, landmark precision, street-beats-neighbourhood,
  "near X" → `geocoding-notes` skill


## Files

One line each. The deep notes for a module live in the skill named beside it — load that
before editing the module, not after.

- `config.py` — all thresholds, gates, blacklist, `FB_GROUPS`, provider + scraper settings.
- `models.py` — `ListingExtract` (LLM schema, incl. `floor`) and `PipelineResult`.
- `llm.py` — Gemini extraction + Ollama fallback, rate-limit, bounded OCR. → `llm-notes`
- `geocode.py` / `streets.py` / `govmap.py` / `seed_anchors.py` / `load_osm_*.py` —
  address → coordinate, anchors, pooling, interpolation. → `geocoding-notes`
- `storage.py` / `sheets.py` — SQLite (dedup, listings, votes, archive) + the optional
  Google Sheets mirror. → `storage-notes`
- `notifier.py` / `bot_listener.py` / `digest.py` / `dm_digest.py` / `top_listings.py` —
  Telegram alerts, vote buttons, digests. → `telegram-notes`
- `dashboard.py` / `serve_dashboard.py` / `map_listings.py` / `area_map.py` /
  `publish.py` / `load_map_neighborhoods.py` — the browse-by-hand map and its published
  snapshot. → `dashboard-notes`
- `scraper.py` / `login.py` / `main.py` / `manual.py` / `watchdog.py` /
  `setup_always_on.cmd` — the Playwright reader, the lock, the watchdogs. →
  `scraper-notes`

Modules whose notes are short enough to live here:

- `osrm.py` — local foot routing; min over gates (drives the 20-min amber boundary).
- `zones.py` — green polygon + no-amber (ד') polygons; walk-time tier classification.
- `amenities.py` / `load_amenities.py` — walk times to the bus/gym that matter
  (`config.AMENITY_TARGETS`), from MOT GTFS + Overpass. **Display only, never scored.**
- `fit.py` — 0–100 fit score → ⭐1–5 (zone, walk, price, rooms, freshness, entry date).
  - **`MIN_ALERT_SCORE` (75) WAS AUDITED 2026-08-05 AND DELIBERATELY LEFT ALONE.** It lets
    the top **45%** of MATCH rows through (86 of 191, ~1–5 alerts on a normal day), which
    is neither the silence you stop trusting nor the flood you mute. The distribution is
    **smooth across 75** — the gate sits inside the densest bucket — so there is no valley
    to snap the number to, and any new value would be as arbitrary as this one. The only
    evidence that could justify moving it is which flats the group actually stars, and
    that is **n=3** (1 saved, 1 dismissed, 1 contacted) against 191 MATCHes. `stats.py`'s
    `alert gate` row prints the histogram, the gate's position in it, and its own n, and
    warns only once ≥20 votes exist and a **saved** listing scored below the gate. Do not
    retune this on the score shape alone; wait for the votes.

- `pipeline.py` — `process_post(...)` funnel; `_classify(...)` reused by replay.
- `query.py` — parse a free Hebrew/English search into filters; ranked SQLite search.
- `replay.py` / `stats.py` — offline re-classify (+`--apply`) and funnel stats.
  **`replay.py --frozen` is the only reproducible mode**; `full_replay.py` warms the
  geocode cache then replays frozen, and refuses on battery / no OSRM / a scrape due.
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
- **DOCKER WON'T START AFTER AN UNCLEAN SHUTDOWN: ORPHANED UNIX SOCKETS** (2026-08-05,
  cost most of an afternoon). Docker Desktop dies with *"An unexpected error occurred …
  initializing X: listening on unix://…: remove …: The file cannot be accessed by the
  system"*. Those `.sock` files are zero-length **reparse points** whose backing object
  died with the crash; Windows can neither open nor delete them, so `Remove-Item` fails
  with the same error Docker gets. **A reboot does NOT clear them** — they are on disk.
  - Fix: stop every Docker process, then **rename the PARENT DIRECTORY** (the file itself
    cannot be touched). Docker recreates it empty on the next start. Renaming beats
    deleting — it is reversible, and these dirs can hold more than the socket.
  - **THE ERROR MOVES TO THE NEXT SOCKET**, so fixing one looks like it did nothing.
    Seen in order: `%LOCALAPPDATA%\Docker\run\dockerInference`, then
    `%LOCALAPPDATA%\docker-secrets-engine\engine.sock`. Sweep for
    `Attributes -match "ReparsePoint"` under both roots and clear them all at once.
  - **Never click "Reset to factory defaults"** on that dialog — it is the other button,
    and it deletes all images and containers, `osrm_bgu` included (a multi-GB rebuild
    from the Israel PBF).
  - Symptom while it is broken: `docker` CLI HANGS rather than erroring, and
    `wsl -l -v` shows `docker-desktop` **Stopped** with no `dockerd` inside it.

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
- **2026-08-05 — THE LAG IS LOST RUNS, NOT CADENCE. Do not rebalance the schedule.**
  Detection lag was measured before changing anything, and the schedule is not the
  binding constraint: **20 of 42 scheduled full runs completed in the 7 days to 08-05
  (48%), with 17 slots LOST to a held lock.** A slot that never runs cannot be fixed by
  moving the slots around, and the three lock repairs (bounded teardown, the self
  watchdog, `start_keep_awake`) all landed 08-04/08-05 — *after* almost all of this data.
  Re-measure over clean days before touching cadence; the plan's "trim productive groups
  to pay for a hot pass" trade-off is not needed if the scheduled runs simply happen.
  - **`stats.py`'s reliability row counted `END|SKIP` TOGETHER, so it read healthiest
    exactly when runs were being lost** — 08-03 reported 11 runs / **119%** of target
    while 5 were `lock held` and 4 actually ran. A SKIP is a run that did not happen.
    Lock-held skips are now counted as losses and the ~1-in-8 `random human-like skip`
    apart from them, because that one is designed and flagging it trains you to ignore
    the row. This is the gate the whole latency question is decided on; there is a test.
  - **A RUN THAT STARTS AND NEVER ENDS IS A THIRD LOSS, AND END/SKIP CANNOT SEE IT.**
    The 14:00 full run on 08-05 logged START, wrote no END, and was gone from the process
    table an hour later. It is not a SKIP (it took the slot) and not an END (it produced
    nothing), so the row called the day quiet. `START - END` counts it: **7 in 7 days**,
    on top of the 20 completed and 17 lock-lost. It is a DIFFERENT fault from the wedged
    lock — a crash releases the lock, so the next slot starts normally and nothing
    downstream ever complains. The scrape in flight while the report runs is excluded.
  - **The worst lag cluster is one wedged run, not a bad group.** 90 posts at ~17.7 h,
    59 of them in one group, were the 00:46 hot run that slept until 09:15 and read an
    Aug-4-noon backlog at ~06:00. Group `138595033004411`'s 1,066-min median is that
    event, not its posting pattern — don't drop a group on it.
  - **What IS structural: the overnight gap.** Posts published 19:00–23:00 (27% of the
    usable sample) have a 500–680-min median because night runs are forbidden. Afternoon
    posts (13:00–17:00), where hourly coverage already works, sit at 45–139 min. Nothing
    to fix without breaking the daytime-only rule.
  - **Two thirds of the archive cannot answer this question at all.** `posted_at` was
    rewritten on every sighting while `first_seen` is not, and `sig` is a content
    signature — so a landlord reposting the same text pushed `posted_at` past
    `first_seen` and the row was dropped as impossible. 1,968 of 3,027 on 08-05, silently
    until then. The surviving sample is *posts published once*, and `stats.py` says so.
    - **FIXED at the source** (`record_post` now keeps the EARLIEST of the two, not the
      newest). **Forward-only**: rows already overwritten have genuinely lost the original
      publication time and there is nothing honest to rebuild it from. The unusable share
      should fall as new posts are archived — `stats.py` prints it, so it is checkable.
- **Dry-run by default** — print what it *would* process; only commit/notify
  when explicitly run with `--live`.
- Read-only: it never posts, comments, messages, or interacts. Only scrolls/reads.
- Do not add CAPTCHA-solving or detection-evasion beyond human-like pacing.

## Working notes

- **Tuning workflow:** after changing the green zone, `MAX_WALK_MINUTES`, `fit.py`,
  or a threshold, run `python replay.py` to preview which stored listings flip,
  then `python replay.py --apply` to write it (updates the DB + rebuilds the
  Sheet, no Telegram). `stats.py` shows the funnel.
  - **Two preconditions for `--apply`, neither of which announces itself.** OSRM must be
    UP: it is a Docker container, a replay without it silently substitutes the
    straight-line walk estimate, and the AMBER boundary IS a walk time — so applying
    while it is down bakes the approximation into every tier and score. And no scrape may
    be running: runs start on the hour all day, both processes write the same SQLite, and
    a collision leaves the DB half-rewritten. `doctor.py` answers both in one command.
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
- **ONE SCRAPED BLOCK IS NOT ALWAYS ONE POST** (`scraper._clean_story`, 2026-08-05).
  A block can run on into the NEXT story, and that story has its own price, address and
  phone. Measured: **404 of 6,502 archived posts (6%)** carry an embedded author+age
  header — 32 live MATCHes, 20 NEEDS_DATA. Mostly harmless (the tail is a comment and the
  right flat was still extracted), but the reported case was a couple's *wanted*-ad
  followed by a stranger's offer: the LLM extracted the OFFER, so the listing showed the
  wanted-ad's text under the wanted-ad's permalink. The `_TAIL_MARKERS` cut never fired
  because that block had no "View more comments".
  - The boundary is the **author line + bare relative age** (`3h`, `13h`) FB renders above
    every story after the first. The post's own header does not survive cleaning in that
    shape — its timestamp is the CSS-scrambled single characters dropped just above — so a
    surviving pair marks the next story. Cutting there keeps the post the permalink
    belongs to, which is the first one.
  - Index 0/1 is excluded, or the cut eats the post itself and leaves an empty body.
  - Fixes future scrapes only. The 404 already archived keep their merged text; re-parsing
    them needs `replay.py --llm`, which spends Gemini quota.
- **FB DOM is unstable:** all selectors live in the FRAGILE block of `scraper.py`
  with a multi-selector fallback chain; expect periodic tuning. `FacebookBlock`
  detection aborts a run on a checkpoint/login wall (never retries).
- **Docs drift:** the code is the source of truth for thresholds — keep this file
  and `README.md` in sync when key decisions change.

## Where the rest lives

`CLAUDE.md` holds what every session needs. Everything else is a skill, loaded on demand.
**These pointers are load-bearing** — `tests/test_docs_integrity.py` fails if a skill is
not named here, or if a name here has no skill, because a note nobody loads is a note
nobody has.

**Doing something** — load before you start:

| skill | for |
|---|---|
| `apply-replay` | re-classifying stored listings with `replay.py --apply` |
| `zone-update` | changing the green zone, the no-amber areas, or `MAX_WALK_MINUTES` |
| `fix-location` | a flat with no dot, or a dot in the wrong place |
| `geo-verify` | measuring whether a geocoding change actually helped |
| `rebuild-data` | regenerating anchors, buildings, neighborhoods, amenities, landmarks |
| `prompt-tuning` | editing the Hebrew prompt or comparing Gemini models |
| `fb-selectors` | the scraper stopped reading posts, or FB changed its DOM |
| `scraper-volume` | changing cadence, groups, depth, or the hot pass — **read first** |
| `health-triage` | a run was lost, the lock is wedged, `doctor` is failing |
| `osrm-docker` | OSRM down, Docker won't start, orphaned unix sockets |
| `testing-conventions` | writing a test without corrupting real operational data |
| `write-a-note` | recording a finding here or in a skill so it survives |

**Knowing something** — reference, loaded when the work touches it:

| skill | covers |
|---|---|
| `llm-notes` | the model ladder, quota, budget, retries |
| `geocoding-notes` | placement: anchors, pooling, interpolation, masks, landmarks |
| `storage-notes` | dedup identity, enrichment, pruning, the archive |
| `telegram-notes` | alerts, the 64-byte callback cap, digests, routing |
| `dashboard-notes` | the map, clustering, layers, the PWA snapshot |
| `scraper-notes` | the lock, wedged/crashed runs, watchdogs, keep-awake |
| `dead-ends` | **what was tried and measured to be worse — read before proposing** |
| `evidence-rules` | how to measure something here so the answer is trustworthy |

Live state (listings, quota, OSRM, whether a scrape is running) is printed by
`.claude/hooks/session_start.py` — it is measured, not written down, because a
hand-dated status block drifts.

Guards that will stop a command: `.claude/hooks/guard.py` blocks `replay --apply` while a
scrape runs or OSRM is down, the A/B harnesses during a scrape, `git add` of `.env` /
`auth/` / `data/`, a piped `pytest`, and `docker` commands that could destroy `osrm_bgu`.
