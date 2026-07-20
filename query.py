"""
Filtered search over the stored listings — ask the bot for what you want.

Used by the Telegram /search command (bot_listener.py) and importable directly.
Parses a free Hebrew/English query into filters and ranks the matches by their
vote-adjusted score. Read-only over the local SQLite `listings` table.

    from query import search
    search("2 rooms under 1500 green october", limit=10)

Supported filters (mix freely, any order):
  • rooms:      "2 rooms" / "2 חדרים"          -> at least N rooms free
  • max price:  "under 1500" / "עד 1500"       -> price per room <= N
  • zone:       "green"/"ירוק", "amber"/"צהוב" -> location tier
  • month:      "october"/"אוקטובר"/"10"       -> lease-start month
  • min rating: "4 stars" / "4 כוכבים",
                "score 80" / "ניקוד 80"        -> minimum (vote-adjusted) score
  • free text:  any other Hebrew word          -> address / summary contains it
"""
from __future__ import annotations
import re
import sqlite3

import config
import fit
import storage

_ZONES = {"green": "GREEN", "ירוק": "GREEN",
          "amber": "AMBER", "צהוב": "AMBER", "קרוב": "AMBER"}
_STAR_TO_SCORE = {5: 88, 4: 70, 3: 52, 2: 34, 1: 1}
_MONTHS_EN = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
              "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
              "november": 11, "december": 12}
# Hebrew words that are FILTERS, not address text — excluded from the free-text match.
_HE_STOP = {"חדרים", "חדר", "עד", "מתחת", "פחות", "ירוק", "צהוב", "קרוב",
            "כוכב", "כוכבים", "ניקוד", "מעל", "דירה", "דירות"}

HELP = ("🔎 חיפוש דירות. דוגמאות:\n"
        "• /search 2 חדרים עד 1500 ירוק אוקטובר\n"
        "• /search green 4 stars רגר\n"
        "מסננים: חדרים · 'עד <מחיר>' · ירוק/צהוב · חודש · 'X כוכבים' · טקסט חופשי (רחוב).")


def _parse(text: str) -> dict:
    q = (text or "").strip()
    low = q.lower()
    f: dict = {}

    if (m := re.search(r"(?:under|below|max|עד|מתחת ל?|פחות מ?)\s*(\d{3,5})", low)):
        f["max_price"] = int(m.group(1))
    if (m := re.search(r"(\d)\s*(?:rooms?|חדרים|חדר)", low)):
        f["rooms"] = int(m.group(1))
    for kw, tier in _ZONES.items():
        if kw in low:
            f["tier"] = tier
            break
    min_score = 0
    if (m := re.search(r"(\d)\s*(?:stars?|כוכב(?:ים)?)", low)):
        min_score = max(min_score, _STAR_TO_SCORE.get(int(m.group(1)), 0))
    if (m := re.search(r"(?:score|ניקוד)\s*(?:over|above|מעל|>)?\s*(\d{2,3})", low)):
        min_score = max(min_score, int(m.group(1)))
    if min_score:
        f["min_score"] = min_score

    mon = fit._lease_month(q)                       # "1.10" / "01/10" style
    if mon is None:
        for name, num in {**fit._HE_MONTHS, **_MONTHS_EN}.items():
            if name in low:
                mon = num
                break
    if mon:
        f["month"] = mon

    # free-text address terms: Hebrew words ≥2 chars that aren't filter keywords
    # or month names (those are consumed as the month filter, not an address).
    ignore = _HE_STOP | set(fit._HE_MONTHS)
    terms = [w for w in re.findall(r"[א-ת]{2,}", q) if w not in ignore]
    if terms:
        f["terms"] = terms
    return f


def search(text: str, limit: int = 10) -> list:
    """Ranked listings matching the parsed query. Each item is a dict of the row
    plus an 'eff_score' (base score + the group's net votes)."""
    f = _parse(text)
    where = ["status IN ('MATCH','NEEDS_DATA')"]
    params: list = []
    if "max_price" in f:
        where.append("price_per_room IS NOT NULL AND price_per_room <= ?")
        params.append(f["max_price"])
    if "rooms" in f:
        where.append("available_rooms >= ?")
        params.append(f["rooms"])
    if "tier" in f:
        where.append("location_tier = ?")
        params.append(f["tier"])
    if "min_score" in f:
        where.append("score >= ?")
        params.append(f["min_score"])
    for term in f.get("terms", []):
        where.append("(address LIKE ? OR summary LIKE ?)")
        params.extend([f"%{term}%", f"%{term}%"])

    sql = ("SELECT dedup_key, status, location_tier, price_per_room, available_rooms, "
           "total_roommates, address, walk_minutes, lease_start, score, source_url "
           "FROM listings WHERE " + " AND ".join(where))
    with sqlite3.connect(config.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]

    if "month" in f:
        rows = [r for r in rows if fit._lease_month(r["lease_start"]) == f["month"]]
    for r in rows:
        r["eff_score"] = storage.effective_score(r["dedup_key"], r["score"] or 0)
    rows.sort(key=lambda r: r["eff_score"], reverse=True)
    return rows[:limit]
