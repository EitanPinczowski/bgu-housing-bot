"""
Post the top-N recent MATCH listings as FULL alerts (photo album + details +
⭐/🗑 vote buttons), ranked by the vote-adjusted (effective) score. Used for the
morning highlights (top 3) and the evening digest (top 5).

    python top_listings.py <N> [hours] [--test]

    e.g.  top_listings.py 3 24            # morning: top 3 of last 24h, to everyone
          top_listings.py 5 13            # evening: top 5 of the day
          top_listings.py 5 24 --test     # dry-run to your OWN DM only (no group)

Note: photo albums depend on Facebook image URLs, which expire after a while, so
older tops may fall back to text (still with all the details + buttons).
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import config
import notifier
import storage
from models import ListingExtract, PipelineResult, Status

_SQL = """SELECT dedup_key, status, location_tier, price_per_room, available_rooms,
                 total_roommates, address, walk_minutes, lease_start, contact,
                 summary, source_url, "group", price_from_comment, score, images
          FROM listings WHERE first_seen >= ? AND status = 'MATCH'"""
_COLS = ("dedup_key", "status", "location_tier", "price_per_room", "available_rooms",
         "total_roommates", "address", "walk_minutes", "lease_start", "contact",
         "summary", "source_url", "group", "price_from_comment", "score", "images")


def _top(n: int, hours: int):
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(config.DB_PATH) as c:
        rows = c.execute(_SQL, (since,)).fetchall()
    rows.sort(key=lambda r: storage.effective_score(r[0], r[14] or 0), reverse=True)
    return rows[:n]


def _to_result(r) -> PipelineResult:
    d = dict(zip(_COLS, r))
    e = ListingExtract(
        is_apartment_ad=True, price_per_room_ils=d["price_per_room"],
        available_rooms_count=d["available_rooms"], total_roommates_in_apt=d["total_roommates"],
        street_address_or_neighborhood=d["address"], lease_start_date=d["lease_start"],
        contact_phone_or_link=d["contact"], price_from_comment=bool(d["price_from_comment"]),
        summary_hebrew=d["summary"])
    try:
        imgs = json.loads(d["images"]) if d["images"] else []
    except Exception:
        imgs = []
    return PipelineResult(
        status=Status.MATCH, preferred=(d["location_tier"] == "GREEN"),
        location_tier=d["location_tier"], walk_minutes=d["walk_minutes"],
        dedup_key=d["dedup_key"], source_url=d["source_url"], group=d["group"],
        images=imgs, score=storage.effective_score(d["dedup_key"], d["score"] or 0),
        extract=e)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    test = "--test" in sys.argv           # send to your own DM only, not the group
    n = int(args[0]) if len(args) > 0 else 5
    hours = int(args[1]) if len(args) > 1 else 24
    rows = _top(n, hours)
    tag = " [בדיקה]" if test else ""
    if not rows:
        notifier.send(notifier._esc(f"אין דירות מובילות ב-{hours} השעות האחרונות.{tag}"),
                      primary_only=test)
        print("no top listings")
        return
    notifier.send(notifier._esc(f"🏆 {len(rows)} הדירות המובילות ({hours} שעות אחרונות):{tag}"),
                  primary_only=test)
    for r in rows:
        notifier._send_alert(_to_result(r), primary_only=test)
    print(f"posted top {len(rows)} over {hours}h{' (test/DM only)' if test else ''}")


if __name__ == "__main__":
    main()
