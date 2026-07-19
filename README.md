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

## Google Sheets (optional organized DB)

Mirror every match / near-miss into a shared Google Sheet you can sort and
filter by hand, with its own row-level dedup. SQLite stays the fast local cache;
the Sheet is additive. Disabled until you set it up — the bot runs fine without.
The sheet is kept **sorted by rating (score), best first** — re-sorted at the end
of each run and after every vote. Transient Google API errors are retried with
backoff, so a blip no longer drops a whole run's rows.

1. In **Google Cloud Console**: create a project → enable the **Google Sheets
   API** → create a **service account** → add a **JSON key** and download it.
2. Save that file as **`auth\google_service_account.json`** (the `auth\` folder
   is git-ignored, so the key never gets committed).
3. Create a Google Sheet. Open the JSON and copy the `client_email` value, then
   **Share** the sheet with that email as **Editor**.
4. Copy the sheet's id from its URL
   (`docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`) into `.env`:
   ```
   GOOGLE_SHEET_ID=THIS_PART
   ```
5. `pip install gspread` (already in `requirements.txt`). Done — the next `--live`
   run appends a header row and one row per listing, skipping any dedup_key it
   already has.

## Swapping the LLM (local model via Ollama)

The default is Gemini (`gemini-flash-lite-latest`, free tier). You can switch to
a **fully local** model instead — nothing leaves your PC (phone numbers stay
private) and there's no daily quota. The pipeline code doesn't change.

**Hardware note (this PC):** Snapdragon X Plus (ARM64), 31 GB RAM, no CUDA GPU.
Ollama runs **CPU-only** here (the Adreno GPU and Hexagon NPU aren't used by
Ollama/llama.cpp), so it's slower than a cloud model — fine for a background
scraper that handles a few dozen posts a few times a day, not for real-time use.
RAM is plenty for a 9B model.

### Steps

1. **Install Ollama** (Windows ARM64 build) from <https://ollama.com/download>
   — or `winget install Ollama.Ollama`. It runs as a background service.
2. **Pull a Hebrew-capable model:**
   ```powershell
   ollama pull gemma2:9b        # good Hebrew + JSON following (~5.5 GB)
   # ollama pull gemma2:2b      # much faster, weaker Hebrew (fallback)
   ```
3. **Install the client + point the bot at Ollama:**
   ```powershell
   pip install openai
   ```
   In `.env`, uncomment and set:
   ```
   LLM_BASE_URL=http://localhost:11434/v1
   LLM_MODEL=gemma2:9b
   LLM_API_KEY=ollama
   ```
4. **Flip the provider** in `config.py`: `LLM_PROVIDER = "openai_compatible"`.
5. Test with `python manual.py` (paste a real Hebrew post). Watch the extraction
   quality and speed; if it's too slow, drop to `gemma2:2b`.

To go back to Gemini, set `LLM_PROVIDER = "gemini"`. **Groq** (free, fast cloud,
OpenAI-compatible) works the same way — set `LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY`
to your Groq values instead.

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

### Schedule it every 2 h, 08:00–20:00 (Windows Task Scheduler)

**Already set up.** A scheduled task named **`BGU Housing Scraper`** runs the
scraper **every 2 hours from 08:00 to 20:00** daily (08/10/12/14/16/18/20, 7
runs), each with **up to 25 min of random delay** so the runs don't fire on the
exact minute (clockwork timing is the main thing that looks automated to
Facebook). Each run sweeps a
random **⅓–½ of the groups** (`SCRAPER_GROUPS_FRACTION`) and keeps scrolling a
group until it has at least **5 posts** (`SCRAPER_MIN_POSTS_PER_GROUP`, hard cap
`SCRAPER_SCROLL_CAP`). It calls `run_scraper.cmd`,
which pins the correct Python, sets UTF-8, and runs `python main.py --live`,
appending all output to `data\scraper_runs.log`. The task is configured to *run
only when you're logged on* (the browser is non-headless by design), to *start
as soon as possible after a missed start* (your PC may be asleep), and to run on
battery.

Manage it from PowerShell:

```powershell
# see it / its next run time
Get-ScheduledTask -TaskName "BGU Housing Scraper"
Get-ScheduledTaskInfo -TaskName "BGU Housing Scraper"

# run it right now to test (this does a real --live run: writes + Telegram)
Start-ScheduledTask -TaskName "BGU Housing Scraper"

# watch the log
Get-Content data\scraper_runs.log -Tail 40 -Wait

# change the times, disable, or remove
Disable-ScheduledTask -TaskName "BGU Housing Scraper"
Unregister-ScheduledTask -TaskName "BGU Housing Scraper" -Confirm:$false
```

To recreate it on another machine (or after editing), the exact registration
command is in the project history; or use Task Scheduler's GUI → the task is
under the root folder.

OSRM only affects the displayed walk time, so the scraper still works if the
OSRM Docker container isn't running — you just won't get walk minutes. The
`osrm_bgu` container is set to restart with Docker Desktop; make sure Docker
Desktop is set to start on login if you want walk times on scheduled runs.

### Helper tasks (also scheduled)

- **`BGU Watchdog`** (`watchdog.py`) — runs 08:30/11:30/14:30/17:30, 30 min before
  each scrape. Checks OSRM + Ollama and pings Telegram if a dependency is down,
  so you can fix it before a run degrades. (Facebook-login loss is caught by the
  scraper's own "0 posts" alert.)
- **`BGU Morning`** (`top_listings.py 3 24`) — every day at 08:00, posts the
  **top 3** listings of the last 24 h to Telegram as **full listings** (photo
  album + details + ⭐/🗑 vote buttons), ranked by the **vote-adjusted** score.
- **`BGU Digest`** (`top_listings.py 5 13`) — every evening at 20:00, posts the
  **top 5 of the day** (last 13 h) the same way. Run either by hand, e.g.
  `python top_listings.py 5 24` (top 5 over the last 24 h). The old text recap is
  still there as `python digest.py 3` (last 3 days) if you want a plain list.

Ranking uses the fit score (`fit.py`) **plus the group's votes**: each ⭐ on a
listing adds `MARK_SCORE_DELTA` (25) and each 🗑 subtracts it, per person. The
score also has a small **freshness** factor — a just-posted listing outranks a
day-old repost. Photos re-post reliably because the first alert caches Telegram
**`file_id`s** (which never expire) in the DB; only listings never sent with a
photo fall back to text.

Alerts include the apartment **photos as an album** automatically when the post
has several.

### Alert buttons + listener

Each alert carries **⭐ מעניין / 🗑 הסר** buttons. Tapping one records your triage
in the `marks` table (SQLite) and the sheet's `mark` column, and the button
updates to show the live tally, e.g. **⭐ מעניין (3)**. **Votes are final —
one per person per apartment**: a repeat tap (or trying to switch) just shows
"כבר הצבעת" and changes nothing. This is handled by **`bot_listener.py`**, a
small always-on process that long-polls Telegram for the taps — it autostarts at
login via a **Startup shortcut** ("BGU Bot Listener", windowless `pythonw`).
It's the only process that *reads* Telegram; everything else only sends. If it's
not running, taps just queue and are processed next time it starts. Run it by
hand to see logs: `python bot_listener.py`.

### Facebook safety extras

- **Checkpoint abort.** If a run lands on a Facebook checkpoint / login /
  "confirm it's you" wall, the scraper stops the whole run immediately (never
  retries into it) and sends a distinct ⛔ Telegram alert telling you to re-login
  via `login.py`. This is the one condition to act on before the next run.
- **Occasional skipped run.** ~1 in 8 live runs is skipped on purpose
  (`SCRAPER_SKIP_RUN_PROBABILITY`) so the cadence isn't clockwork; the skip is
  logged as a `SKIP` line in `data/search_log.txt` and sends no Telegram.

### Tests

Fast, offline unit tests cover the deterministic, historically-buggy bits — the
⭐ score thresholds, dedup keys, the green-zone classifier, and the vote ledger:

```powershell
python -m pip install pytest      # once
python -m pytest tests\ -q
```
