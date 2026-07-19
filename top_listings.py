"""
Post the top-N recent MATCH listings as FULL alerts (photo album + details +
⭐/🗑 vote buttons), ranked by the vote-adjusted (effective) score. Used for the
morning highlights (top 3) and the evening digest (top 5).

    python top_listings.py <N> [hours]      # e.g.  3 24  /  5 13

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
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    rows = _top(n, hours)
    if not rows:
        notifier.send(notifier._esc(f"אין דירות מובילות ב-{hours} השעות האחרונות."))
        print("no top listings")
        return
    notifier.send(notifier._esc(f"🏆 {len(rows)} הדירות המובילות ({hours} שעות אחרונות):"))
    for r in rows:
        notifier._send_alert(_to_result(r))
    print(f"posted top {len(rows)} over {hours}h")


if __name__ == "__main__":
    main()
