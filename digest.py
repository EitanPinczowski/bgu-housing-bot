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
import fit
import notifier

_MAX_ROWS = 25   # keep the message under Telegram's length limit


def _recent(days: int):
    # space separator (not isoformat's 'T') to match how SQLite stores first_seen
    # — otherwise same-day rows sort wrong (' ' < 'T') and get excluded.
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(config.DB_PATH) as c:
        rows = c.execute(
            """SELECT status, price_per_room, available_rooms, address, walk_minutes,
                      summary, source_url, "group", location_tier
               FROM listings
               WHERE first_seen >= ? AND status IN ('MATCH', 'NEEDS_DATA')""",
            (since,),
        ).fetchall()
    # best-first: MATCHes above near-misses, each sorted by fit score
    return sorted(rows, key=lambda r: ((r[0] == "MATCH"), fit.score(r[1], r[4], r[8])),
                  reverse=True)


_MSG_LIMIT = 3800   # stay under Telegram's 4096-char message cap


def build(rows, days: int) -> list[str]:
    """One or more messages (chunked to stay under Telegram's size limit)."""
    esc = notifier._esc
    if not rows:
        return [esc(f"📋 סיכום {days} ימים אחרונים: לא נמצאו דירות מתאימות.")]

    def block(r) -> str:
        status, price, rooms, addr, walk, summary, url, group, tier = r
        icon = "✅" if status == "MATCH" else "⚠️"
        stars = fit.stars(fit.score(price, walk, tier))
        price_s = f'{price} ש"ח לחדר' if price else "מחיר לא צוין"
        ls = [f"{icon} {stars} {esc(summary or addr or '?')}",
              f"   💰 {esc(price_s)} · 🛏 {esc(rooms if rooms is not None else '?')}"
              f" · 📍 {esc(addr or '?')}"
              + (f" · 🚶 {esc(round(walk))} דק׳" if walk is not None else "")]
        link = url or ((group + "?sorting_setting=CHRONOLOGICAL") if group else None)
        if link:
            ls.append(f"   🔗 [{'צפייה בפוסט' if url else 'פתיחת הקבוצה'}]({notifier._esc_url(link)})")
        return "\n".join(ls)

    messages, chunk = [], f"📋 *סיכום {days} ימים — {len(rows)} דירות*"
    for r in rows[:_MAX_ROWS]:
        b = block(r)
        if len(chunk) + len(b) + 2 > _MSG_LIMIT:
            messages.append(chunk)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + b
    if chunk.strip():
        messages.append(chunk)
    if len(rows) > _MAX_ROWS:
        messages.append(esc(f"…ועוד {len(rows) - _MAX_ROWS}. פתחו את הגיליון לכל הרשימה."))
    return messages


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    rows = _recent(days)
    msgs = build(rows, days)
    ok = all(notifier.send(m) for m in msgs)
    print(f"digest: {len(rows)} listings over {days} days, {len(msgs)} message(s) — sent={ok}")


if __name__ == "__main__":
    main()
