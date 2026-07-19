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
MAX_WALK_MINUTES = 25             # OSRM edge safety-net (see below)

# In-range is decided PRIMARILY by your hand-drawn green zone (point-in-polygon,
# see green_zone.json / zones.py). OSRM walk time is informational + a safety
# net: a listing just OUTSIDE the polygon but within MAX_WALK_MINUTES is kept
# as a borderline NEEDS_DATA rather than dropped, so a good one near the line
# isn't lost to hand-drawing imprecision.
GREEN_ZONE_PATH = ROOT / "green_zone.json"

# Outside the green zone but within this distance of it = "acceptable, not
# preferred" (still a match, flagged amber). Beyond it = dropped.
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

# ---------------------------------------------------------------------------
# Geocoding. Static name table is primary (see geocode.py) for slang/
# neighborhood names. Nominatim is the fallback for real street addresses.
# It's ON now because the green-zone gate only needs a point on the right side
# of your boundary (coarse), not pinpoint accuracy — street-level Nominatim in
# Be'er Sheva is good enough for that. Unknown locations still flag NEEDS_DATA.
# ---------------------------------------------------------------------------
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
    "https://www.facebook.com/groups/708432163853635",
    "https://www.facebook.com/groups/167457006612972",
    "https://www.facebook.com/groups/279135451973",
    "https://www.facebook.com/groups/989159401625656",
    "https://www.facebook.com/groups/501446271648548",
    "https://www.facebook.com/groups/712487315492862",
    "https://www.facebook.com/groups/2835281153355520",
]

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
NOTIFY_ON_MATCH = True
NOTIFY_ON_NEEDS_DATA = True        # master switch for near-miss pings
# Most real posts omit the price (negotiated in DMs), so they land in
# NEEDS_DATA. To keep those pings worth reading, only alert on a near-miss that
# is actually promising: in/near the green zone AND with enough rooms free
# (i.e. a good place that just didn't state a price). Non-promising near-misses
# are still saved to SQLite — you just aren't pinged. Set False to ping on every
# NEEDS_DATA regardless.
NEEDS_DATA_ONLY_PROMISING = True
# ...but ALWAYS alert a near-miss whose fit score reaches this, even if it isn't
# "promising" by the rooms/zone heuristic above — a genuinely good-looking place
# is worth surfacing even when it still needs more details. Set None to disable.
NEEDS_DATA_MIN_SCORE = 60

# ---------------------------------------------------------------------------
# Auto-scraper (increment 2). Conservative by design — see the SAFETY
# CONSTRAINTS section in CLAUDE.md. A persistent real browser profile (log in
# once via login.py), long randomized delays, a rotating subset of groups per
# run, dry-run unless --live. Do NOT crank these up: the account is the user's
# only Facebook account.
# ---------------------------------------------------------------------------
SCRAPER_PROFILE_DIR = AUTH_DIR / "chrome_profile"  # persistent login session
SCRAPER_HEADLESS = False                # never headless — see CLAUDE.md
SCRAPER_MAX_SCROLLS = 6                  # normal scroll depth per group
SCRAPER_SCROLL_CAP = 12                  # hard cap when still chasing MIN posts
SCRAPER_MIN_POSTS_PER_GROUP = 5          # keep scrolling until at least this many
SCRAPER_SCROLL_DELAY = (4.0, 9.0)        # seconds between scrolls (randomized)
SCRAPER_GROUP_DELAY = (20.0, 45.0)       # seconds between groups (randomized)
# groups per run: a RANDOM fraction of all groups, between these bounds (⅓–½)
SCRAPER_GROUPS_FRACTION = (1 / 3, 1 / 2)

# Each Telegram save/dismiss tap nudges a listing's score by this much, per user
# (2 people saving in the group = +50), so the group's votes shape the ranking.
MARK_SCORE_DELTA = 25
# Only process posts newer than this many hours. FB shows relative times
# (minutes/hours under 24h, then days/dates), which the scraper reads from the
# post's timestamp link — so a 24h cutoff is exact. Posts whose age can't be
# read (timestamp not rendered) are KEPT, not dropped, so a recent listing is
# never lost to a missed timestamp. Set to None to disable the age filter.
SCRAPER_MAX_POST_AGE_HOURS = 24
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
