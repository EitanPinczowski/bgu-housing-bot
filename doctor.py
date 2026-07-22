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
    # (path, remediation-loader) for each artifact the classifier needs
    files = [
        (config.GREEN_ZONE_PATH, "regenerate with load_zone_from_kmz.py"),
        (config.NEIGHBORHOODS_PATH, "run: python load_neighborhoods.py"),
        (config.NO_AMBER_ZONES_PATH, "regenerate the no-amber polygons"),
        (config.ROOT / "boundary_streets.json", "run: python load_boundary_streets.py"),
        (config.ROOT / "area_features.json", "run: python load_area_features.py"),
    ]
    out = []
    for path, fix in files:
        name = f"data:{path.name}"
        if not path.exists():
            out.append((name, FAIL, "missing", fix))
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
            out.append((name, PASS, "present + parses", ""))
        except Exception as exc:
            out.append((name, FAIL, f"unparseable: {exc}", fix))
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
def _overpass_live() -> list:
    out = []
    for url in config.OVERPASS_URLS:
        host = url.split("/")[2]
        out.append((host, PASS if _http_ok(url, timeout=8, params={"data": "[out:json];out;"}) else FAIL, ""))
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
    out += [_check_db(), _check_osrm(), _check_telegram(), _check_gemini(), _check_sheets()]
    return out


# --- reporting -------------------------------------------------------------------
_ICON = {PASS: "✅", FAIL: "❌", WARN: "⚠️ ", SKIP: "· "}


def report() -> int:
    rows = checks()
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


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    code = report()
    if "--alert" in argv:
        rows = checks()
        bad = [(n, r) for n, s, _, r in rows if s == FAIL]
        if bad:
            import notifier
            msg = "🩺 בדיקת תלויות מצאה בעיה:\n" + "\n".join(f"• {n}: {r}" for n, r in bad)
            notifier.send(notifier._esc(msg), target="primary")
            print(f"[doctor] alerted: {[n for n, _ in bad]}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
