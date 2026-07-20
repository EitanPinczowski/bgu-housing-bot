"""
Weekly self-health digest — sent to your PRIVATE DM (never the group).

Parses data/search_log.txt for the last N days plus the DB, so silence stays
trustworthy: it reports runs completed / skipped / Facebook-blocked, total
posts/matches/needs, a **crashed-run** flag (a START with no END), store totals,
and the locations still unmapped.

    python weekly_digest.py [days]      # default 7
"""
from __future__ import annotations
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import notifier
import storage

_LOG = config.DATA_DIR / "search_log.txt"
_END_NUMS = re.compile(r"posts=(\d+)\s+match=(\d+)\s+needs=(\d+)")
_FUNNEL_NUMS = re.compile(r"read=(\d+)\s+age_skip=(\d+)\s+seen_skip=(\d+)")   # newer runs


def _summarize(lines, now: datetime, days: int) -> dict:
    """Pure parse of search_log lines into weekly counts (testable)."""
    cutoff = now - timedelta(days=days)
    d = {"runs": 0, "skipped": 0, "blocked": 0, "dangling": 0,
         "posts": 0, "matches": 0, "needs": 0,
         "read": 0, "age_skip": 0, "seen_skip": 0}
    pending = None                        # timestamp of a START awaiting its END
    for ln in lines:
        try:
            ts = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if ts < cutoff:
            continue
        rest = ln[19:].strip()
        ev = rest.split(None, 1)[0] if rest else ""
        detail = rest[len(ev):].strip()
        if ev == "START":
            if pending is not None:       # previous START never got an END
                d["dangling"] += 1
            pending = ts
        elif ev == "END":
            pending = None
            if "BLOCKED" in detail or "block=" in detail:
                d["blocked"] += 1
            else:
                d["runs"] += 1
                if (m := _END_NUMS.search(detail)):
                    d["posts"] += int(m.group(1))
                    d["matches"] += int(m.group(2))
                    d["needs"] += int(m.group(3))
                if (fm := _FUNNEL_NUMS.search(detail)):     # optional, newer runs only
                    d["read"] += int(fm.group(1))
                    d["age_skip"] += int(fm.group(2))
                    d["seen_skip"] += int(fm.group(3))
        elif ev == "SKIP":
            d["skipped"] += 1
    # a still-open START that's too old to be an in-progress run = a crash
    if pending is not None and (now - pending) > timedelta(hours=2):
        d["dangling"] += 1
    return d


def build(days: int = 7) -> str:
    try:
        lines = open(_LOG, encoding="utf-8").read().splitlines()
    except Exception:
        lines = []
    s = _summarize(lines, datetime.now(), days)
    with sqlite3.connect(config.DB_PATH) as c:
        listings = c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        matches = c.execute("SELECT COUNT(*) FROM listings WHERE status='MATCH'").fetchone()[0]
        votes = c.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
    out = [f"📊 סיכום שבועי ({days} ימים אחרונים):",
           f"• ריצות שהושלמו: {s['runs']} · דילוגים: {s['skipped']} · חסימות פייסבוק: {s['blocked']}",
           f"• פוסטים: {s['posts']} · התאמות: {s['matches']} · חוסר-מידע: {s['needs']}"]
    if s["read"]:
        out.append(f"• נסרקו {s['read']} · דילוג ישן {s['age_skip']} · דילוג נראו {s['seen_skip']}")
    if s["dangling"]:
        out.append(f"⚠️ {s['dangling']} ריצות שלא הסתיימו (ייתכן קריסה — בדקו את הלוג)")
    out.append(f"🗃️ מאגר: {listings} מודעות ({matches} התאמות) · {votes} הצבעות")
    uk = storage.unknown_locations(days=days)
    if uk:
        out.append("🗺️ מקומות שלא מופו: " + ", ".join(f"{loc}×{cnt}" for loc, cnt, _ in uk[:8]))
    return "\n".join(out)


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    text = build(days)
    notifier.send(notifier._esc(text), target="primary")
    print(text)


if __name__ == "__main__":
    main()
