#!/usr/bin/env python
"""SessionStart: report the bot's LIVE state, instead of trusting a typed-out summary.

CLAUDE.md opens with "## OPEN RIGHT NOW — read this first", hand-dated. On 2026-08-09
that block was dated 08-06 while commits had landed on 08-07 and 08-08 — so the section
a session is told to read first was three days behind the code. That is not a discipline
failure; a block that must be retyped to stay true will always drift.

So: the file keeps the open DECISIONS (which change slowly and need judgment), and this
prints the STATE (which changes hourly and can simply be measured).

Everything here is read-only and individually wrapped — a probe that fails reports
itself as one line and the rest of the banner still prints. This runs on every session
start, so it must never be the reason a session cannot begin.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))


def _safe(label, fn):
    try:
        v = fn()
        return f"  {label}: {v}" if v is not None else None
    except Exception as e:
        return f"  {label}: (could not check — {type(e).__name__})"


def _scrape():
    import scraper
    r = scraper.run_in_progress()
    if r is None:
        return "UNKNOWN (couldn't ask the OS)"
    return "RUNNING — do not write the DB" if r else "idle"


def _osrm():
    import osrm
    return "up" if osrm.alive() else "DOWN — walk times fall back to straight-line"


def _listings():
    import config
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        m = con.execute("SELECT COUNT(*) FROM listings WHERE status='MATCH'").fetchone()[0]
        return f"{n} ({m} MATCH)"
    finally:
        con.close()


def _quota():
    import config
    import dates
    import llm
    window = dates.quota_window()
    parts = []
    for model in getattr(config, "GEMINI_MODELS", []):
        try:
            _, used = llm.budget_state(model)
        except Exception:
            used = "?"
        parts.append(f"{model.replace('gemini-', '')}={used}")
    resets = dates.quota_window_resets_at()
    left = resets - datetime.now()
    hrs = max(0, int(left.total_seconds() // 3600))
    return f"window {window}, {', '.join(parts)} of {config.LLM_DAILY_BUDGET} (resets in ~{hrs}h)"


def _last_run():
    import config
    log = os.path.join(config.DATA_DIR, "scraper_runs.log")
    if not os.path.exists(log):
        return None
    with open(log, encoding="utf-8", errors="replace") as f:
        lines = [ln for ln in f if "END" in ln or "START" in ln]
    return lines[-1].strip()[:90] if lines else None


def _notes_drift():
    """Is the hand-written status block older than the newest commit?

    This is the exact drift that motivated the hook, so it keeps watching for it."""
    import re
    root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    md = os.path.join(root, "CLAUDE.md")
    if not os.path.exists(md):
        return None
    with open(md, encoding="utf-8") as f:
        head = f.read(4000)
    m = re.search(r"OPEN RIGHT NOW.*?\((\d{4}-\d{2}-\d{2})\)", head)
    if not m:
        return None
    noted = m.group(1)
    r = subprocess.run(["git", "log", "-1", "--format=%ad", "--date=short"],
                       capture_output=True, text=True, cwd=root, timeout=10)
    newest = (r.stdout or "").strip()
    if newest and newest > noted:
        return f"CLAUDE.md's status block says {noted}, newest commit is {newest} — stale"
    return f"current ({noted})"


def _write_cache(values: dict) -> None:
    """Leave the probe results where the statusline can read them.

    The statusline renders constantly, so it must never probe: `osrm.alive()` alone is a
    3s timeout when the server is down, and `run_in_progress` was 750ms before psutil.
    A statusline that shells out on every render is worse than no statusline. It reads
    this file and shows how old it is, so a stale line is visibly stale rather than
    quietly wrong."""
    import json
    try:
        import config
        path = config.DATA_DIR / ".claude_state.json"
        values["generated"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(values), encoding="utf-8")
    except Exception:
        pass                       # a banner must never fail because a cache write did


def main() -> int:
    probes = {"scrape": _scrape, "osrm": _osrm, "listings": _listings,
              "gemini": _quota, "last run": _last_run, "notes": _notes_drift}
    rows, values = [], {}
    for label, fn in probes.items():
        try:
            v = fn()
            values[label] = v
            if v is not None:
                rows.append(f"  {label}: {v}")
        except Exception as e:
            rows.append(f"  {label}: (could not check — {type(e).__name__})")
    _write_cache(values)
    if rows:
        print("bgu_housing_bot — live state at session start:")
        print("\n".join(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
