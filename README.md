# BGU Housing Bot

Finds apartment-share listings near Ben-Gurion University from Hebrew Facebook
posts, filters them against your rules, verifies the real walking time to
campus, and pings you on Telegram.

**Two ways to feed it:** _manual mode_ (`manual.py` — paste a post, zero Facebook
risk) and the _auto-scraper_ (`main.py` — a conservative Playwright reader for
your groups, dry-run by default). Both run the exact same pipeline. See
**Auto-scraper** below before running the scraper.

The pipeline: `post text → Gemini (Hebrew NLP) → hard filters → geocode →
in-range check against your hand-drawn green zone → OSRM walk time → SQLite +
Telegram alert`.

**In-range is decided by the green zone you drew** (`green_zone.json`), in three
tiers: **inside → preferred** (✅ MATCH), **within 500m of it → acceptable, not
preferred** (🟡 MATCH nearby), **beyond 500m → dropped**. OSRM reports the actual
walk time for context. The named blacklist (Ramot, Neve Zeev, …) is a separate
instant-drop applied before any of this.

Your four Facebook groups are already registered in `config.py` (`FB_GROUPS`)
for the scraper increment.

---

## Setup (Windows, one time)

Do steps 1–3 first — the OSRM download/processing runs in the background while
you finish the rest.

### 1. Install Python + Docker

- **Python 3.11+** from <https://www.python.org/downloads/> — tick **“Add
  python.exe to PATH”** during install.
- **Docker Desktop** from <https://www.docker.com/products/docker-desktop/>
  (needed only for OSRM). Launch it once so the engine is running.

Verify in PowerShell:

```powershell
python --version
docker --version
```

### 2. Set up OSRM (local walking-distance server)

Israel is a small map, so this is quick. In an **empty folder** (e.g. `C:\osrm`):

```powershell
cd C:\osrm

# a) download the Israel + Palestine map extract
curl.exe -O https://download.geofabrik.de/asia/israel-and-palestine-latest.osm.pbf

# b) process it with the FOOT profile (three steps)
docker run -t -v ${PWD}:/data ghcr.io/project-osrm/osrm-backend osrm-extract   -p /opt/foot.lua /data/israel-and-palestine-latest.osm.pbf
docker run -t -v ${PWD}:/data ghcr.io/project-osrm/osrm-backend osrm-partition  /data/israel-and-palestine-latest.osrm
docker run -t -v ${PWD}:/data ghcr.io/project-osrm/osrm-backend osrm-customize  /data/israel-and-palestine-latest.osrm
```

Then start the server (leave this window open while the bot runs):

```powershell
docker run -t -i -p 5000:5000 -v ${PWD}:/data ghcr.io/project-osrm/osrm-backend osrm-routed --algorithm mld /data/israel-and-palestine-latest.osrm
```

Quick test (new PowerShell window) — should return JSON with a duration:

```powershell
curl.exe "http://localhost:5000/route/v1/foot/34.79,31.25;34.8015,31.2622?overview=false"
```

### 3. Create the Telegram bot

1. In Telegram, message **@BotFather** → `/newbot` → follow prompts → copy the
   **bot token**.
2. Message **@userinfobot** to get your numeric **chat id**.
3. Send your new bot any message once (so it’s allowed to message you).

### 4. Get a free LLM key

- Gemini free tier: <https://aistudio.google.com/apikey> → create key.
- Privacy note: Google’s **free** tier may use prompts to improve their
  products. If that bothers you (posts contain phone numbers), you can later
  switch to a fully-local model — see “Swapping the LLM” below.

### 5. Install the project

```powershell
cd path\to\bgu_housing_bot
pip install -r requirements.txt
playwright install chromium   # for the scraper (next increment); harmless now
```

### 6. Add your secrets

Copy `.env.example` to `.env` and fill in the four values
(`GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
`.env` is git-ignored — keep it that way.

---

## Run it

With the OSRM window open:

```powershell
python manual.py
```

Paste a Facebook post, add a line with just `END`, and watch it classify,
store, and alert. Try a couple of real posts to sanity-check the extraction.

---

## Your data

- **Green zone — done.** Loaded from your `Untitled_layer.kmz` into
  `green_zone.json` (31-point polygon covering the student neighborhoods west of
  campus). To change it later: redraw the shape in My Maps, export a new KMZ, and
  run `python load_zone_from_kmz.py path\to\NewLayer.kmz`.
- **Red areas → still needed.** Add every avoid-neighborhood (and common
  misspellings) to `BLACKLIST_NEIGHBORHOODS` in `config.py`. These are dropped
  before geocoding. Currently: Ramot, Neve Zeev, Nahal Ashan, Pelach 7.
- **Optional — geocoding hints.** Street addresses are geocoded automatically
  via Nominatim (Be'er Sheva). For slang/neighborhood-only posts, add the name →
  a point inside that area to `STATIC_TABLE` in `geocode.py` for more reliable
  placement (a few seeds are already there).

Also worth verifying once: the `GATES` coordinates in `config.py` (main gate is
from your spec; Soroka/north are approximate — drop pins and correct them). They
only affect the displayed walk time, not the in/out decision.

---

## Tuning the rules

All thresholds live at the top of `config.py`
(price ≤ 2000/room, ≥ 2 rooms free, ≤ 4 roommates, ≤ 25 min walk).

## Swapping the LLM

Set `LLM_PROVIDER = "openai_compatible"` in `config.py`, uncomment `openai` in
`requirements.txt`, and set `LLM_BASE_URL` / `LLM_MODEL` in `.env`. Works with a
local **Ollama** model (private, no rate limits) or **Groq** (free, fast).

## Auto-scraper (increment 2)

A conservative Playwright reader for your Facebook groups. Same pipeline as
manual mode — it just feeds it posts it scrolled past instead of ones you
pasted. It **only reads**: never posts, comments, messages, reacts, or clicks
anything. Read [CLAUDE.md](CLAUDE.md) → *SAFETY CONSTRAINTS* before touching the
pacing knobs; the account is your only Facebook account.

**How it stays safe (all in `config.py`):** a real, non-headless browser you log
into once; long randomized delays between scrolls and groups; only a rotating
subset of groups per run (`SCRAPER_GROUPS_PER_RUN`); and **dry-run by default —
it writes nothing and sends no alerts unless you pass `--live`.**

### One-time login

```powershell
python login.py
```

A real Chrome window opens on Facebook. Log in fully (including 2FA), land on
your normal feed, then press Enter in the terminal. The session is saved to
`auth/chrome_profile/` (git-ignored) and reused by the scraper. Re-run only if
Facebook logs you out.

### Run it

```powershell
python main.py          # DRY RUN — classify + print, writes nothing, no alerts
python main.py --live   # commit: dedup, store, and send Telegram alerts
```

Start with a few dry runs and read the summary — confirm posts are being read
and classified sensibly (FB's DOM shifts; if 0 posts come through, the selectors
in `scraper.py` need retuning — they're all in one clearly-marked block). Only
switch to `--live` once you trust it. On a live run it sends one Telegram
heartbeat when done, so **silence means something broke.**

### Schedule it ~2×/day (Windows Task Scheduler)

The scraper is meant to run about twice a day — no more. Open **Task Scheduler**
→ *Create Task* (not *Basic Task*):

- **General:** name it e.g. `BGU housing scraper`. Leave *Run only when user is
  logged on* selected — the browser needs a visible desktop session (it is
  non-headless by design).
- **Triggers:** add two daily triggers (e.g. 09:00 and 19:00).
- **Actions:** *Start a program*
  - Program/script: `powershell.exe`
  - Arguments: `-Command "cd 'C:\path\to\bgu_housing_bot'; python main.py --live"`
- **Settings:** tick **“Run task as soon as possible after a scheduled start is
  missed”** — your PC may be asleep at the trigger time. Leave *Stop the task if
  it runs longer than* at a couple of hours as a safety net.

OSRM only affects the displayed walk time, so the scraper still works if the
OSRM Docker window isn't running — you just won't get walk minutes.
