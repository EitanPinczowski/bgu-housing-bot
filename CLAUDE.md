# BGU Housing Bot — project context

Personal tool to find apartment-share listings near Ben-Gurion University
(Be'er Sheva) from Hebrew Facebook group posts, filter them against fixed rules,
check they're within a hand-drawn walkable zone, and alert on Telegram.

## Current status

**Increment 1 (the whole non-Facebook pipeline) is BUILT and TESTED.** You can
paste a post into `manual.py` and it parses → filters → geocodes → grades
location → stores → alerts, with zero Facebook risk.

**Increment 2 (the Facebook auto-scraper) is BUILT** (`login.py`, `scraper.py`,
`main.py`; `pipeline.process_post` gained a `commit` flag for dry runs).
Dry-run by default; `--live` to commit + notify. Still needs real-world tuning
of the FB selectors in `scraper.py` and validating in dry-run against live
groups before trusting `--live`. See "Next task" for the original spec.

## Pipeline

`post text → Gemini (Hebrew NLP) → hard filters → geocode → green-zone tier
(+500m buffer) → OSRM walk time (informational) → SQLite + Telegram alert`

## Key decisions (do not silently reverse these)

- **LLM = Google Gemini free tier** (`gemini-2.5-flash`), behind a small
  interface in `llm.py` so it can swap to an OpenAI-compatible endpoint
  (Ollama/Groq). It uses guaranteed structured output and a Hebrew prompt whose
  core rule is *return null, never guess* (prevents hallucinated prices).
- **Output = local SQLite + Telegram.** No Google Cloud / Sheets (user had
  nothing set up; GCP setup was not worth the burden).
- **In-range = the user's hand-drawn green zone** (`green_zone.json`, from a
  Google My Maps KMZ), graded in three tiers by `zones.classify_location`:
  - `GREEN` inside the polygon → preferred match (✅)
  - `AMBER` within `BUFFER_METERS` (500m) of it → acceptable, not preferred (🟡)
  - `RED` beyond the buffer → dropped
  - `UNKNOWN` couldn't geocode → NEEDS_DATA
- **OSRM is informational only** now (reports walk minutes); the zone makes the
  in/out decision. The bot works even if OSRM isn't running.
- **Blacklist** (`config.BLACKLIST_NEIGHBORHOODS`: Ramot, Neve Zeev, Nahal
  Ashan, Pelach 7) is a separate hard instant-drop applied before geocoding.
- **Dedup** prefers the contact phone (survives reposts/cross-posting), else a
  hash of address+price+rooms. Written incrementally.
- **Filters** (`config.py`): ≤2000 ILS/room, ≥2 rooms free, ≤4 total roommates.
- Missing critical fields → kept as **NEEDS_DATA**, never silently dropped.

## Files

- `config.py` — all thresholds, gates, blacklist, `FB_GROUPS`, provider settings.
- `models.py` — `ListingExtract` (LLM schema) and `PipelineResult` (+`location_tier`, `preferred`).
- `llm.py` — Gemini extraction (provider-abstracted).
- `geocode.py` — static name table (primary) + Nominatim street fallback.
- `osrm.py` — local foot routing; min over gates; informational.
- `zones.py` — green polygon load, point-in-polygon, 500m tier classification.
- `storage.py` — SQLite dedup + listings.
- `notifier.py` — Telegram MarkdownV2 alerts (✅ preferred / 🟡 nearby / ⚠️ needs data).
- `pipeline.py` — `process_post(raw_text, source_url, group, ...)`: the funnel.
- `manual.py` — paste-a-post CLI (risk-free entry point).
- `load_zone_from_kmz.py` — regenerate `green_zone.json` from a new My Maps export.
- `green_zone.json` — the 31-point walkable polygon.
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
- Long randomized delays; a **rotating subset** of groups per run. Runs **6×/day
  daytime** (09/11/13/15/17/19, each +up to 25 min jitter so it isn't clockwork;
  no night runs). GROUPS_PER_RUN=8, MAX_SCROLLS=6. Raised over time at the user's
  request (2→4→6×/day) for more coverage — they accept the added risk. The real
  protections (real logged-in profile, home IP, read-only, human-like pacing)
  are unchanged. This is roughly the ceiling; don't push higher without a reason.
- **Dry-run by default** — print what it *would* process; only commit/notify
  when explicitly run with `--live`.
- Read-only: it never posts, comments, messages, or interacts. Only scrolls/reads.
- Do not add CAPTCHA-solving or detection-evasion beyond human-like pacing.

## Next task — build the scraper (increment 2)

Add three files + one small refactor:

1. **`pipeline.py` refactor:** add a `commit: bool = True` param to
   `process_post`. When `commit=False`: skip the `is_seen` early-return, and
   skip `mark_seen` / `save_listing` / `notify` — pure classify-and-return, for
   dry runs. (Currently there's a `notify` flag; fold it into `commit`.)

2. **`login.py`:** launch a Playwright **persistent context**
   (`chromium.launch_persistent_context(config.SCRAPER_PROFILE_DIR,
   headless=False, locale="he-IL", timezone_id="Asia/Jerusalem")`), open
   `https://www.facebook.com`, print "log in, then press Enter here", wait for
   input, close. Session persists in the profile dir for the scraper to reuse.

3. **`scraper.py`:** `open_browser()` (persistent context, non-headless) and
   `scrape_group(page, url) -> list[dict]`. For each group: `page.goto(url)`,
   scroll `SCRAPER_MAX_SCROLLS` times sleeping `random.uniform(*SCRAPER_SCROLL_DELAY)`
   between, then collect `[role="article"]` elements → `inner_text()` cleaned +
   a permalink (first `a[href]` containing `/posts/`, `/permalink/`, or
   `story_fbid`). Dedup by text within the group. Wrap each group in try/except
   so one failure doesn't kill the run. **FB's DOM is unstable — expect these
   selectors to need periodic tuning; keep them isolated and easy to edit.**

4. **`main.py`:** orchestrator. Dry-run unless `--live`. Select a rotating
   subset of `config.FB_GROUPS` (size `SCRAPER_GROUPS_PER_RUN`, persist the
   offset in `data/rotation.json`). For each selected group → `scrape_group` →
   for each post → `pipeline.process_post(text, source_url=permalink,
   group=url, commit=not dry_run)`. Sleep `random.uniform(*SCRAPER_GROUP_DELAY)`
   between groups. Print a per-run summary (counts by status). If `--live`, send
   one heartbeat Telegram ("run done: N posts, M matches") so silence signals a
   break. Intended to be scheduled ~2×/day via Windows Task Scheduler (use "run
   task as soon as possible after a missed start" since the PC may be asleep).

5. **`config.py` additions** (conservative defaults):
   ```python
   SCRAPER_PROFILE_DIR = AUTH_DIR / "chrome_profile"
   SCRAPER_HEADLESS = False
   SCRAPER_MAX_SCROLLS = 4
   SCRAPER_SCROLL_DELAY = (4.0, 9.0)     # seconds between scrolls
   SCRAPER_GROUP_DELAY = (20.0, 45.0)    # seconds between groups
   SCRAPER_GROUPS_PER_RUN = 6            # rotating subset per run
   ```

After the scraper works in dry-run against a couple of groups, wire up the Task
Scheduler entry and document it in the README.
