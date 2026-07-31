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
tiers: **inside → preferred** (✅ MATCH); **outside but within a 20-minute walk of
a campus gate → acceptable, not preferred** (🟡 MATCH nearby); **farther than that
→ dropped**. Real listings use the OSRM walk time for that boundary (a calibrated
straight-line estimate when OSRM is down). `no_amber_zones.json` areas (e.g.
שכונה ד') get no walk-grace — outside the green polygon there is red. The named
blacklist (Ramot, Neve Zeev, …) is a separate instant-drop applied before any of this.

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

- **Green zone — done.** A hand-drawn polygon in `green_zone.json` (originally
  from a My Maps KMZ; since expanded east toward the campus except the Soroka
  side — `green_zone.backup.json` keeps the original). To redraw it: export a new
  KMZ from My Maps and run `python load_zone_from_kmz.py path\to\NewLayer.kmz`.
  `no_amber_zones.json` holds the שכונה ד' polygon (no walk-grace there).
- **Red areas → still needed.** Add every avoid-neighborhood (and common
  misspellings) to `BLACKLIST_NEIGHBORHOODS` in `config.py`. These are dropped
  before geocoding. Currently: Ramot, Neve Zeev, Nahal Ashan, Pelach 7.
- **Optional — geocoding hints.** Street addresses are geocoded automatically
  via Nominatim (Be'er Sheva). For slang/neighborhood-only posts, add the name →
  a point inside that area to `STATIC_TABLE` in `geocode.py` for more reliable
  placement (a few seeds are already there).

- **Optional — amenity & transit proximity.** `python load_amenities.py` builds
  `amenities.json`, and every alert then carries a line like
  `🚌 669 מרגר · 6 דק׳ (כל ~20 דק׳) ↔ 8 דק׳` — walking minutes to the bus and
  places listed in `config.AMENITY_TARGETS` (line 669 on רגר in both directions,
  a bus heading to רכבת באר שבע מרכז with its frequency, and the gym at
  קניון עזריאלי הנגב). **Display only** — it never changes the fit score.
  - Frequency comes from Israel's Ministry of Transport **GTFS** feed (official
    open data, free, no key). It's ~100 MB zipped, cached in `data/`, streamed
    straight out of the zip; `--skip-download` reuses it and `--poi-only`
    refreshes just the Overpass places.
  - Re-run it every few months (timetables change). Everything degrades quietly:
    no `amenities.json`, or a stopped OSRM, and alerts simply omit the line.
  - Note OSM lags rebrands — the Azrieli mall is still tagged `קניון הנגב`, which
    is why each place carries a `match_names` list tried in order.

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
into once; long randomized delays between scrolls and groups; checkpoint-abort on
a login/verification wall; and **dry-run by default — it writes nothing and sends
no alerts unless you pass `--live`.** Coverage is set by `SCRAPER_SCAN_ALL_GROUPS`
(currently every group each run) and `SCRAPER_MIN_POSTS_PER_GROUP`. Each group
**early-stops** once it turns up no more *fresh* posts — fresh = within
`SCRAPER_MAX_POST_AGE_HOURS` (24h) **and** not already processed in an earlier run —
so later runs in a day are shallow (mostly already-seen). ⚠️ Coverage still drives
how much you scrape; higher = more Facebook-detection risk on your only account.

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

### Schedule it 7×/day, 08:00–20:00 (Windows Task Scheduler)

**Already set up.** A scheduled task named **`BGU Housing Scraper`** runs the
scraper **7 times a day at 08:00 / 10:00 / 12:00 / 14:00 / 16:00 / 18:00 / 20:00**
(every 2 h), each with **up to 25 min of random delay** so the runs don't fire on
the exact minute (clockwork timing is the main thing that looks automated to
Facebook). With `SCRAPER_SCAN_ALL_GROUPS=True` (current), each run reads **every
group** in a random order, scrolling each until it has
**`SCRAPER_MIN_POSTS_PER_GROUP` (20)** fresh posts, hits the hard cap
`SCRAPER_SCROLL_CAP`, or **early-stops** because the group turned up no more fresh
(recent + not-already-seen) posts. That early-stop is what keeps 7×/day affordable:
later runs mostly re-see posts and bail per group after a few passes. ⚠️ Still a
heavy per-run scrape for one personal account — raise the cadence only on an
explicit, informed decision. (Set `SCRAPER_SCAN_ALL_GROUPS=False` to
fall back to the most-overdue ⅓–½ subset with the `SCRAPER_MIN_SCRAPES_PER_DAY`
coverage guarantee.) It calls `run_scraper.cmd`,
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

### The dashboard — offline file, or live on your phone

```bash
python dashboard.py
```
writes a self-contained `data/dashboard.html` that works offline forever (no CDN, no
tile server). Filtering the table now moves the **map dots** with it, and every row
expands to show the per-factor score breakdown and the **original Hebrew post**.

```bash
run_dashboard.cmd
```
serves the same page live from SQLite, which is the only way to get three things a
static file can't do: pick up new listings without a rebuild, vote (⭐/🗑/📵) and write
notes, and be opened from a phone. It prints its URLs on startup.

**A token is required on every request** — the page lists landlords' phone numbers and
home addresses. It comes from `DASHBOARD_TOKEN` in `.env`, or is generated once into
`data/dashboard_token.txt` so a phone bookmark keeps working. There is deliberately no
unauthenticated mode.

**To reach it away from home, install [Tailscale](https://tailscale.com/) on the PC and
the phone** and use the `100.x` URL the server prints. That is a private network between
your own devices — unlike ngrok or a router port-forward, it puts no public URL on the
internet holding other people's contact details. For same-Wi-Fi access you may need to
allow the port once:

```bash
netsh advfirewall firewall add rule name="BGU dashboard" dir=in action=allow protocol=TCP localport=8777
```

Other things it does: photos (cached on first view, because Facebook image URLs expire),
free-text search that also covers the **original post text** — so `ללא תיווך` or `מזגן`
are findable even though the bot never extracts them — "new since your last visit"
badges, side-by-side compare with an OSRM-planned viewing route, and `j`/`k`/`s`/`x`
keyboard triage. On a phone the table becomes cards.

**The map is the page.** On a desktop it zooms with Ctrl/⌘+wheel (a plain wheel scrolls
the page on purpose) and pans by dragging; on a phone **one finger pans and two pinch**,
and you scroll the page from outside the map. Hovering a dot opens a small card beside
it; tapping does the same on a phone. Street names appear as they become legible.
**Shift+drag** (or the `▭ אזור` button) rubber-bands an area and filters to it. The
legend panel — collapsed, top corner — explains every symbol and switches layers off:
streets, neighborhood outlines, transit pins, and **walk-time rings** at 5/10/15/20 min
around each gate, which is the `MAX_WALK_MINUTES` rule behind every AMBER made visible.

**How much to trust a dot.** Only 45% of listings resolve to a house number; 41% land
on a street or neighbourhood centroid because the post never gave one. Those draw
**hollow** instead of solid, the card says which it is, and `מיקום משוער בלבד` filters
to just them. This is also why "the clusters don't open up": 282 mapped listings sit on
**105 distinct coordinates**, 19 of them on a single point, and no zoom level can
separate identical coordinates. A badge over a genuine spread zooms; a badge over a
*stack* **fans out** on leader lines so you can reach each flat.

**Fixing a wrong location.** 📍 on a card arms place-mode; the next tap on the map puts
the flat where it really is, and asks whether that applies to this listing only or to
**every listing at that address** (the right answer for the two flats whose address is
literally `אוניברסיטת בן גוריון`, and for a bare `שכונה ד`). The correction goes through
`pipeline._classify`, so it survives `replay --apply`, and the card reports what it did
to the tier, walk and score — moving a dot changes the verdict, and that shouldn't be
silent. `↩` undoes it.

**Transport.** Opening a card pins *that listing's* 669 stops (both directions), its
train-bound stop and the gym, each on a hairline back to the flat and each with its own
🚶 that draws the real OSRM path. The map-wide layer only shows fixed landmarks: "a stop
with a bus to the train station" matches 428 of the city's stops, so there is no useful
map-wide version of it. 🚶 without a destination routes to the nearest campus gate. All
of it is display-only and never enters the fit score.

### Sharing it with the people flat-hunting with you

Two routes, and the difference matters:

```bash
python dashboard.py --share          # data/dashboard-YYYY-MM-DD.html
python dashboard.py --share --send   # …and post it to the Telegram group
```

**A file** (~800 KB, one self-contained page) that anyone can open on a phone straight
from the chat — no account, nothing to install. It is a *snapshot*: the write buttons
(⭐/🗑/📵, notes, route planning) are **removed rather than disabled**, and a dated
banner says when it was taken, so a three-day-old copy is never mistaken for live data.
Contacts and WhatsApp links stay, because your partners need to call the landlord too —
which is exactly why it goes to the group only, never anywhere public.
`update_schedule.cmd` registers **BGU Dashboard Share** to post it daily at 21:00, after
the last scrape; `run_dashboard_share.cmd` is the same thing by hand.

**Live**, if they need to vote and see new listings as they land: `tailscale share`, or
invite their own Tailscale account to this one machine, and give them the token URL.
That needs their account and this PC awake, which is why the daily file is the default
rather than the only route.

### A public URL that works when this PC is off

Tailscale and every tunnel still need the machine awake. For a link that doesn't:

```bash
python dashboard.py --share --publish
```

pushes the same snapshot to **GitHub Pages**. It is already set up and live at

**https://eitanpinczowski.github.io/bgu-housing-dashboard/**

from the dedicated repo `EitanPinczowski/bgu-housing-dashboard` (Pages: `main` / root),
with `SITE_REPO_URL` in `.env`. `BGU Dashboard Publish` refreshes it **hourly at :30
from 08:30 to 20:30**, and `BGU Dashboard Share` does it once more at 21:00 with the
Telegram post — 14 refreshes a day against 10 scrapes, so the URL is never more than
about an hour behind. Publishing costs ~2 s and touches nothing outside this machine
(no Facebook), which is why it can be this frequent. Without `SITE_REPO_URL` the
publish step prints one line and does nothing.

To point it at a different repo, change `SITE_REPO_URL` and delete `data/site` — the
next publish re-clones.

Three things to know:

- **It is a snapshot, never live.** The scraper needs this PC and OSRM runs in local
  Docker, so the URL is only as fresh as the last push. The dated banner on the page
  says so — a URL looks permanently current in a way a dated file doesn't.
- **It is public and unauthenticated.** Anyone with the link reads every listing,
  contact and address, and search engines will index it. `PUBLISH_NOINDEX=1` in `.env`
  keeps it out of search results; it does not restrict access.
- **It must be its own repo.** Publishing into this code repo would write those phone
  numbers into public git history permanently, where deleting the file wouldn't remove
  them. `publish.py` refuses to do it.

### Scrape timing — run `update_schedule.cmd` once, as Administrator

Two problems, both measured on 2026-07-30 and both fixed by this script:

- **`main.py --hot` had never been scheduled.** It exists to cut detection lag to
  ~30–40 min and `CLAUDE.md` even counted it in the volume budget — but Task
  Scheduler had one scraper task running `run_scraper.cmd` with no arguments.
  Measured consequence: **median time-to-detect 8.4 hours (n=44)**, with only 7 of
  44 listings seen within an hour of being posted.
- **The schedule ignored the market.** Of 63 timed posts, **45 land 14:00–20:00**,
  yet runs were spaced evenly 08/10/12/14/16/18/20 — the busiest hours got the same
  two-hour lag as the empty ones, and 11:00–13:00 is nearly dead.

After: **6 full runs** (08/10/14/16/18/20) **+ 4 hot runs** (12/15/17/19), so
between 14:00 and 20:00 something runs *every hour*. **Total volume falls**:
7 × 251 = 1757 → 6 × 251 + 4 × 30 = **1626 page-reads/day (−7.5%)**, because one
expensive full run in an empty window pays for four cheap passes across the peak.
Nothing else changes — same groups, same delays and jitter, daytime only, read-only.

`python stats.py` now prints **time to detect** and **runs/day** (each with its `n`),
so the effect is measured rather than assumed, and `doctor` gains a **`hot pass`** row
so a feature that isn't running can't hide again.

### Keeping it always-on (run `setup_always_on.cmd` once, as Administrator)

Scheduled runs are only as reliable as the machine being awake. Every `BGU *` task
was created with **"Wake the computer to run this task" OFF**, so a run scheduled
while the PC is asleep is *silently skipped* — Task Scheduler reports success and
the only symptom is a quiet Telegram. That is the real answer to "why didn't the
last run run?".

Measured on this machine: `WakeToRun=False` on all six tasks, wake timers disabled
on battery, and a 3-minute battery sleep timeout that would cut a run short.

**`setup_always_on.cmd`** (right-click → Run as administrator) fixes all three:
sets `WakeToRun` on every BGU task, enables wake timers on battery as well as
mains, and raises the battery sleep timeout to 30 minutes. It prints an UNDO block
and changes nothing about the scraper's volume or any safety rule. It's a script
you run rather than something the bot does for you, because these are Windows
power settings, not project settings.

`python doctor.py` now reports a **`wake timers`** row, so this can't go back to
being invisible.

**Why there is no Docker/VPS setup here.** Splitting the non-Facebook services
(listener, digests) into containers or onto a cheap VPS sounds tidy but is a trap
with this design: the Facebook scraper *must* stay on your machine (real logged-in
profile, home IP), and everything shares one SQLite file. SQLite locking over a
Windows→Linux bind mount is unreliable, and a VPS split needs a real DB-sync story
first. Until that exists, the honest setup is: everything local, the machine awake
when it needs to be, and `run_listener.cmd` supervising the listener.

### Helper tasks (also scheduled)

- **`BGU Watchdog`** (`watchdog.py`) — runs 07:30/11:30/15:30/19:30, 30 min before
  each scrape. A thin wrapper around `doctor` (below) that pings Telegram if a
  dependency is down, so you can fix it before a run degrades. (Facebook-login loss
  is caught by the scraper's own "0 posts" alert.)

Run **`python doctor.py`** anytime for a full, human-readable health check: it
probes config, the data files (green zone / neighborhoods / boundary streets / …),
the SQLite DB, OSRM, Telegram, Gemini, and the optional Google Sheet, prints a
PASS/FAIL/WARN table **with the fix for anything broken** (e.g. OSRM down →
`docker start osrm_bgu`), and shows which backend of each fallback chain
(geocode / LLM / Overpass mirrors) is currently live. `python doctor.py --alert`
adds the Telegram DM (what `watchdog.py` runs).
- **`BGU Morning`** (`top_listings.py 3 24`) — every day at 08:00, posts the
  **top 3** listings of the last 24 h to Telegram as **full listings** (photo
  album + details + ⭐/🗑 vote buttons), ranked by the **vote-adjusted** score.
- **`BGU Digest`** (`top_listings.py 5 13`) — every evening at 20:00, posts the
  **top 5 of the day** (last 13 h) the same way. Run either by hand, e.g.
  `python top_listings.py 5 24` (top 5 over the last 24 h). The old text recap is
  still there as `python digest.py 3` (last 3 days) if you want a plain list.
- **`BGU DM Digest`** (`dm_digest.py 1`) — every evening at 20:05, sends **to your
  private DM only** the day's **unmapped locations** — names the bot extracted but
  couldn't geocode (e.g. a new area nickname), most frequent first, so you can pin
  the common ones to `geocode.STATIC_TABLE` and stop missing that area.

**Where things go:** listings (scraper alerts + the morning/evening top-N) go to
the **group**; operational pings and the DM digest go to **your private DM**.
Routing is by chat-id sign (groups are negative, DMs positive). Each alert shows
its numeric fit score next to the stars, e.g. `⭐⭐⭐⭐ (73)`.

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

It also answers two **DM-only** text commands (ignored in the group, and only from
your own DM):

- **`/search <query>`** — filtered search over your stored listings, ranked by the
  vote-adjusted score. Mix filters freely, Hebrew or English:
  `/search 2 חדרים עד 1500 ירוק אוקטובר` or `/search green 4 stars רגר`. Supports
  rooms, `עד <price>`/`under <price>`, `ירוק`/`green` & `צהוב`/`amber`, a move-in
  month, `X כוכבים`/`X stars` or `ניקוד <n>`, and free-text street. (See `query.py`.)
- **`/status`** — last-24h run funnel (from `search_log.txt`), DB totals, and a live
  OSRM health check, replied to your DM on demand.
- **`/top [N]`** — the best N listings right now · **`/saved`** — the flats you ⭐-saved.
- **`/classify <post text>`** — paste any listing (from anywhere) and get the bot's
  verdict/tier/score — `manual.py` from your phone.
- **`/unknowns`** — places that failed to geocode, each with a 📌 one-tap pin of the
  Overpass suggestion · **`/pin <name> <lat,lon>`** / **`/uncache <name>`** — add/remove a
  geocode pin by hand (`user_pins.json`).
- **`/stats`** — DB funnel & drop reasons · **`/doctor`** — the dependency health check ·
  **`/sheet`** — link to the Google Sheet · **`/help`** — this list.

Each alert also carries **ℹ️ למה** (the fit-score breakdown) and **📵 שוחחתי** (mark a flat
contacted so it stops showing in `/top`) alongside the ⭐/🗑 vote buttons.

### Facebook safety extras

- **Checkpoint abort.** If a run lands on a Facebook checkpoint / login /
  "confirm it's you" wall, the scraper stops the whole run immediately (never
  retries into it) and sends a distinct ⛔ Telegram alert telling you to re-login
  via `login.py`. This is the one condition to act on before the next run.
- **Occasional skipped run.** ~1 in 8 live runs is skipped on purpose
  (`SCRAPER_SKIP_RUN_PROBABILITY`) so the cadence isn't clockwork; the skip is
  logged as a `SKIP` line in `data/search_log.txt` and sends no Telegram.

### Introspection: stats + replay (no browser)

Every post that reaches the LLM is archived (raw text + parsed fields + verdict)
in the `posts` table, so you can see what the filters do and re-test changes
against your whole history without re-scraping Facebook.

```powershell
python stats.py       # funnel: how many posts became MATCH/NEEDS_DATA/DROP/NOT_AD,
                      # why they were dropped, store totals, top unmapped locations

python replay.py      # re-run classify+score over EVERY archived post with the
                      # current code+config, and list what changed. Reuses the
                      # stored LLM parse -> fast, no browser, no Gemini quota.
python replay.py --llm       # also re-run the LLM (for prompt/llm.py edits)
python replay.py --changed   # only the posts whose verdict/score flipped
```

`replay.py` is the tuning workflow: after editing the green zone, `MAX_WALK_MINUTES`,
`fit.py`, a threshold, etc., run it to see exactly which past listings flip —
read-only, it never writes or sends anything.

### Tests

Fast, offline unit tests cover the deterministic, historically-buggy bits — the
⭐ score thresholds, dedup keys, the green-zone classifier, and the vote ledger:

```powershell
python -m pip install pytest      # once
python -m pytest tests\ -q
```
