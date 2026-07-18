"""
Telegram digest of recent listings from the local SQLite DB — a periodic
"here's what turned up" summary, separate from the per-post alerts.

    python digest.py [days]      # default 7

Schedule it (e.g. every evening) via Task Scheduler for a regular recap, or run
it by hand any time.
"""
from __future__ import annotations
import sqlite3
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

import config
import notifier

_MAX_ROWS = 25   # keep the message under Telegram's length limit


def _recent(days: int):
    since = (datetime.now() - timedelta(days=days)).isoformat()
    with sqlite3.connect(config.DB_PATH) as c:
        return c.execute(
            """SELECT status, price_per_room, available_rooms, address, walk_minutes,
                      summary, source_url, "group"
               FROM listings
               WHERE first_seen >= ? AND status IN ('MATCH', 'NEEDS_DATA')
               ORDER BY (status='MATCH') DESC, first_seen DESC""",
            (since,),
        ).fetchall()


def build(rows, days: int) -> str:
    esc = notifier._esc
    if not rows:
        return esc(f"📋 סיכום {days} ימים אחרונים: לא נמצאו דירות מתאימות.")
    header = f"📋 *סיכום {days} ימים — {len(rows)} דירות*"
    lines = [header, ""]
    for status, price, rooms, addr, walk, summary, url, group in rows[:_MAX_ROWS]:
        icon = "✅" if status == "MATCH" else "⚠️"
        price_s = f'{price} ש"ח לחדר' if price else "מחיר לא צוין"
        lines.append(f"{icon} {esc(summary or addr or '?')}")
        detail = f"   💰 {esc(price_s)} · 🛏 {esc(rooms if rooms is not None else '?')} · 📍 {esc(addr or '?')}"
        if walk is not None:
            detail += f" · 🚶 {esc(round(walk))} דק׳"
        lines.append(detail)
        link = url or ((group + "?sorting_setting=CHRONOLOGICAL") if group else None)
        if link:
            label = "צפייה בפוסט" if url else "פתיחת הקבוצה"
            lines.append(f"   🔗 [{label}]({notifier._esc_url(link)})")
        lines.append("")
    if len(rows) > _MAX_ROWS:
        lines.append(esc(f"…ועוד {len(rows) - _MAX_ROWS}. פתחו את הגיליון/DB לכל הרשימה."))
    return "\n".join(lines)


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    rows = _recent(days)
    ok = notifier.send(build(rows, days))
    print(f"digest: {len(rows)} listings over {days} days — sent={ok}")


if __name__ == "__main__":
    main()
