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
# Hard filter thresholds (from the spec)
# ---------------------------------------------------------------------------
MAX_PRICE_PER_ROOM_ILS = 2000      # per roommate, excluding utilities
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
# Campus destinations (lat, lon). Reported walk time is the MINIMUM over all of
# them — i.e. the shortest walk to any access point you actually use. These are
# the real BGU gates + your department building, from Google Maps pins you sent.
# Informational only (the green zone decides in/out). Add/remove points freely.
# ---------------------------------------------------------------------------
GATES = {
    "rager_north": {"lat": 31.2639703, "lon": 34.7992252},  # שער רגר צפוני
    "mexico":      {"lat": 31.2623329, "lon": 34.8056559},  # שער מקסיקו
    "aliya":       {"lat": 31.2612680, "lon": 34.8011969},  # שער העלייה
    "se_building": {"lat": 31.2649620, "lon": 34.8020603},  # Software & Info Systems Eng bldg
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

# ---------------------------------------------------------------------------
# Auto-scraper (increment 2). Conservative by design — see the SAFETY
# CONSTRAINTS section in CLAUDE.md. A persistent real browser profile (log in
# once via login.py), long randomized delays, a rotating subset of groups per
# run, dry-run unless --live. Do NOT crank these up: the account is the user's
# only Facebook account.
# ---------------------------------------------------------------------------
SCRAPER_PROFILE_DIR = AUTH_DIR / "chrome_profile"  # persistent login session
SCRAPER_HEADLESS = False                # never headless — see CLAUDE.md
SCRAPER_MAX_SCROLLS = 4                  # how far down each group to scroll
SCRAPER_SCROLL_DELAY = (4.0, 9.0)        # seconds between scrolls (randomized)
SCRAPER_GROUP_DELAY = (20.0, 45.0)       # seconds between groups (randomized)
SCRAPER_GROUPS_PER_RUN = 6               # rotating subset of FB_GROUPS per run
