"""
A 0–100 "fit" score for a listing → ⭐1–5, so the best options surface from the
flood. Used on MATCH alerts and to sort the digest best-first.

Factors (higher = better):
  • zone:          inside the green zone > near it
  • walk time:     shorter is better
  • price:         vs your budget (config.TARGET_PRICE_PER_ROOM_ILS)
  • available rooms: the whole apartment free is best (avail / total)
  • total roommates: 2 is best, then 3, then 4, then more
  • freshness:      a just-posted listing beats a day-old repost
  • uncertainty:   unknown price, or a price taken from a comment, is penalized
Star thresholds are deliberately strict so 5⭐ means genuinely excellent.
"""
from __future__ import annotations
import re
from typing import Optional

import config

# Hebrew month names -> number, for lease dates written as words ("בספטמבר").
_HE_MONTHS = {
    "ינואר": 1, "פברואר": 2, "מרץ": 3, "אפריל": 4, "מאי": 5, "יוני": 6,
    "יולי": 7, "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12,
}
_DM = re.compile(r"\b(\d{1,2})[./](\d{1,2})\b")   # "1.9", "01/10" -> (day, month)


def _lease_month(lease_start: Optional[str]) -> Optional[int]:
    """Best-effort month (1–12) from a free-text lease-start string, else None."""
    if not lease_start:
        return None
    s = str(lease_start)
    m = _DM.search(s)
    if m:
        mon = int(m.group(2))
        return mon if 1 <= mon <= 12 else None
    for name, num in _HE_MONTHS.items():
        if name in s:
            return num
    return None


def score(price: Optional[int], walk_min: Optional[float], tier: Optional[str],
          avail_rooms: Optional[int] = None, total_mates: Optional[int] = None,
          price_uncertain: bool = False, age_hours: Optional[float] = None,
          lease_start: Optional[str] = None) -> int:
    s = 0

    # zone
    s += 25 if tier == "GREEN" else 10 if tier == "AMBER" else 0

    # walk time
    if walk_min is not None:
        s += (25 if walk_min < 8 else 18 if walk_min < 12
              else 10 if walk_min < 16 else 4 if walk_min < 20 else 0)
    else:
        s += 8

    # price vs your budget
    t = config.TARGET_PRICE_PER_ROOM_ILS
    if price is None:
        s += 6
    elif price <= t * 0.8:
        s += 25
    elif price <= t:
        s += 18
    elif price <= t * 1.2:
        s += 8
    else:
        s += 2

    # available rooms — the whole apartment free is best
    if avail_rooms and total_mates:
        s += round(15 * min(1.0, avail_rooms / total_mates))
    elif avail_rooms:
        s += 10 if avail_rooms >= 3 else 6

    # total roommates — 2 best, then 3, then 4
    if total_mates is not None:
        s += 15 if total_mates <= 2 else 10 if total_mates == 3 else 5 if total_mates == 4 else 0
    else:
        s += 5

    # freshness — centered so it rewards a brand-new post and penalizes a stale
    # repost, without just inflating every score. Unknown age (manual paste) = 0.
    if age_hours is not None:
        s += 4 if age_hours < 6 else 2 if age_hours < 18 else 0 if age_hours < 36 else -4

    # entry date vs your target move-in month — the SMALLEST factor by design, so
    # it only nudges ties. Same month +4, an adjacent month +2, else 0.
    if config.TARGET_MOVE_IN_MONTH:
        m = _lease_month(lease_start)
        if m is not None:
            diff = min((m - config.TARGET_MOVE_IN_MONTH) % 12,
                       (config.TARGET_MOVE_IN_MONTH - m) % 12)
            s += 4 if diff == 0 else 2 if diff == 1 else 0

    # penalize uncertainty
    if price is None:
        s -= 6
    if price_uncertain:
        s -= 8

    return max(0, min(100, s))


def stars(points: int) -> str:
    if points >= 88:
        return "⭐⭐⭐⭐⭐"
    if points >= 70:
        return "⭐⭐⭐⭐"
    if points >= 52:
        return "⭐⭐⭐"
    if points >= 34:
        return "⭐⭐"
    return "⭐"
