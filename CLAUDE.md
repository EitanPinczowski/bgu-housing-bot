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
- **OSRM gives the amber walk time** for real listings (min over gates); when it's
  down, and for the whole-area map, a calibrated straight-line estimate is used —
  so the bot still classifies without OSRM running. (`BUFFER_METERS` is deprecated.)
- **Blacklist** (`config.BLACKLIST_NEIGHBORHOODS`: Ramot, Neve Zeev, Nahal
  Ashan, Pelach 7) is a separate hard instant-drop applied before geocoding.
- **Dedup** prefers the contact phone (survives reposts/cross-posting), else a
  hash of address+price+rooms. Written incrementally.
- **Filters** (`config.py`): ≤2000 ILS/room, ≥2 rooms free, ≤4 total roommates.
- Missing critical fields → kept as **NEEDS_DATA**, never silently dropped.

## Files

- `config.py` — all thresholds, gates, blacklist, `FB_GROUPS`, provider + scraper settings.
- `models.py` — `ListingExtract` (LLM schema, incl. `floor`) and `PipelineResult`.
- `llm.py` — Gemini extraction + Ollama fallback (provider-abstracted); rate-limit.
- `geocode.py` — static name table (primary) → cache → optional Google → Nominatim.
- `osrm.py` — local foot routing; min over gates (drives the 20-min amber boundary).
- `zones.py` — green polygon + no-amber (ד') polygons; walk-time tier classification.
- `fit.py` — 0–100 fit score → ⭐1–5 (zone, walk, price, rooms, freshness, entry date).
- `storage.py` — SQLite: dedup, listings, votes/marks, unknown-locations, fingerprints, post archive.
- `sheets.py` — optional Google Sheets sink (append, batch reconcile, sort, rebuild).
- `notifier.py` — Telegram MarkdownV2 alerts; group-vs-DM routing; albums; vote buttons.
- `pipeline.py` — `process_post(...)` funnel; `_classify(...)` reused by replay.
- `scraper.py` / `login.py` / `main.py` — Playwright reader, one-time login, orchestrator.
- `manual.py` — paste-a-post CLI (risk-free entry point).
- `top_listings.py` / `digest.py` / `dm_digest.py` — morning/evening top-N, recaps, DM digest.
- `bot_listener.py` / `watchdog.py` — vote-button listener; dependency health check.
- `replay.py` / `stats.py` — offline re-classify (+`--apply`) and funnel stats.
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
  user's request — currently **`SCRAPER_SCAN_ALL_GROUPS=True` (all 17 groups every
  run), `MIN_POSTS_PER_GROUP=20`, `MAX_SCROLLS=15`/`SCROLL_CAP=25`, 7×/day** (08–20
  every 2h). This is ~6× the earlier "rotating ⅓–½ subset, 5 posts" volume and is
  **past the old ceiling** — the user was given a clear high-risk assessment and
  chose it, on their only FB account. The real protections (real logged-in profile,
  home IP, read-only, human-like pacing, checkpoint-abort) are unchanged, but they
  don't offset raw volume. The recommended offset is fewer runs/day (e.g. 3); do
  not raise volume further without an explicit, informed request.
- **Dry-run by default** — print what it *would* process; only commit/notify
  when explicitly run with `--live`.
- Read-only: it never posts, comments, messages, or interacts. Only scrolls/reads.
- Do not add CAPTCHA-solving or detection-evasion beyond human-like pacing.

## Working notes

- **Tuning workflow:** after changing the green zone, `MAX_WALK_MINUTES`, `fit.py`,
  or a threshold, run `python replay.py` to preview which stored listings flip,
  then `python replay.py --apply` to write it (updates the DB + rebuilds the
  Sheet, no Telegram). `stats.py` shows the funnel.
- **Geocoding gaps:** listings whose location the LLM extracted but geocoding
  couldn't map are logged (`unknown_locations`) and surfaced by the daily DM
  digest — pin the frequent ones into `geocode.STATIC_TABLE`. The zone can be
  regenerated from a My Maps KMZ via `load_zone_from_kmz.py`.
- **FB DOM is unstable:** all selectors live in the FRAGILE block of `scraper.py`
  with a multi-selector fallback chain; expect periodic tuning. `FacebookBlock`
  detection aborts a run on a checkpoint/login wall (never retries).
- **Docs drift:** the code is the source of truth for thresholds — keep this file
  and `README.md` in sync when key decisions change.
