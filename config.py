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
# A listing that names no street AND scores no better than this is dropped rather than
# kept as NEEDS_DATA (user's rule, 2026-08-03). "No street" is geocode.has_location:
# a street counts even with no house number, a bare neighbourhood does not, and neither
# does an institution or a description. Tested against the RAW fit score, never the
# voted one, so replay gives the same verdict whatever the group has clicked since.
MIN_SCORE_WITHOUT_ADDRESS = 50
# Bonus added to the fit score when the flat is furnished (a bed, table, and
# closet in each sleeping room). A one-way bonus — an unfurnished flat isn't penalized.
FURNISHED_BONUS = 10
# Bonus when the ad mentions a balcony or a garden/yard — a major, near-top-tier
# feature (compare: zone/walk/price = 25 each).
BALCONY_BONUS = 18
# Small one-way bonus when the post has photo(s) — a listing with pictures is more
# real/serious than a bare text post. Not a penalty when photos are absent.
PHOTO_BONUS = 6
# A contact advertising at least this many DISTINCT numbered flats is an agency, not
# a private landlord (storage.phone_listing_count). Detected from the data rather than
# by matching "תיווך" in the text — many brokers never write it and many posts mention
# it about someone else. Brokers usually mean a fee, so they're labelled in the alert
# and take a fit penalty; they are NOT dropped, since some list good flats.
BROKER_MIN_LISTINGS = 4
BROKER_PENALTY = 12
# Penalty when the post is explicitly looking for FEMALE roommates ("מחפשות שותפה",
# "שותפות", "בנות בלבד") — not relevant to this search. Deterministic text match
# (see pipeline._seeks_female_roommates); does NOT fire on the neutral שותף/שותפים.
FEMALE_ROOMMATE_PENALTY = 15
# Penalty for a high floor with NO elevator (or elevator not mentioned): it grows
# exponentially with the floor — -round(min(cap, base**(floor-1))) — so floor 2 ≈ -3,
# 3 ≈ -6, 4 ≈ -16, 5 ≈ -39, 6+ = -40. No penalty for floor ≤ 1, unknown floor, or a
# confirmed elevator.
FLOOR_PENALTY_BASE = 2.5
FLOOR_PENALTY_CAP = 40
# How close to the zone boundary a point must be before we stop trusting a LOW-precision
# geocode. Within this many metres, a street-level/area point (we know the street but not
# the house) can't tell green from red, so the listing is flagged NEEDS_DATA instead of
# being confidently accepted or dropped. An exact/interpolated point keeps its real tier.
EDGE_UNCERTAIN_METERS = 150
# The green zone is traced BY HAND in Google My Maps, so its boundary carries real
# drawing error — yet a PRECISE point 5 m outside it was confidently downgraded from
# GREEN to AMBER (worth 15 score points and the ✅ label). Measured 2026-07-30: 35 of
# 239 placed listings sit within 50 m of that line, so the polygon's own error
# dominates the verdict there. A precise point this far OUTSIDE the polygon is
# therefore treated as GREEN. Deliberately small, and only outward — it must never
# start pulling in genuinely distant flats. 0 disables.
ZONE_EDGE_GRACE_METERS = 40
# For a listing we can only place at STREET level on a boundary-crossing street, judge it
# by how much of that street is actually in range (zones.street_in_range_fraction):
#   >= BOUNDARY_STREET_ACCEPT  -> trust the tier (e.g. השלום is 98% in-range — dropping
#                                 those was throwing away good apartments)
#   <= BOUNDARY_STREET_REJECT  -> RED (e.g. יהודה הלוי is 91% red)
#   in between                 -> genuinely ambiguous, so NEEDS_DATA: surfaced for a human
#                                 rather than silently dropped.
BOUNDARY_STREET_ACCEPT = 0.80
BOUNDARY_STREET_REJECT = 0.20
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
    "רמות",        # Ramot (also covers רמות ב'/ד' by substring)
    "נווה זאב",    # Neve Zeev
    "נחל עשן",     # Nahal Ashan
    "פלח 7",       # Pelach 7
    # Only שכונה ב/ג/ד are acceptable — every OTHER named Be'er Sheva neighborhood is
    # an instant hard-drop (the שכונה-letter areas א/ה/ו/ז/ח/ט/י are already dropped
    # pre-geocode by pipeline._neighborhood_letter + ALLOWED_NEIGHBORHOODS). These are
    # the NAMED areas that carry no letter. Substrings chosen to avoid false matches.
    "נאות אברהם", "נאות לון", "העיר העתיקה", "נווה מנחם", "כלניות", "סיגליות",
    "נחל בקע", "מרכז העיר", "שכונה דרום", "קרית יהודית", "הרובע", "רסקו",
    # 2026-08-05 — the two names that kept topping the DM digest's "couldn't map" list
    # (×2 each). Neither is in OSM, in the surveyed neighbourhoods, or in landmarks.json,
    # so nothing free can place them, and a govmap POI lookup is the measured trap (no
    # house number to validate the answer against). The user's call: both sit in the RED
    # zone, so there is nothing to place — dropping by name is the honest answer and it
    # costs no geocode or LLM work. `מרכז אזרחי` is a full phrase on purpose: bare `מרכז`
    # would swallow the `מרכז הנגב` landmark.
    "נאות הדרים", "מרכז אזרחי",
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
# The Docker container name for OSRM, so `python doctor.py --fix` can auto-start it
# when it's down (self-healing) instead of only alerting.
OSRM_DOCKER_CONTAINER = "osrm_bgu"

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
# THE MODEL LADDER. The free quota is per project per MODEL, so a second model carries
# its own RPD 500 and the day's ceiling becomes ~1,000 instead of 500 — which is what
# peak demand (~700-870 calls) actually needs. On a PER-DAY refusal the next rung is
# tried; only when every rung is spent does a run fall to the local model.
#
# 3.5 LEADS. 3.1 was briefly promoted on n=48 and the promotion was REVERTED when the
# confirmation run at n=100 overturned it (2026-08-07). Both gates failed at the larger
# sample, and the reason the first result looked so clean is instructive:
#   * At n=48 the two models disagreed on PRICE 5 times and 3.1 divided the total rent
#     by the residents correctly all five, which is what the prompt asks. At n=100 they
#     disagree 15 times (13 with 3.5 self-consistent, so not noise) and **3.1 returns the
#     WHOLE FLAT'S rent as the per-room price twice** — 2,800 where 3.5 said 1,400 for 2
#     roommates, 3,000 where 3.5 said 1,000 for 3. Neither model applies the rule
#     reliably; n=48 had simply caught 3.1's good cases.
#   * The failure modes are not equally expensive. 3.1's error INFLATES the price, and
#     the <=2000 filter then drops the flat silently. 3.5's usual miss is null, which
#     lands in NEEDS_DATA where a person still sees it. Prefer the visible failure.
#   * 3.1 does find prices 3.5 misses (11 of the 15 are 3.5=null vs a 3.1 number), so it
#     is not simply worse — the honest fix is the PROMPT's division rule, which both
#     models apply inconsistently. Until that is tightened, neither ordering is clearly
#     right and the safer failure mode wins.
# The ladder itself is unaffected: 3.1 remains the reserve rung, which is where the
# doubled daily capacity comes from.
GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
# Pinned, never `-latest`: that alias moves, and a silent model swap changes what is
# extracted from thousands of posts. Kept as the single-model name for callers that
# still ask for one (and as the ladder's first rung by default).
GEMINI_MODEL = GEMINI_MODELS[0]
# For "openai_compatible" (Ollama / Groq): set base_url + model in llm.py/.env
# Client-side pacing so we don't trip the free-tier RPM limit (which would 429 us
# onto the slow local fallback). Minimum seconds between Gemini calls.
# 4.5 = 13.3/min against a MEASURED RPM LIMIT OF 15 (AI Studio's Rate Limit page,
# 2026-08-06). It was 4.0 — exactly 15/min, zero headroom — and that page shows a peak
# of **17**, i.e. we were going over.
# This was proposed, then dropped the day before, on the reasoning that 429s were under
# 1% of requests so "the cap is plainly not being saturated". That inference was WRONG:
# the error rate was low because the DAILY ceiling bit first and exiled the run to
# Ollama, not because the per-minute cap had room. Measure the cap; do not infer it from
# how often it complains. TPM is nowhere near binding (28.55K of 250K), so requests —
# not tokens — are the constraint.
GEMINI_MIN_INTERVAL_SEC = 4.5
# Beyond quota (429), also switch to the local fallback after this many CONSECUTIVE
# non-quota Gemini errors (transient 500s/timeouts) — so a Gemini hiccup doesn't
# fail post after post. Each failing post still gets served by the fallback.
LLM_MAX_CONSECUTIVE_ERRORS = 3
# RETRY A TRANSIENT REFUSAL BEFORE GIVING THE POST — OR THE RUN — TO OLLAMA.
# There was no retry at all: ONE 429 latched Gemini off for the whole process, so a
# per-minute blip cost an entire run. Measured 2026-08-05 on the AI Studio usage
# dashboard: ~500-750 requests/day at ~100% success, with only **2-7 errors a day**,
# split between `429 TooManyRequests` and `503 ServiceUnavailable`. Under 1% of
# requests fail — and each failure was forfeiting a whole run to a ~2 min/post local
# model. A handful of retries a day buys all of that back.
# 3 attempts with the backoff below is ~60 s worst case for one post, against the
# ~40+ minutes of Ollama that one wrongly-latched run costs.
GEMINI_RETRIES = 3
# Cap on any SINGLE sleep. Google usually names its own delay in the error
# ("retry in 27.9s") and that is preferred when present, but it must not be able to
# park a run for minutes on one poisoned post.
GEMINI_RETRY_MAX_SLEEP_SEC = 30.0
# HOW MANY POSTS ONE RUN MAY SERVE FROM THE LOCAL FALLBACK BEFORE IT STOPS.
# The fallback exists so a quota-less run still gets SOMETHING; it is not meant to
# carry a whole run. Measured 2026-08-03: Gemini's daily window resets at 10:00
# Israel time (midnight US Pacific), so the 08:00 run always spends the PREVIOUS
# day's leftovers — that morning it had none, fell through to Ollama at ~63 s/post
# for 186 posts, and took 5h12m. It held the scraper lock the whole time, so the
# 10:00 and 12:00 runs both logged "another scraper session is running" and never
# ran. Three scheduled runs, one completion — and the 10:00 run it locked out is
# precisely the one that WOULD have had fresh quota.
# So the damage is not slowness, it is the rest of the day. 40 posts is ~40 min of
# Ollama, which fits comfortably inside the gap between runs. Posts left unread are
# never marked seen, so the next run picks them up: work is deferred, not lost.
LOCAL_FALLBACK_MAX_POSTS_PER_RUN = 40
# HOW MANY POSTS GO INTO ONE GEMINI REQUEST. The free tier meters REQUESTS PER DAY,
# not tokens, so this divides daily usage directly: measured 2026-08-02, ~865 calls
# against a ~1,000/day ceiling becomes ~175 at 5.
# The posts are small enough that this is nearly free — over the 4,935-post archive,
# p50 316 chars, p90 602, max 1,784, so five is ~3 KB. Raising it further trades a
# shrinking quota saving against a growing blast radius: every failed batch is redone
# post by post, and a bigger batch gives the model more chances to lose track of which
# result belongs to which post (which `llm._validate_batch` catches, at the cost of
# re-doing the lot). 1 disables batching entirely.
#
# SET TO 1 — BATCHING IS OFF UNTIL ITS A/B GATE PASSES. The code is complete and
# unit-tested, but the measurement that decides whether a BATCHED extract matches a
# SINGLE one has not been run yet: the first attempt compared against the archived
# parsed_json, which is written by two different models (186 posts on 2026-08-03 came
# from the Ollama fallback) and by older prompt versions, so it measured model drift,
# not batching. The valid control is a single Gemini call made in the same session,
# and Gemini's daily quota ran out before it could be run.
# Flip this to 5 only after scratchpad/batch_ab.py passes both gates: no post flips
# is_apartment_ad, and no MATCH-eligible post loses its price, rooms, or address.
# A wrong batch mis-attributes a whole listing — right flat, wrong phone and address —
# so this is not a knob to turn hopefully.
LLM_BATCH_SIZE = 1
# A CLIENT-SIDE DAILY CEILING, so we stop BEFORE Google does. Hitting the real 429 is
# what makes a run fall through to the local model and crawl; stopping ourselves a
# little early leaves the fallback for genuine surprises.
# Counted against the QUOTA WINDOW (10:00 Israel to 10:00 — see dates.quota_window),
# never the calendar day: a midnight-reset counter would hand the 08:00 run a full
# budget it does not have, which is worse than no counter at all.
# 0 disables the ceiling and leaves only Google's own 429.
#
# 480, AND THE NUMBER COMES FROM GOOGLE, NOT FROM A GUESS (2026-08-06). It was 900,
# chosen "under the ~1,000/day observed ceiling" — but a usage chart shows where you
# have BEEN, never where the cap IS, so 900 sat above the real limit and could never
# bind. The refusal states it outright:
#   Quota exceeded for metric: generate_content_free_tier_requests, limit: 500,
#   model: gemini-3.5-flash-lite
#   quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier, quotaValue: '500'
# Our own counter read 506 at that moment, so it was accurate all along — the ceiling
# was simply set too high to protect anything. 480 leaves ~20 for the OCR calls and
# retries that also spend quota.
# **If a future refusal names a different `limit:`, follow it** — `doctor`'s llm budget
# row reports the value Google last stated, precisely so this is not guessed again.
LLM_DAILY_BUDGET = 480

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
OVERPASS_TIMEOUT_SEC = 8           # per-mirror; short so a dead mirror fails fast (a
                                   # mirror that fails is then skipped for the rest of
                                   # the process — see geocode._dead_mirrors)

USE_NOMINATIM_FALLBACK = True
NOMINATIM_USER_AGENT = "bgu-housing-bot/1.0 (personal apartment search)"
# Bounding box around Be'er Sheva, as Nominatim wants it:
# "lon_left,lat_top,lon_right,lat_bottom". Used with bounded=1 so a street name
# that also exists in another city can't geocode outside the city (which would
# silently drop a good listing). Widen slightly if a real edge address is missed.
BEER_SHEVA_VIEWBOX = "34.74,31.30,34.86,31.19"

# ---------------------------------------------------------------------------
# Amenity & transit proximity (DISPLAY ONLY — deliberately NOT part of the fit
# score, which stays exactly as calibrated). Answers "what's daily life like at
# this address": the bus you actually use, and the gym.
#
# The data is precomputed into amenities.json by load_amenities.py so a run needs
# no network; amenities.py then routes from a listing to the nearest option with
# ONE OSRM /table call. Every piece is optional — a missing file, a dead OSRM, or
# nothing in range simply prints no amenity line.
# ---------------------------------------------------------------------------
AMENITIES_PATH = ROOT / "amenities.json"

# ---------------------------------------------------------------------------
# Live dashboard server (serve_dashboard.py) — reads SQLite on every request so the
# page is never stale, and is reachable from a phone.
#
# IT SHOWS OTHER PEOPLE'S PERSONAL DATA (landlord phone numbers and home addresses),
# so a token is REQUIRED on every route — there is no "no auth" mode. Put
# DASHBOARD_TOKEN in .env; if absent, one is generated into data/dashboard_token.txt
# on first run so a phone bookmark keeps working across restarts.
#
# The live server is LAN-ONLY by decision (user, 2026-08-02): no tunnel, no VPN.
# Away from home the answer is the published snapshot — see publish.py.
# ---------------------------------------------------------------------------
DASHBOARD_PORT = 8777              # 5000 is OSRM
DASHBOARD_POLL_SECONDS = 45        # how often an open page checks /api/version
# Cached listing images live here so Facebook URL expiry stops destroying them: only
# 8 of 349 listings have permanent Telegram file_ids, the rest are FB URLs that rot.
DASHBOARD_IMAGE_DIR = DATA_DIR / "images"

# --- publishing the snapshot to an always-on URL ---------------------------------
# The live server needs this PC awake; the scraper needs it too, so a hosted copy is
# always a SNAPSHOT, never live. publish.py pushes the dated self-contained page to a
# DEDICATED GitHub Pages repo — never the code repo, whose public git history would
# then hold landlords' phone numbers permanently.
# SITE_REPO_URL and PUBLISH_NOINDEX are read from the environment in publish.py, not
# here: .env is loaded per entry point, so a module-level getenv in config would run
# before load_dotenv and silently see nothing. Same reason notifier reads its token
# lazily. Unset -> `--publish` prints one line and does nothing, like the Sheets sink.
SITE_DIR = DATA_DIR / "site"          # local checkout of the published-site repo
DASHBOARD_MAX_IMAGE_BYTES = 4 * 1024 * 1024
# Only consider stops/POIs within this straight-line distance before routing —
# keeps the OSRM table small and stops us reporting a "nearby" stop that isn't.
# A target may override it with "max_meters" (the gym is a single destination 2-3 km
# from the search area, so the default would silently hide it from every listing).
AMENITY_MAX_METERS = 1500
# When two stops are about equally close, the more FREQUENT one is the better answer.
# Among stops within this many extra walking minutes of the nearest, pick the best
# headway. Without it we once reported a bus every 36 min while a 10-min line was
# 6 metres further away.
AMENITY_DETOUR_MINUTES = 2
# Daytime window used to turn a stop's weekday departure count into a headway
# ("a bus every ~N minutes"). (start_hour, end_hour), 24h.
AMENITY_HEADWAY_WINDOW = (7, 22)
# Israel Ministry of Transport GTFS — official open data, free, no key. Big
# (~100 MB zipped, stop_times.txt ~1 GB raw), so load_amenities.py streams it
# straight out of the zip and caches the download in data/.
GTFS_URL = "https://gtfs.mot.gov.il/gtfsfiles/israel-public-transportation.zip"
GTFS_CACHE_PATH = DATA_DIR / "israel-gtfs.zip"
# Which weekday's service to measure frequency on (0=Mon … 6=Sun). Tuesday is an
# ordinary Israeli working day (Fri/Sat service is very different).
GTFS_WEEKDAY = 1
# "The train station" = באר שבע מרכז, beside the central bus station — NOT
# באר שבע צפון/אוניברסיטה, which is already at campus. load_amenities.py locates it
# in the GTFS feed itself by name (`name_match`, which resolves to exactly one rail
# stop); the coordinate here is only the fallback if that lookup finds nothing.
TRAIN_STATION = {"lat": 31.2430, "lon": 34.7981, "name": "רכבת באר שבע מרכז",
                 "name_match": "באר שבע מרכז"}
# Stops within this far of the station count as being AT it — buses serve the
# adjacent central bus station, not the railway platform, so some slack is required.
# 250 m covers the terminal but stops short of the mall's stop (~300 m away), which
# a wider radius would wrongly credit as "a bus to the train".
TRAIN_STATION_RADIUS_M = 250
# What to report on each listing. Data-driven: a fourth amenity is a config edit
# plus (for a new "kind") a branch in load_amenities.py.
#   kind "bus_route"  — nearest stop served by `route`, reported PER DIRECTION
#   kind "bus_toward" — nearest stop with a bus heading to the train station
#   kind "poi"        — nearest of a set of named places resolved via Overpass
AMENITY_TARGETS = [
    {"key": "bus669", "kind": "bus_route", "route": "669", "street": "רגר",
     "label": "669 מרגר", "icon": "🚌"},
    {"key": "train", "kind": "bus_toward", "label": "לרכבת מרכז", "icon": "🚆"},
    # OSM still carries this mall's PRE-REBRAND name (קניון הנגב) — nothing in
    # Be'er Sheva is tagged עזריאלי at all. match_names is tried in order, so the
    # current name wins if/when OSM catches up, and the old one keeps it working.
    {"key": "gym", "kind": "poi", "query": "קניון עזריאלי הנגב",
     "match_names": ["עזריאלי הנגב", "קניון הנגב"], "max_meters": 4000,
     "label": "חדר כושר עזריאלי", "icon": "🏋️"},
]

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
    # Re-added 2026-07-27: it was pruned as "0 matches ever", but the archive now shows
    # the BEST match rate of any group (2 MATCH / 13 posts). Prune only on real yield —
    # see group_report.py.
    "https://www.facebook.com/groups/989159401625656",
    # dropped — 0 matches ever as of 2026-07-20 (group_yield); re-add if desired:
    # "https://www.facebook.com/groups/708432163853635",
    # "https://www.facebook.com/groups/2835281153355520",
]

# Yield-scaled scan depth. Every group is still visited each run (coverage), but a group
# that historically produces almost nothing is read SHALLOWLY and a productive one gets
# full depth — so matches per run rise without increasing total reads on the account.
# Depth is derived from the measured MATCH-per-post rate (storage.group_yield):
#   rate >= GROUP_RICH_RATE      -> full SCRAPER_MIN_POSTS_PER_GROUP
#   rate <= GROUP_POOR_RATE      -> GROUP_MIN_POSTS_FLOOR (never zero: a quiet group can
#                                   still post a gem, and a new group has no history yet)
# Set GROUP_YIELD_SCALING = False to go back to a uniform depth everywhere.
GROUP_YIELD_SCALING = True
GROUP_RICH_RATE = 0.05             # >=5% of posts became MATCHes -> full depth
GROUP_POOR_RATE = 0.015            # <=1.5% -> floor depth
GROUP_MIN_POSTS_FLOOR = 8          # never read fewer than this per group
GROUP_MIN_HISTORY = 25             # below this many archived posts, assume full depth

# --- "hot path" (python main.py --hot): a fast, SHALLOW check of only the best groups,
# so a great listing is seen in ~30-40 min instead of up to ~2h25m. Speed matters in this
# market — the first person to message often gets the flat.
# VOLUME IS NOT INCREASED: yield-scaling above cuts the normal runs from 2100 to ~1540
# post-reads/day, and 4 hot runs cost ~120/day -> ~1660 total, still ~21% BELOW the old
# 2100. Every safety rule is unchanged (real logged-in profile, randomized delays +
# jitter, daytime only, read-only, dry-run default, checkpoint-abort).
# NOTE (2026-07-30): this pass existed in code and in the volume budget below for days
# but was NEVER SCHEDULED — Task Scheduler had one scraper task running run_scraper.cmd
# with no arguments. Measured result: median time-to-detect 8.4 h (n=44), only 7 of 44
# listings seen within an hour. `update_schedule.cmd` finally wires it up; `stats.py`
# prints the lag so the effect is measured rather than assumed.
HOT_GROUP_COUNT = 3                # how many top-yield groups the hot path visits
HOT_MIN_POSTS = 10                 # shallow: just the newest posts in each

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
NOTIFY_ON_MATCH = True
NOTIFY_ON_NEEDS_DATA = True        # master switch for near-miss pings
# Quality gate on ALERTS (not on storage): only ping a listing — whether MATCH
# or NEEDS_DATA — whose fit score (fit.py, 0–100) is at least this. Everything is
# still saved to SQLite/Sheets and shows up in the digest/top-N; low-scoring ones
# just don't buzz your phone. Raise to be pickier, lower to see more.
# Measured 2026-07: at 85 only 14 of 44 MATCH listings alerted (median MATCH = 78), so
# ~68% of genuine matches never reached the phone. 75 sits just under the median and lets
# a clear majority through. Re-check against the score distribution if scoring changes.
MIN_ALERT_SCORE = 75
# A listing older than this is almost certainly gone — stop surfacing it in /top and the
# scheduled top-N so you don't chase dead flats. It stays in the DB/Sheet and in /search.
LISTING_STALE_DAYS = 21
# doctor/watchdog: complain if no scrape has COMPLETED in this many hours during active
# hours (08–20). Catches a sleeping PC or a disabled Task Scheduler job — previously the
# bot could go quiet for hours with nothing to show for it.
MAX_HOURS_BETWEEN_RUNS = 5
# A run is judged by PROGRESS, not elapsed time. Measured over 40 real runs: median 27
# min, but legitimate runs reached 99/195/268 minutes — those are the local-Ollama
# fallback runs (~199 s per post vs ~20 s on Gemini), so any wall-clock deadline short
# enough to catch a hang would kill real work whenever Gemini's quota runs out.
# The scraper touches data/scraper.heartbeat as it finishes posts/groups; if that stops
# for this many minutes the run is wedged and gets killed so the next one can proceed.
# Well above the slowest single post (~3.3 min) so a slow local-LLM run is never touched.
STALL_MINUTES = 30
# ...AND A HARD WALL-CLOCK CEILING ON TOP OF THAT (user, 2026-08-05).
# The stall test above only catches a run that stops PROGRESSING. A run that crawls is
# invisible to it: on 08-05 the 18:00 run was 90 minutes in, still on group 1 of 15 at
# ~2 min/post, heartbeat fresh the whole time — perfectly "healthy" and useless, holding
# the DB lock against every later slot.
# This reverses the deliberate choice recorded just above ("judged by PROGRESS, not
# elapsed time", because legitimate local-Ollama runs reached 99/195/268 minutes). Two
# things changed and make the ceiling safe now:
#   1. LOCAL_FALLBACK_MAX_POSTS_PER_RUN (40) ends a run before it can grind for hours —
#      verified firing twice on 08-04, where those 268-minute runs came from.
#   2. A transient 429/503 is retried rather than latching the whole run onto Ollama,
#      so falling back at all is now rare.
# Worst legitimate case left is ~40 capped local posts (~80 min) plus scraping overhead,
# which fits under this. Raise it if a real run is ever killed; do not lower it without
# re-checking those two mechanisms.
# Wall clock, NOT monotonic: a run that slept through the night (the 00:46 run on 08-05
# took 8.5 h for ~23 min of work) is exactly what this must catch.
MAX_RUN_MINUTES = 120
# HOW MANY TIMES TO RETRY OPENING THE BROWSER BEFORE GIVING UP THE RUN.
# Every traceback in the run log is the same call — `launch_persistent_context` — and it
# kills the run before it reads a single post: the slot is simply lost, with no END line
# to show for it (9 such runs in the 7 days to 2026-08-05). Two observed causes, both
# transient:
#   "Opening in existing browser session ... the profile is already in use by another
#    instance of Chromium"   -> a leftover Chromium still holding auth/chrome_profile
#   "TimeoutError: launch_persistent_context: Timeout 180000ms exceeded"
# `reap_orphan_browsers()` is precisely the cure for the first and already existed; it
# was just never called BEFORE a launch, only after a wedge was detected.
BROWSER_LAUNCH_RETRIES = 2
BROWSER_LAUNCH_RETRY_SLEEP_SEC = 5.0
# Per-navigation cap inside a group. The 2026-07-27 hang produced ZERO output — it stuck
# on the very first page load and sat there for 37h; a page timeout raises instead.
PAGE_TIMEOUT_MS = 90_000

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
# 6 FULL runs (08/10/14/16/18/20) + 4 HOT runs (12/15/17/19) — see update_schedule.cmd.
# Re-timed 2026-07-30 around where the listings actually are: 45 of 63 timed posts land
# 14:00–20:00, and the old even 2-hourly spacing gave the busiest hours the same lag as
# the empty ones. Between 14:00 and 20:00 something now runs every hour.
# Volume went DOWN: 7×251 = 1757 reads/day → 6×251 + 4×30 = 1626 (−7.5%).
SCRAPER_RUNS_PER_DAY = 6            # FULL runs only; the hot pass is counted separately
SCRAPER_MIN_SCRAPES_PER_DAY = 3     # each group read at least this often per day

# Each Telegram save/dismiss tap nudges a listing's score by this much, PER USER
# (2 people saving in the group = +50), so the group's votes shape the ranking. These
# votes STACK on top of the normalized 0–100 quality score (storage.effective_score is
# not clamped), so a well-endorsed listing can read above 100 — a human ⭐ is never
# swallowed by the ceiling. Keep this ≥ 10 (each vote must clearly move the needle).
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


def validate() -> None:
    """Fail fast on an obviously-broken config, with a clear message — call at the
    start of main.run()/manual so a bad edit surfaces at startup, not mid-run."""
    problems = []
    if not GATES:
        problems.append("GATES is empty — no campus gates to route to")
    try:
        if len([float(x) for x in BEER_SHEVA_VIEWBOX.split(",")]) != 4:
            problems.append("BEER_SHEVA_VIEWBOX must be 4 comma-separated numbers")
    except Exception:
        problems.append(f"BEER_SHEVA_VIEWBOX does not parse: {BEER_SHEVA_VIEWBOX!r}")
    if TARGET_PRICE_PER_ROOM_ILS > MAX_PRICE_PER_ROOM_ILS:
        problems.append(f"TARGET_PRICE_PER_ROOM_ILS ({TARGET_PRICE_PER_ROOM_ILS}) > "
                        f"MAX_PRICE_PER_ROOM_ILS ({MAX_PRICE_PER_ROOM_ILS})")
    if not 0 <= MIN_SCORE_WITHOUT_ADDRESS < 100:
        problems.append(f"MIN_SCORE_WITHOUT_ADDRESS ({MIN_SCORE_WITHOUT_ADDRESS}) must be "
                        "0-99; the fit score is 0-100, so 100 would drop every "
                        "placeless listing and a negative would drop none")
    if MIN_SCORE_WITHOUT_ADDRESS >= MIN_ALERT_SCORE:
        problems.append(f"MIN_SCORE_WITHOUT_ADDRESS ({MIN_SCORE_WITHOUT_ADDRESS}) >= "
                        f"MIN_ALERT_SCORE ({MIN_ALERT_SCORE}) — every placeless listing "
                        "good enough to alert about would be deleted first")
    if LLM_BATCH_SIZE < 1:
        problems.append(f"LLM_BATCH_SIZE ({LLM_BATCH_SIZE}) must be >= 1 "
                        "(1 disables batching; 0 or less would extract nothing)")
    if MAX_RUN_MINUTES and MAX_RUN_MINUTES <= STALL_MINUTES:
        problems.append(f"MAX_RUN_MINUTES ({MAX_RUN_MINUTES}) <= STALL_MINUTES "
                        f"({STALL_MINUTES}) — the wall-clock ceiling would fire before a "
                        "stall could ever be detected, making STALL_MINUTES dead code "
                        "and killing healthy runs")
    if LOCAL_FALLBACK_MAX_POSTS_PER_RUN < 1:
        problems.append(f"LOCAL_FALLBACK_MAX_POSTS_PER_RUN "
                        f"({LOCAL_FALLBACK_MAX_POSTS_PER_RUN}) must be >= 1 — 0 would "
                        "abandon a run the moment Gemini's quota ran out, losing posts "
                        "the local model could still have read")
    if not GREEN_ZONE_PATH.exists():
        problems.append(f"green-zone file missing: {GREEN_ZONE_PATH}")
    if problems:
        raise SystemExit("config error — fix config.py:\n  - " + "\n  - ".join(problems))
