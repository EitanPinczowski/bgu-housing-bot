"""
Global configuration and hard filter thresholds.

Coordinate convention in THIS file: everything is stored as (lat, lon) with
named keys, human-readable. We only flip to OSRM's (lon, lat) order at the
single call site in osrm.py. This is deliberate — mixing the two orders is the
classic silent bug in routing code.
"""
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
AUTH_DIR = ROOT / "auth"
DB_PATH = DATA_DIR / "listings.sqlite"
DATA_DIR.mkdir(exist_ok=True)
AUTH_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Cheap keyword pre-filter — runs BEFORE the LLM. A post with none of these
# housing words at all (lost-pet posts, furniture sales, chit-chat) is dropped
# as NOT_AD without spending an LLM call — saving Gemini quota and, especially,
# the slow local fallback. Deliberately broad: only posts matching NONE are
# skipped, so real ads (which almost always say דירה/שותף/חדר…) get through.
# Set to [] to disable.
# ---------------------------------------------------------------------------
PREFILTER_KEYWORDS = [
    "דירה", "דירת", "שותף", "שותפה", "שותפים", "שותפות", "חדר", "חדרים",
    "להשכרה", "השכרה", "שכירות", "מפנים", "מתפנה", "מתפנים", "מושכר",
    'שכ"ד', "שכ״ד", "שכד", "סאבלט", "סבלט", "כניסה",
]

# ---------------------------------------------------------------------------
# Hard filter thresholds (from the spec)
# ---------------------------------------------------------------------------
MAX_PRICE_PER_ROOM_ILS = 2000      # per roommate, excluding utilities (hard drop above)
TARGET_PRICE_PER_ROOM_ILS = 1500   # your budget — used by the ⭐ fit score
MIN_AVAILABLE_ROOMS = 2            # rooms currently free for lease
MAX_TOTAL_ROOMMATES = 4            # total occupants in the whole apartment
# Bonus added to the fit score when the flat is furnished (a bed, table, and
# closet in each sleeping room). A one-way bonus — an unfurnished flat isn't penalized.
FURNISHED_BONUS = 10
# Bonus when the ad mentions a balcony or a garden/yard — a major, near-top-tier
# feature (compare: zone/walk/price = 25 each).
BALCONY_BONUS = 18
# Penalty for a high floor with NO elevator (or elevator not mentioned): it grows
# exponentially with the floor — -round(min(cap, base**(floor-1))) — so floor 2 ≈ -3,
# 3 ≈ -6, 4 ≈ -16, 5 ≈ -39, 6+ = -40. No penalty for floor ≤ 1, unknown floor, or a
# confirmed elevator.
FLOOR_PENALTY_BASE = 2.5
FLOOR_PENALTY_CAP = 40
MAX_WALK_MINUTES = 20             # AMBER = a walk of at most this many minutes to
                                  # the nearest campus gate (GREEN still = inside
                                  # the hand-drawn polygon). Beyond it = RED.
# Real listings use the OSRM walk time (osrm.py) for this. When OSRM is down, and
# for the whole-area map (can't route thousands of cells), we estimate walk time
# from straight-line distance to the nearest gate: minutes ≈ metres * DETOUR /
# SPEED. Calibrated so הבלוק (~520m straight to שער סורוקה) ≈ its ~8-min OSRM walk.
WALK_SPEED_M_PER_MIN = 80          # ~4.8 km/h
WALK_DETOUR_FACTOR = 1.25          # streets aren't straight lines
# Preferred move-in month (1–12). Listings entering around this month score a
# little higher — but this is deliberately the SMALLEST factor in the fit score
# (max +4), so it only breaks ties, never overrides price/location/rooms. Your
# target is 01/10, i.e. October. Set None to ignore entry dates entirely.
TARGET_MOVE_IN_MONTH = 10

# In-range is decided PRIMARILY by your hand-drawn green zone (point-in-polygon,
# see green_zone.json / zones.py). OSRM walk time is informational + a safety
# net: a listing just OUTSIDE the polygon but within MAX_WALK_MINUTES is kept
# as a borderline NEEDS_DATA rather than dropped, so a good one near the line
# isn't lost to hand-drawing imprecision.
GREEN_ZONE_PATH = ROOT / "green_zone.json"
# Neighborhood polygons where the 500m amber buffer does NOT apply — outside the
# green zone there is red (e.g. שכונה ד'). Same format as green_zone.json but a
# list under "zones". Missing file = no such areas (feature simply off).
NO_AMBER_ZONES_PATH = ROOT / "no_amber_zones.json"

# Deprecated: the amber boundary is now a 20-minute walk to a gate (see
# MAX_WALK_MINUTES), not a fixed ring around the polygon. Kept only so old
# references don't break; not used by the classifier anymore.
BUFFER_METERS = 500

# ---------------------------------------------------------------------------
# Campus gates (lat, lon, name). The alert reports the walk to the CLOSEST one
# and names it ("12 דק׳ הליכה משער רגר"). Informational only — the green zone
# decides in/out. "name" is what shows in Telegram.
# All four coords are from the Google Maps pins you sent.
# ---------------------------------------------------------------------------
GATES = {
    "rager":  {"lat": 31.2639703, "lon": 34.7992252, "name": "שער רגר"},
    "mexico": {"lat": 31.2623329, "lon": 34.8056559, "name": "שער מקסיקו"},
    "gate90": {"lat": 31.2649620, "lon": 34.8020603, "name": "שער 90"},
    "soroka": {"lat": 31.2612680, "lon": 34.8011969, "name": "שער סורוקה"},
}

# ---------------------------------------------------------------------------
# Neighborhood blacklist — dropped BEFORE routing (fast pre-filter only;
# OSRM remains the source of truth for anything that isn't an obvious no).
# Add the Hebrew spellings from your red-area map here.
# ---------------------------------------------------------------------------
BLACKLIST_NEIGHBORHOODS = [
    "רמות",        # Ramot
    "נווה זאב",    # Neve Zeev
    "נחל עשן",     # Nahal Ashan
    "פלח 7",       # Pelach 7
    # TODO: add every red-area name from your map, incl. common misspellings
]

# Neighborhoods where the 500m amber grace does NOT apply: anything OUTSIDE the
# green polygon there is treated as RED (dropped), not amber. Matched against the
# extracted address text (geresh/quote marks are ignored). A location still
# scores GREEN if it's actually inside the polygon — this only removes the buffer.
NO_AMBER_NEIGHBORHOODS = [
    "שכונה ד",     # neighborhood ד' — outside the polygon here is red, no buffer
    "שכונת ד",
]

# The ONLY numbered neighborhoods we want. A post that explicitly names a שכונה
# outside this set (e.g. שכונה א/ה/ו/ז/ט…) is an instant hard-drop, like the
# blacklist — only ב/ג/ד are relevant to this search. Matched on the address TEXT
# (see pipeline._neighborhood_letter); a plain street or a named area (הבלוק,
# הרובע…) is unaffected.
ALLOWED_NEIGHBORHOODS = ["ב", "ג", "ד"]
# Among the allowed ones, ב is preferred over ג and ד (which tie). A small fit-score
# tie-breaker (letter -> bonus points); letters not listed get 0.
NEIGHBORHOOD_BONUS = {"ב": 4}
# Neighborhood boundary polygons (שכונה ב/ג/ד) imported from OSM by
# load_neighborhoods.py — used by zones.neighborhood_of to resolve a listing's
# neighborhood from its coordinate (the fallback when the text doesn't name one).
NEIGHBORHOODS_PATH = ROOT / "neighborhoods.json"

# ---------------------------------------------------------------------------
# OSRM — local, self-hosted foot-routing server (see README).
# ---------------------------------------------------------------------------
OSRM_BASE_URL = "http://localhost:5000"

# ---------------------------------------------------------------------------
# LLM provider.  "gemini" (free tier) is the default. Swappable to a local /
# OpenAI-compatible endpoint (Ollama, Groq) without touching pipeline code.
# ---------------------------------------------------------------------------
LLM_PROVIDER = "gemini"            # "gemini" | "openai_compatible"
# Fallback when the primary hits its daily/rate quota (429). Gemini is fast and
# free but capped per day; when it's exhausted mid-run we switch to the local
# Ollama model so no post is missed. Once the primary 429s in a run, we route
# straight to the fallback for the rest of that run (Gemini's slow retry-backoff
# isn't paid per post). Next run tries the primary again. Set None to disable.
LLM_FALLBACK_PROVIDER = "openai_compatible"   # local Ollama (see LLM_* in .env)
# Model chosen for FREE-TIER DAILY QUOTA, not quality — quota is the binding
# constraint. This API key's free buckets (measured 2026-07, per-key specific):
#   gemini-flash-latest   -> gemini-3.5-flash : only 20 requests/DAY (too few)
#   gemini-2.0-flash / -lite                  : limit 0 (no free quota at all)
#   gemini-2.5-flash / -lite                  : 404 (not served to new keys)
#   gemini-flash-lite-latest                  : works, generous lite bucket  ✅
# The "lite" latest alias gets a much larger free daily allowance and handles
# this structured Hebrew extraction fine. If it ever regresses, check current
# free RPD at https://ai.google.dev/gemini-api/docs/rate-limits before changing.
GEMINI_MODEL = "gemini-flash-lite-latest"
# For "openai_compatible" (Ollama / Groq): set base_url + model in llm.py/.env
# Client-side pacing so we don't trip the free-tier RPM limit (which would 429 us
# onto the slow local fallback). Minimum seconds between Gemini calls.
GEMINI_MIN_INTERVAL_SEC = 4.0
# Beyond quota (429), also switch to the local fallback after this many CONSECUTIVE
# non-quota Gemini errors (transient 500s/timeouts) — so a Gemini hiccup doesn't
# fail post after post. Each failing post still gets served by the fallback.
LLM_MAX_CONSECUTIVE_ERRORS = 3

# ---------------------------------------------------------------------------
# Geocoding. Static name table is primary (see geocode.py) for slang/
# neighborhood names. Nominatim is the fallback for real street addresses.
# It's ON now because the green-zone gate only needs a point on the right side
# of your boundary (coarse), not pinpoint accuracy — street-level Nominatim in
# Be'er Sheva is good enough for that. Unknown locations still flag NEEDS_DATA.
# ---------------------------------------------------------------------------
# Google Maps geocoding (optional, most accurate) — OFF by default. It needs a
# billing account (a card on file) even to use the free $200/mo credit, so it's
# opt-in only. To enable: set this True, enable "Geocoding API" (+ "Places API"
# for slang names) in your Google Cloud project, and put the key in .env as
# GOOGLE_MAPS_API_KEY. Order when on: static table -> Google -> Nominatim, with
# results cached to data/geocode_cache.json. Left False, the bot uses the free
# path only (static table + Nominatim) and never touches a paid API.
USE_GOOGLE_GEOCODE = False

# Overpass (OpenStreetMap's query API) — FREE, no key/billing. Tried before
# Nominatim because OSM's name index resolves many Be'er Sheva Hebrew street names
# that Nominatim's geocoder returns nothing for. Bounded to the BS box and paced
# ~1 req/s; successful hits are cached like the others. We try a list of public
# mirrors in order and take the FIRST that responds — any single instance is often
# overloaded and times out. OSM data is identical across mirrors, so a mirror that
# answers with an empty result is authoritative (we don't keep retrying elsewhere).
USE_OVERPASS_FALLBACK = True
OVERPASS_URLS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_TIMEOUT_SEC = 15          # per-mirror; short so a dead mirror fails fast

USE_NOMINATIM_FALLBACK = True
NOMINATIM_USER_AGENT = "bgu-housing-bot/1.0 (personal apartment search)"
# Bounding box around Be'er Sheva, as Nominatim wants it:
# "lon_left,lat_top,lon_right,lat_bottom". Used with bounded=1 so a street name
# that also exists in another city can't geocode outside the city (which would
# silently drop a good listing). Widen slightly if a real edge address is missed.
BEER_SHEVA_VIEWBOX = "34.74,31.30,34.86,31.19"

# ---------------------------------------------------------------------------
# Facebook groups to scan (used by the auto-scraper — next increment).
# ---------------------------------------------------------------------------
FB_GROUPS = [
    "https://www.facebook.com/groups/227042837307326",   # verified test group (שכונה ב' + הבלוק)
    "https://www.facebook.com/groups/138595033004411",
    "https://www.facebook.com/groups/582276193473149",
    "https://www.facebook.com/groups/864908790226104",
    "https://www.facebook.com/groups/532324530266141",
    "https://www.facebook.com/groups/1730789290457027",
    "https://www.facebook.com/groups/322313854934686",
    "https://www.facebook.com/groups/2302505389980235",
    "https://www.facebook.com/groups/1637994659811132",
    "https://www.facebook.com/groups/170744879507",
    "https://www.facebook.com/groups/167457006612972",
    "https://www.facebook.com/groups/279135451973",
    "https://www.facebook.com/groups/501446271648548",
    "https://www.facebook.com/groups/712487315492862",
    # dropped — 0 matches ever as of 2026-07-20 (group_yield); re-add if desired:
    # "https://www.facebook.com/groups/708432163853635",
    # "https://www.facebook.com/groups/989159401625656",
    # "https://www.facebook.com/groups/2835281153355520",
]

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
NOTIFY_ON_MATCH = True
NOTIFY_ON_NEEDS_DATA = True        # master switch for near-miss pings
# Quality gate on ALERTS (not on storage): only ping a listing — whether MATCH
# or NEEDS_DATA — whose fit score (fit.py, 0–100) is at least this. Everything is
# still saved to SQLite/Sheets and shows up in the digest/top-N; low-scoring ones
# just don't buzz your phone. Raise to be pickier, lower to see more.
MIN_ALERT_SCORE = 70

# ---------------------------------------------------------------------------
# Auto-scraper (increment 2). Conservative by design — see the SAFETY
# CONSTRAINTS section in CLAUDE.md. A persistent real browser profile (log in
# once via login.py), long randomized delays, a rotating subset of groups per
# run, dry-run unless --live. Do NOT crank these up: the account is the user's
# only Facebook account.
# ---------------------------------------------------------------------------
SCRAPER_PROFILE_DIR = AUTH_DIR / "chrome_profile"  # persistent login session
SCRAPER_HEADLESS = False                # never headless — see CLAUDE.md
SCRAPER_MAX_SCROLLS = 15                 # normal scroll depth per group
SCRAPER_SCROLL_CAP = 25                  # hard cap when still chasing MIN posts
SCRAPER_MIN_POSTS_PER_GROUP = 20         # keep scrolling until at least this many
# Early-stop: the feed is newest-first, so once scrolling stops turning up NEW fresh
# (recent, not-already-seen) posts, everything below is old/seen — quit the group
# instead of grinding to SCROLL_CAP. Break when a pass adds no new fresh post for
# STOP_AFTER_STALE_PASSES passes in a row (after MIN_SCROLLS_BEFORE_STOP passes, so
# the feed has hydrated). This is the main runtime win on quiet groups.
SCRAPER_STOP_AFTER_STALE_PASSES = 2
SCRAPER_MIN_SCROLLS_BEFORE_STOP = 2
SCRAPER_SCROLL_DELAY = (4.0, 9.0)        # seconds between scrolls (randomized)
SCRAPER_GROUP_DELAY = (20.0, 45.0)       # seconds between groups (randomized)
# Scan EVERY group each run (user request), reading up to SCRAPER_MIN_POSTS_PER_GROUP
# recent posts each — the scroll cap stops early when a group has no more new posts.
# NOTE: scans ALL groups each run. The age + already-seen early-stops (above /
# scraper.py) keep each run SHALLOW — a run soon after another finds mostly seen
# posts and bails per group after a few passes — which is what makes the 7×/day
# cadence's total work comparable to the old 4×/day deep scans. Still a single
# personal account: raise the cadence only on an explicit, informed request (this
# 7×/day was one). When SCAN_ALL is True the coverage-rotation knobs below are unused.
SCRAPER_SCAN_ALL_GROUPS = True
# groups per run when NOT scanning all: a RANDOM fraction of all groups (⅓–½).
SCRAPER_GROUPS_FRACTION = (1 / 3, 1 / 2)
SCRAPER_RUNS_PER_DAY = 7            # 08–20 every 2h (early-stops keep each run light)
SCRAPER_MIN_SCRAPES_PER_DAY = 3     # each group read at least this often per day

# Each Telegram save/dismiss tap nudges a listing's score by this much, per user
# (2 people saving in the group = +50), so the group's votes shape the ranking.
MARK_SCORE_DELTA = 25
# Only process posts newer than this many hours. FB shows relative times
# (minutes/hours under 24h, then days/dates), which the scraper reads from the
# post's timestamp link — so a 24h cutoff is exact. Posts whose age can't be
# read (timestamp not rendered) are KEPT, not dropped, so a recent listing is
# never lost to a missed timestamp. Set to None to disable the age filter.
SCRAPER_MAX_POST_AGE_HOURS = 24
# Hover-to-reveal permalinks: for a post whose real link couldn't be read/reconstructed
# from its anchors (~60% of posts — FB renders the timestamp link's href lazily), briefly
# HOVER the timestamp so Facebook fills in the real permalink, then read it. This is the
# only extra interaction beyond scrolling (a hover, not a click) — bounded per run so it
# stays human-like on a single account. Set False to disable.
SCRAPER_HOVER_FOR_LINK = True
# We hover a post when it's missing a link OR an age, so nearly every fresh post gets
# hovered — hence the higher cap. Already-seen posts are skipped BEFORE hovering (see
# scrape_group), so the 2nd–7th daily runs stay cheap; only run 1 hovers in bulk.
SCRAPER_MAX_HOVERS_PER_RUN = 300     # hard cap on hovers per run
SCRAPER_HOVER_MAX_PER_POST = 3       # candidates to try per post
# The hover both reveals the permalink href AND pops a date tooltip (FB renders the
# date in English even under he-IL, e.g. "Tuesday, July 21, 2026 at 12:56 PM"), which
# fixes post-age detection that the Hebrew scrambled timestamp text otherwise breaks.
# 0.6s gives the tooltip time to appear (the href alone is faster).
SCRAPER_HOVER_WAIT_SEC = 0.6
# Batch alerts: instead of pinging the group per matching post mid-run, collect a
# run's matches and send ONE header + the top-K ranked alerts at the end (photos +
# vote buttons intact). Cuts noise now that we scan 7×/day; the rest stay saved
# (DB/Sheet) and still surface in the morning/evening top-N digest. False = the old
# per-post behaviour. Only affects --live runs.
SCRAPER_BATCH_ALERTS = True
SCRAPER_ALERT_TOP_K = 5
# OCR image-only posts: many FB housing posts are a PHOTO of the text with only a
# tiny caption, so they fail the text gate and are lost. When on, the scraper keeps
# a thin-text post that has an image, and the LLM reads the ONE image (Gemini only)
# to extract the fields. Strictly bounded so the free Gemini quota isn't blown:
# at most SCRAPER_MAX_OCR_PER_RUN image extractions per run, one image each; a post
# counts as "thin" (image carries the text) under OCR_MIN_TEXT_CHARS characters.
SCRAPER_OCR_IMAGE_ONLY = True
SCRAPER_MAX_OCR_PER_RUN = 12
OCR_MIN_TEXT_CHARS = 40
# Occasionally skip a scheduled LIVE run entirely (~1 in 8), so the 7×/day
# cadence isn't perfectly periodic — a real person doesn't check like clockwork.
# The skip is logged (SKIP line in data/search_log.txt) and sends no Telegram, so
# it just looks like a quiet slot. 0 disables. Only affects --live runs.
SCRAPER_SKIP_RUN_PROBABILITY = 0.12
# Click "See more" to expand truncated long posts before reading them, so buried
# details (price, dates) aren't lost. This is the ONLY place the scraper clicks
# anything — it's a harmless in-place expand, not a post/comment/like, but it is
# still an interaction, so it's toggleable. Set False for strictly scroll-only.
SCRAPER_EXPAND_SEE_MORE = True
# Save a screenshot to data/ when a group reads 0 posts (debug_<id>.png) or hits a
# checkpoint (checkpoint_<id>.png) — to tell a selector break apart from a real
# block. Off by default; images can accumulate.
SCRAPER_DEBUG_SCREENSHOTS = False

# Retention: after this many days, an archived post's raw_text/parsed_json is
# nulled (its dedup key + verdict are kept forever, so it's never rescanned), to
# bound DB growth. Replay stays useful within this window. Pruned at end of run.
POST_ARCHIVE_RETENTION_DAYS = 90
