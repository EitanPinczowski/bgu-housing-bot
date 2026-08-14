"""
doctor — one command that checks EVERYTHING the bot depends on and tells you how to
fix whatever's broken. Inspired by Agent-Reach's `doctor`: probe each dependency,
report failures WITH remediation (not just "it's down").

    python doctor.py            # human-readable status table + fixes
    python doctor.py --alert    # also DM a Telegram alert on a hard failure (scheduled use)

Covers config, the data files (green zone / neighborhoods / boundary streets / …), the
SQLite DB, OSRM, Telegram, Gemini, the optional Google Sheet, AND the fallback chains
(geocode / LLM / Overpass mirrors) — showing which backend of each is actually live.
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import time
import datetime as dt
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

import config

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


def _http_ok(url, timeout=6, **kw) -> bool:
    try:
        return requests.get(url, timeout=timeout, **kw).status_code == 200
    except Exception:
        return False


# --- individual checks: each returns (name, status, detail, remediation) ---------
def _check_config():
    try:
        config.validate()
        return ("config", PASS, "thresholds / gates / viewbox / zone valid", "")
    except SystemExit as e:
        return ("config", FAIL, str(e).splitlines()[-1].strip(), "fix the value in config.py")


def _check_data_files():
    # (path, remediation-loader, required) for each artifact the classifier needs.
    # required=False -> a missing file is a WARN, not a FAIL: the feature is simply
    # off (amenities are display-only and never affect a listing's fate).
    files = [
        (config.AMENITIES_PATH, "run: python load_amenities.py", False),
        (config.GREEN_ZONE_PATH, "regenerate with load_zone_from_kmz.py"),
        (config.NEIGHBORHOODS_PATH, "run: python load_neighborhoods.py"),
        (config.NO_AMBER_ZONES_PATH, "regenerate the no-amber polygons"),
        (config.ROOT / "boundary_streets.json", "run: python load_boundary_streets.py"),
        (config.ROOT / "area_features.json", "run: python load_area_features.py"),
    ]
    out = []
    for path, fix, *rest in files:
        bad = FAIL if (rest[0] if rest else True) else WARN
        name = f"data:{path.name}"
        if not path.exists():
            out.append((name, bad, "missing", fix))
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
            out.append((name, PASS, "present + parses", ""))
        except Exception as exc:
            out.append((name, bad, f"unparseable: {exc}", fix))
    return out


def _check_db():
    if not config.DB_PATH.exists():
        return ("db", WARN, "no listings.sqlite yet", "created on first manual.py / --live run")
    try:
        with sqlite3.connect(config.DB_PATH) as c:
            n = c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        return ("db", PASS, f"{n} listings", "")
    except Exception as exc:
        return ("db", FAIL, f"unreadable: {exc}",
                "restore from data/backups/ (see backup_db.py)")


def _check_backups():
    """Is there a RECENT snapshot of the one thing that cannot be re-derived?

    The listings come back from Facebook. The group's ⭐/🗑 votes and the post archive do
    not — and the vote data `MIN_ALERT_SCORE` is waiting on is still n=3 after weeks of
    running. `_check_db` above already tells you to "restore from data/backups/", which is
    advice worth exactly as much as the newest file in there.

    It was worth less than it looked: on 2026-08-09 `backup_db.py` had never been
    scheduled despite its own docstring saying to, so all 14 snapshots were hand-made and
    the newest was already 40h old — inside this threshold by luck, not by design.

    A BACKUP JOB THAT SILENTLY STOPS IS WORSE THAN NO BACKUP, because the plan for the bad
    day still says "restore from backups". 48h so a single missed run is not an alarm but
    a stopped schedule is."""
    d = config.DATA_DIR / "backups"
    files = sorted(d.glob("listings-*.sqlite")) if d.exists() else []
    if not files:
        return ("backups", FAIL, "no DB snapshots at all",
                "run `run_backup.cmd`, and schedule it with update_schedule.cmd")
    newest = max(files, key=lambda f: f.stat().st_mtime)
    age_h = (time.time() - newest.stat().st_mtime) / 3600
    detail = f"{len(files)} kept, newest {newest.name} ({age_h:.0f}h old)"
    if age_h > 48:
        return ("backups", FAIL, detail,
                "the backup schedule has stopped — check the `BGU Backup` task "
                "(schtasks /Query /TN \"BGU Backup\")")
    return ("backups", PASS, detail, "")


def _osrm_ok() -> bool:
    try:
        r = requests.get(f"{config.OSRM_BASE_URL}/route/v1/foot/34.79,31.25;34.8015,31.2622",
                         params={"overview": "false"}, timeout=8)
        return r.json().get("code") == "Ok"
    except Exception:
        return False


def _check_osrm():
    if _osrm_ok():
        return ("osrm", PASS, f"{config.OSRM_BASE_URL} Ok", "")
    # down is a WARN, not FAIL: the bot still classifies via the straight-line estimate,
    # but walk-time SCORES are degraded (this session's exact trap).
    return ("osrm", WARN, "unreachable — walk-time scores use the straight-line estimate",
            "start Docker Desktop, then: docker start osrm_bgu  (verify localhost:5000)")


def _ollama_base() -> str:
    return os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1").rsplit("/v1", 1)[0]


def _ollama_ok() -> bool:
    try:
        return requests.get(f"{_ollama_base()}/api/tags", timeout=6).status_code == 200
    except Exception:
        return False


def _check_geocode_placement():
    """Are the pinned coordinates actually on the streets they name?

    A geocoding blunder is silent — the listing lands somewhere plausible and gets a
    tier and a walk time with nothing looking wrong. Checking the STATIC_TABLE against
    independent OSM geometry is fast and caught a street entry sitting 520 m off its
    own street, which also swallowed every house number on it."""
    try:
        import audit_geocode
        bad = audit_geocode.audit_static()
    except Exception as exc:
        return ("geocode pins", SKIP, f"can't audit ({type(exc).__name__})", "")
    if not bad:
        return ("geocode pins", PASS, "static points sit on their streets", "")
    worst = ", ".join(f"{n} ({d} m)" for d, n in bad[:3])
    return ("geocode pins", FAIL, f"{len(bad)} off their street: {worst}",
            "run: python audit_geocode.py  (then correct the coordinate in geocode.py)")


def _task_wake_flags():
    """{task name: WakeToRun} for the project's scheduled tasks, or None if we can't
    ask (not Windows / no Task Scheduler). Read-only."""
    import subprocess
    ps = ("Get-ScheduledTask | Where-Object {$_.TaskName -like 'BGU*'} | "
          "ForEach-Object { $_.TaskName + '=' + $_.Settings.WakeToRun }")
    # (also used by _check_hot_scheduled below — one PowerShell round-trip, not two)
    try:
        # errors="replace" on every subprocess here: `text=True` decodes as UTF-8, but
        # wmic/powershell/docker emit the Windows OEM codepage, so one stray byte in a
        # process command line raised UnicodeDecodeError inside subprocess's reader THREAD
        # — surfacing as an unhandled thread exception rather than a clean failure. Seen
        # intermittently in the suite 2026-08-13; it matters more now that `main.run()`
        # calls `try_fix()` at the start of every scrape.
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, errors="replace", timeout=25)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            name, _, val = line.strip().rpartition("=")
            out[name] = val.strip().lower() == "true"
    return out or None


def _modern_standby_only():
    """True when this machine has ONLY S0 Low Power Idle — no S3 to RTC-wake out of.
    None if `powercfg` can't be asked. Read-only; `/a` needs no elevation."""
    import subprocess
    try:
        r = subprocess.run(["powercfg", "/a"], capture_output=True, text=True,
                           errors="replace", timeout=20)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out, avail = r.stdout, r.stdout.split("not available")[0]
    if "S0 Low Power Idle" not in out:
        return False
    return "Standby (S3)" not in avail


def _check_wake_timers():
    """Can a scheduled run actually wake this machine?

    The silent failure mode behind "why didn't it run": with WakeToRun off, a run
    scheduled while the PC is asleep is simply skipped — Task Scheduler reports no
    error, and the only symptom is a quiet Telegram. Measured here rather than
    inferred, because it's invisible otherwise."""
    flags = _task_wake_flags()
    if flags is None:
        return ("wake timers", SKIP, "can't query Task Scheduler", "")
    asleep = [n for n, ok in flags.items() if not ok]
    if not asleep:
        # THE FLAG IS NOT THE CAPABILITY, AND THIS ROW SAID PASS FOR WEEKS WHILE SLOTS
        # WERE BEING LOST. `WakeToRun` is an RTC wake out of S3, and this machine has no
        # S3 — `powercfg /a` reports only "Standby (S0 Low Power Idle)". The checkbox is
        # set and Task Scheduler honours it; no wake ever happens. Evidence on 2026-08-14:
        # three resumes (08-11, 08-12, 08-14) all logged `Wake Source: Unknown`, i.e. a
        # human opened the lid, and the scheduler recorded the 08:00 and 10:00 slots
        # itself as `NumberOfMissedRuns: 2`.
        if _modern_standby_only() is True:
            return ("wake timers", WARN,
                    f"all {len(flags)} tasks ASK to wake the PC, but this machine is "
                    "Modern Standby only (no S3) — a scheduled wake cannot fire",
                    "leave it on mains and awake for the hours you want covered; "
                    "check with `powercfg /a` and the Power-Troubleshooter wake source")
        return ("wake timers", PASS, f"all {len(flags)} tasks can wake the PC", "")
    return ("wake timers", WARN,
            f"{len(asleep)}/{len(flags)} task(s) can't wake the PC: " + ", ".join(asleep[:3]),
            "run setup_always_on.cmd as Administrator (missed runs otherwise)")


def _check_keep_awake():
    """Will a run that starts now be able to hold the machine awake?

    `scraper.start_keep_awake()` is MAINS-ONLY by the user's rule, so on battery a run
    gets no protection and this machine — Modern Standby, no S3 — idles into standby with
    the run inside it. What that looks like is NOT a crash: on 2026-08-14 the 11:03 run
    froze at `post 6` three minutes in, the wall clock ran on, and the watchdog aborted at
    212 min against a 120-minute ceiling because the watchdog was throttled too. The
    process held the lock the whole time, so the 12:00 and 14:00 slots were refused with
    Task Scheduler event 322 (`instance already running`). One unplugged laptop cost the
    entire day.

    `doctor` was all-green the night before, because nothing asked this question."""
    try:
        import scraper
        ac = scraper.on_ac_power()
    except Exception:
        return ("keep-awake", SKIP, "can't read the power state", "")
    if ac is True:
        return ("keep-awake", PASS, "on mains — a run can hold the machine awake", "")
    where = "on battery" if ac is False else "power state unknown (treated as battery)"
    return ("keep-awake", WARN,
            f"{where} — keep-awake will NOT hold, and a run may freeze mid-scrape",
            "plug it in for the hours you want covered; a frozen run also holds the "
            "lock, so it costs the following slots too")


def _check_hot_scheduled():
    """Is the fast `--hot` pass actually wired to the scheduler?

    It was built, documented, and counted in the volume budget — and never scheduled,
    for days. Nothing failed; detection was just quietly 8.4 h slow. A feature that
    silently isn't running is worth a row of its own."""
    flags = _task_wake_flags()
    if flags is None:
        return ("hot pass", SKIP, "can't query Task Scheduler", "")
    if any("hot" in name.lower() for name in flags):
        return ("hot pass", PASS, "scheduled", "")
    return ("hot pass", WARN, "built but never scheduled — detection stays slow",
            "run update_schedule.cmd as Administrator")


def _check_last_run():
    """Are scheduled scrapes actually HAPPENING? The watchdog checked dependencies but
    never this — so a sleeping PC or a disabled Task Scheduler job was silent."""
    log = config.DATA_DIR / "search_log.txt"
    try:
        ends = [ln for ln in log.read_text(encoding="utf-8").splitlines() if "  END  " in ln]
    except Exception:
        return ("last run", WARN, "no search log yet", "run: python main.py --live")
    if not ends:
        return ("last run", WARN, "no completed run logged", "run: python main.py --live")
    try:
        last = datetime.strptime(ends[-1][:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return ("last run", WARN, "could not parse the log", "")
    hours = (datetime.now() - last).total_seconds() / 3600
    # A RUN IN FLIGHT IS NOT A MISSED RUN. This row measures time since the last
    # COMPLETION, so a long run makes it climb while the scraper is working perfectly —
    # observed 2026-08-04 16:14: this said "none for 5.6h" while `scraper progress` in
    # the same report said "last progress 1 min ago". Same blind spot `run_in_progress`
    # was written for one check earlier; both rows now consult it.
    import scraper
    if scraper.run_in_progress():
        return ("last run", PASS, f"a run is in progress (last finished {hours:.1f}h ago)", "")
    # only complain during active hours — overnight silence is by design (daytime only)
    quiet_ok = not (8 <= datetime.now().hour <= 20)
    limit = getattr(config, "MAX_HOURS_BETWEEN_RUNS", 5)
    if hours > limit and not quiet_ok:
        return ("last run", FAIL, f"none for {hours:.1f}h (limit {limit}h)",
                "PC asleep or the Task Scheduler job is off/failing — check "
                "'BGU Housing Scraper' in Task Scheduler and the power/sleep settings")
    return ("last run", PASS, f"{hours:.1f}h ago", "")


def _check_wedged_scraper():
    """Is a scrape running but STUCK? Judged by progress (the heartbeat), never by how
    long it has been running — a local-LLM fallback run legitimately takes hours, and
    killing those would throw away real work.

    A stale heartbeat only means something while a run is LIVE. The heartbeat file
    survives the run that wrote it, so between scheduled runs its age just keeps
    growing: on 2026-08-03 this row FAILed with "no progress for 31 min" at 13:30 while
    the 08:00 run had finished cleanly at 13:11 and `last run` PASSed 0.5h ago — two
    rows contradicting each other about the same healthy machine."""
    import scraper
    age = scraper.heartbeat_age()
    if age is None:
        return ("scraper progress", SKIP, "no run has reported progress yet", "")
    mins = age / 60
    if mins <= config.STALL_MINUTES:
        return ("scraper progress", PASS, f"last progress {mins:.0f} min ago", "")
    running = scraper.run_in_progress()
    if running is False:
        return ("scraper progress", PASS, "idle (no run in progress)", "")
    if running is None:
        # couldn't ask the OS: report the doubt, don't spend the alarm on it
        return ("scraper progress", WARN,
                f"no progress for {mins:.0f} min; couldn't tell if a run is live",
                "check for a running main.py; if one is stuck, kill the pid in "
                "data/scraper.heartbeat")
    return ("scraper progress", FAIL,
            f"no progress for {mins:.0f} min (limit {config.STALL_MINUTES})",
            "a run is wedged — the next run clears it automatically, or kill the pid "
            "in data/scraper.heartbeat")


def _listener_running() -> bool:
    """True if a bot_listener.py process is alive (Windows: ask the task list)."""
    import subprocess
    try:
        out = subprocess.run(["wmic", "process", "get", "commandline"],
                             capture_output=True, text=True, errors="replace", timeout=20).stdout
        if out:
            return "bot_listener" in out
    except Exception:
        pass
    try:      # PowerShell fallback (wmic is absent on newer Windows)
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"],
            capture_output=True, text=True, errors="replace", timeout=25).stdout
        return "bot_listener" in (out or "")
    except Exception:
        return True                  # can't tell -> don't cry wolf


def _check_listener():
    if _listener_running():
        return ("listener", PASS, "bot_listener is running", "")
    return ("listener", FAIL, "bot_listener is NOT running — votes and /commands are dead",
            "start it: run_listener.cmd (or pythonw bot_listener.py). NOTE: it does not "
            "reload code — restart it after any change")


def _process_started(needle: str):
    """When did the process whose command line contains `needle` start? None if absent.

    Python imports a module once, so a long-lived server keeps serving whatever the code
    said at startup — see `_check_dashboard_fresh`."""
    import subprocess
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
             f"'{needle}' " + "} | ForEach-Object { $_.CreationDate.ToString('o') }"],
            capture_output=True, text=True, errors="replace", timeout=25).stdout
        for line in (out or "").splitlines():
            line = line.strip()
            if line:
                return dt.datetime.fromisoformat(line).timestamp()
    except Exception:
        pass
    return None


def _check_dashboard_fresh():
    """Is the live dashboard serving the CODE WE HAVE, or the code it started with?

    Found the hard way: `serve_dashboard.py` had been up 22 hours, so the page reachable
    from the phone was still running the previous evening's `dashboard.py`, the previous
    evening's `geocode.py`, and `geocode._anchors` loaded before `govmap_anchors.json`
    existed. A whole day of geocoding work was invisible there and nothing said so — the
    process was healthy, which is exactly why it needed its own check. A stale server
    should be as visible as a dead one."""
    started = _process_started("serve_dashboard")
    if started is None:
        return ("dashboard", WARN, "serve_dashboard is not running — no live/phone page",
                "start it: run_dashboard.cmd")
    newest, newest_name = 0.0, ""
    for name in ("dashboard.py", "geocode.py", "govmap_anchors.json", "house_anchors.json",
                 "user_anchors.json", "user_pins.json"):
        p = config.ROOT / name
        if p.exists() and p.stat().st_mtime > newest:
            newest, newest_name = p.stat().st_mtime, name
    age_h = (time.time() - started) / 3600
    if newest > started:
        behind = (newest - started) / 3600
        return ("dashboard", FAIL,
                f"running code from {age_h:.1f} h ago — {newest_name} changed "
                f"{behind:.1f} h after it started",
                "restart it: the module is imported once, so edits and new anchors are "
                "NOT picked up until the process restarts")
    return ("dashboard", PASS, f"serving current code (up {age_h:.1f} h)", "")


def _check_telegram():
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return ("telegram", FAIL, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set",
                "add both to .env (see README)")
    if _http_ok(f"https://api.telegram.org/bot{tok}/getMe"):
        return ("telegram", PASS, "bot token valid, chat id set", "")
    return ("telegram", FAIL, "getMe failed — bad token or no network",
            "check TELEGRAM_BOT_TOKEN in .env")


def _check_gemini():
    if os.environ.get("GEMINI_API_KEY"):
        return ("gemini", PASS, "GEMINI_API_KEY set (not test-called — would burn quota)", "")
    if config.LLM_PROVIDER == "gemini":
        return ("gemini", FAIL, "GEMINI_API_KEY not set but LLM_PROVIDER=gemini",
                "add GEMINI_API_KEY to .env, or set a local LLM_PROVIDER")
    return ("gemini", SKIP, "not the configured provider", "")


def _check_llm_budget():
    """How much of this window's Gemini allowance is gone, and when it comes back.

    Worth a row of its own because the failure is invisible until it bites: a run that
    has no quota does not stop, it silently drops to the local model at ~63 s/post and
    holds the scraper lock (2026-08-03: 5h12m, and the day's next two runs skipped).
    The window is 10:00 Israel to 10:00, NOT the calendar day — see dates.quota_window.
    """
    import llm
    cap = getattr(config, "LLM_DAILY_BUDGET", 0)
    if not cap:
        return ("llm budget", SKIP, "no client-side ceiling (LLM_DAILY_BUDGET=0)", "")
    window, used = llm.budget_state()          # the ACTIVE rung — what the gate uses
    import dates
    resets = dates.quota_window_resets_at()
    hrs = max(0.0, (resets - datetime.now()).total_seconds() / 3600)
    # SHOW EVERY RUNG, not just the active one. The budget gate is per model because the
    # quota is, but a row reading "0/480" on a day that made 429 calls is a lie of
    # omission — the reader wants the window's total and where it went.
    ladder = getattr(config, "GEMINI_MODELS", None) or [config.GEMINI_MODEL]
    per = [(m, llm.budget_state(m)[1]) for m in ladder]
    spread = " · ".join(f"{m.replace('gemini-', '')} {n}" for m, n in per)
    msg = (f"{used}/{cap} on {llm.active_model().replace('gemini-', '')} "
           f"[{spread}] · window {window} · resets in {hrs:.1f}h")
    # THE MEASUREMENT THAT REPLACES THE GUESS. 900 was picked out of the air; the real
    # ceiling is wherever the provider first says no, and that used to be visible only in
    # the stdout of an unattended run. `llm.record_quota_refusal` now stores it.
    refused = llm.quota_refusal()
    if refused is not None:
        kind = llm.quota_refusal_kind()
        stated = llm.stated_quota_limit()
        if kind == "day":
            # Google NAMES its limit in the refusal; prefer that over our own count,
            # which includes retries and OCR calls and so runs slightly ahead.
            if stated and cap > stated:
                return ("llm budget", FAIL,
                        f"{msg} · Google states a limit of {stated}/day",
                        f"LLM_DAILY_BUDGET ({cap}) is ABOVE Google's real ceiling "
                        f"({stated}), so it can never bind — set it just under {stated}")
            return ("llm budget", WARN, f"{msg} · provider refused (PER-DAY) at {refused}"
                    + (f", stated limit {stated}" if stated else ""),
                    f"measured: set LLM_DAILY_BUDGET just under "
                    f"{stated or refused} so we stop before Google does "
                    f"(it is currently {cap})")
        if kind == "minute":
            return ("llm budget", PASS, f"{msg} · one PER-MINUTE refusal at {refused}",
                    "a rate-limit blip, not the daily ceiling — do NOT lower "
                    "LLM_DAILY_BUDGET on it; consider raising GEMINI_MIN_INTERVAL_SEC")
        return ("llm budget", WARN, f"{msg} · provider refused at {refused} (kind unknown)",
                "the error text was not captured for this one, and a per-minute 429 looks "
                "identical to a daily one — do NOT retune the budget until a refusal "
                "records its metric (…PerDay vs …PerMinute)")
    if used >= cap:
        return ("llm budget", FAIL, msg,
                "the client-side ceiling is spent — runs will use the local model "
                "until the window resets; that is expected, not a fault to fix now")
    if used >= 0.85 * cap:
        return ("llm budget", WARN, msg, "")
    return ("llm budget", PASS, msg, "")


def _check_sheets():
    from sheets import _cred_path
    sid, cred = os.environ.get("GOOGLE_SHEET_ID"), _cred_path()
    if not sid and not os.path.exists(cred):
        return ("sheets", SKIP, "optional sink not configured", "")
    if sid and os.path.exists(cred):
        return ("sheets", PASS, "sheet id + service-account creds present", "")
    return ("sheets", WARN, "partially configured",
            "need BOTH GOOGLE_SHEET_ID (.env) and the service-account JSON in auth/")


# --- fallback chains: name -> ordered [(backend, status, detail)] ----------------
# A TINY BOUNDED QUERY, NOT A BARE `out;`. This probe used to send `[out:json];out;`,
# which has no filter — it asks the server to serialise the ENTIRE dataset. Measured
# 2026-08-10 against overpass-api.de, which was demonstrably healthy at the time:
#     [out:json];out;                          -> ReadTimeout after 8.3s
#     node(31.2620,34.7990,…);out 1;           -> 200 in 815ms
# So the row reported every mirror down whenever any mirror was up, and `geocode`'s
# `overpass` link inherited it. A CHECK THAT CAN ONLY FAIL IS AS USELESS AS ONE THAT CAN
# ONLY PASS, and worse than nothing here: it trains you to ignore the row.
#
# The bbox is a few metres of Be'er Sheva, so it exercises the real query path — parse,
# index lookup, serialise — for about a kilobyte of work.
_OVERPASS_PROBE = ("[out:json][timeout:10];"
                   "node(31.2620,34.7990,31.2625,34.7995);out 1;")


def _overpass_ok(url: str) -> bool:
    """Probe one mirror the SAME WAY `geocode.py` queries it.

    The old probe was a GET with no User-Agent. `geocode._overpass_*` does a POST with
    `config.NOMINATIM_USER_AGENT`, and the difference is not cosmetic: measured
    2026-08-10 against overpass-api.de, the default `python-requests` agent is refused
    outright with **406 Not Acceptable in 307ms**. So the row could never pass, on any
    mirror, however healthy — a health check that does not send what the code sends is
    testing a request nobody makes."""
    try:
        r = requests.post(url, data={"data": _OVERPASS_PROBE},
                          headers={"User-Agent": config.NOMINATIM_USER_AGENT},
                          timeout=getattr(config, "OVERPASS_TIMEOUT_SEC", 8))
        return r.status_code == 200
    except Exception:
        return False


def _overpass_live() -> list:
    out = []
    for url in config.OVERPASS_URLS:
        host = url.split("/")[2]
        out.append((host, PASS if _overpass_ok(url) else FAIL, ""))
    return out


def chains() -> list:
    """(chain_name, [(backend, status, detail), …]) — surfaces the already-existing
    routing so you can see which link of each fallback chain is live."""
    google_on = bool(getattr(config, "USE_GOOGLE_GEOCODE", False) and os.environ.get("GOOGLE_MAPS_API_KEY"))
    overpass = _overpass_live()
    geocode_chain = [
        ("static-table", PASS, "always on"),
        ("cache", PASS, "data/geocode_cache.json"),
        ("google", PASS if google_on else SKIP, "opt-in, needs billing key"),
        ("overpass", PASS if any(s == PASS for _, s, _ in overpass) else FAIL,
         f"{sum(s==PASS for _,s,_ in overpass)}/{len(overpass)} mirrors up"),
        ("nominatim", PASS if config.USE_NOMINATIM_FALLBACK else SKIP, "last resort"),
    ]
    llm_chain = [
        ("gemini", PASS if os.environ.get("GEMINI_API_KEY") else FAIL, "primary, free tier"),
        ("ollama", PASS if _ollama_ok() else (FAIL if config.LLM_FALLBACK_PROVIDER else SKIP),
         "local fallback on quota"),
    ]
    return [("geocode", geocode_chain), ("llm", llm_chain), ("overpass mirrors", overpass)]


def checks() -> list:
    out = [_check_config()]
    out += _check_data_files()
    out += [_check_db(), _check_backups(), _check_osrm(), _check_telegram(), _check_gemini(),
            _check_llm_budget(), _check_sheets(),
            _check_last_run(), _check_listener(), _check_wedged_scraper(),
            _check_wake_timers(), _check_keep_awake(),
            _check_hot_scheduled(), _check_geocode_placement(),
            _check_dashboard_fresh()]
    return out


# --- reporting -------------------------------------------------------------------
_ICON = {PASS: "✅", FAIL: "❌", WARN: "⚠️ ", SKIP: "· "}


def report_json(rows=None) -> int:
    """The same verdict as `report()`, machine-readable.

    Exists so nothing else has to RE-IMPLEMENT these probes. `.claude/hooks/session_start.py`
    grew its own copies of the OSRM and scrape checks, which is how a project ends up with
    two views of its own health that can disagree — and the one nobody runs is always the
    one that is right.

    Takes `rows` so a caller that already has them does not pay for a second round of live
    HTTP probes.
    """
    rows = checks() if rows is None else rows
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "checks": [{"name": n, "status": s, "detail": d, "remediation": r}
                   for n, s, d, r in rows],
        "chains": {c: [{"backend": b, "status": s} for b, s, _ in backends]
                   for c, backends in chains()},
    }
    hard = [n for n, s, *_ in rows if s == FAIL]
    payload["failures"] = hard
    payload["ok"] = not hard
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 1 if hard else 0


def report(rows=None) -> int:
    rows = checks() if rows is None else rows
    width = max(len(n) for n, *_ in rows)
    print("=== dependencies ===")
    for name, status, detail, _ in rows:
        print(f"  {_ICON.get(status,'')} {name:<{width}}  {status:<4}  {detail}")
    print("\n=== fallback chains (first live backend wins) ===")
    for cname, backends in chains():
        parts = " ▸ ".join(f"{b}[{_ICON.get(s,'').strip()}]" for b, s, _ in backends)
        print(f"  {cname:<16} {parts}")
    fixes = [(n, r) for n, s, _, r in rows if s in (FAIL, WARN) and r]
    if fixes:
        print("\n=== fix ===")
        for name, rem in fixes:
            print(f"  {name}: {rem}")
    hard = [n for n, s, *_ in rows if s == FAIL]
    print(f"\n{'❌ ' + str(len(hard)) + ' hard failure(s): ' + ', '.join(hard) if hard else '✅ all good'}")
    return 1 if hard else 0


def _daemon_ok(timeout: int = 12) -> bool:
    """Is the Docker ENGINE answering? Distinct from "is the container running"."""
    import subprocess
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, errors="replace", timeout=timeout)
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception:
        return False                # includes the CLI HANGING, which is a known symptom


def _daemon_ok_retry(tries: int = 20, gap: int = 6) -> bool:
    """Docker Desktop takes a while: it boots a WSL VM before the engine answers."""
    import time
    for _ in range(tries):
        if _daemon_ok():
            return True
        time.sleep(gap)
    return False


def _start_docker_desktop() -> tuple:
    """Launch Docker Desktop itself. Returns (ok, detail).

    Starting the APP is a different repair from starting the container, and it is the one
    that was missing: `run_scraper.cmd` has always run `docker start osrm_bgu` before a
    scrape, and 14 of 88 completed runs STILL logged 'OSRM DOWN'. `docker start` cannot
    do anything when the engine itself is not up, which is what those runs hit."""
    import subprocess
    for exe in (os.path.expandvars(r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Docker\Docker Desktop.exe")):
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception as exc:
                return False, f"{type(exc).__name__}: {exc}"
            return (True, "launched, waiting for the engine") if _daemon_ok_retry() \
                else (False, "launched but the engine did not come up in ~2 min")
    return False, "Docker Desktop.exe not found in Program Files or LocalAppData"


def try_fix() -> list:
    """Attempt to auto-heal what we safely can. Returns [(what, ok, detail)].

    ONLY EVER TOUCHES THE ONE KNOWN CONTAINER, and only ever starts things. Nothing here
    may prune, remove, or reset: `.claude/hooks/guard.py` blocks those from a shell
    precisely because `osrm_bgu` is a multi-GB rebuild from the Israel PBF, and this
    function must not become the way around that guard.

    Two layers, because they fail differently:
      1. the Docker ENGINE is down    -> start Docker Desktop and wait for it
      2. the CONTAINER is stopped     -> docker start
    Layer 2 alone was already wired into run_scraper.cmd and was not enough."""
    done = []
    if _osrm_ok():
        return done

    import subprocess
    if not _daemon_ok():
        ok, detail = _start_docker_desktop()
        done.append(("start Docker Desktop", ok, detail))
        if not ok:
            return done            # no point asking a dead engine to start a container

    container = getattr(config, "OSRM_DOCKER_CONTAINER", "osrm_bgu")
    try:
        r = subprocess.run(["docker", "start", container],
                           capture_output=True, text=True, errors="replace", timeout=60)
        ok = r.returncode == 0 and _osrm_ok_retry()
        done.append(("start OSRM", ok,
                     f"{container} up" if ok else
                     f"docker start {container}: "
                     + ((r.stderr or r.stdout or "").strip()[:80] or "no output")))
    except Exception as exc:
        done.append(("start OSRM", False, f"{type(exc).__name__}: {exc}"))
    return done


def _osrm_ok_retry(tries: int = 6) -> bool:
    import time
    for _ in range(tries):
        if _osrm_ok():
            return True
        time.sleep(3)              # OSRM needs a few seconds after the container starts
    return False


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--fix" in argv:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for what, ok, detail in try_fix():
            print(f"{stamp}  [doctor --fix] {_ICON[PASS if ok else FAIL]} {what}: {detail}")
    # One pass of checks(), shared by every renderer below: each check makes live network
    # probes, so calling checks() per consumer doubles the wall time and the traffic.
    rows = checks()
    if "--quiet" in argv:
        # For scheduled callers (run_scraper.cmd) that want the REPAIR logged and not the
        # whole table: at 7 runs a day the report would bury the log it is written to.
        code = 1 if any(s == FAIL for _, s, *_ in rows) else 0
    else:
        code = report_json(rows) if "--json" in argv else report(rows)
    if "--alert" in argv:
        bad = [(n, r) for n, s, _, r in rows if s == FAIL]
        if bad:
            import notifier
            msg = "🩺 בדיקת תלויות מצאה בעיה:\n" + "\n".join(f"• {n}: {r}" for n, r in bad)
            notifier.send(notifier._esc(msg), target="primary")
            print(f"[doctor] alerted: {[n for n, _ in bad]}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
