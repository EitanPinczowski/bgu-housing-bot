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
  • furnished:     a small bonus if the flat is furnished (bonus only)
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


_HE_FLOORS = {"קרקע": 0, "כניסה": 0, "ראשונה": 1, "שניה": 2, "שנייה": 2,
              "שלישית": 3, "רביעית": 4, "חמישית": 5, "שישית": 6, "שביעית": 7,
              "שמינית": 8, "תשיעית": 9, "עשירית": 10}
_FLOOR_DIGIT = re.compile(r"\d+")


def _floor_num(floor) -> Optional[int]:
    """A numeric floor from the free-text field: the first digit run (so "3 מתוך 5"
    → 3), else a Hebrew ordinal ("קרקע"→0 … "עשירית"→10), else None."""
    if not floor:
        return None
    s = str(floor)
    if (m := _FLOOR_DIGIT.search(s)):
        return int(m.group())
    for word, n in _HE_FLOORS.items():
        if word in s:
            return n
    return None


def breakdown(price: Optional[int], walk_min: Optional[float], tier: Optional[str],
              avail_rooms: Optional[int] = None, total_mates: Optional[int] = None,
              price_uncertain: bool = False, age_hours: Optional[float] = None,
              lease_start: Optional[str] = None, furnished: Optional[bool] = None,
              floor: Optional[str] = None, has_elevator: Optional[bool] = None,
              has_balcony: Optional[bool] = None) -> list:
    """The score's per-factor contributions as [(hebrew_label, delta), …], in the
    order they're applied. `score()` is just the clamped sum of the deltas — this is
    the single source of truth, so the alert's "why this score" line can never drift
    from the number."""
    parts: list = []

    # zone
    parts.append(("אזור ירוק" if tier == "GREEN" else "אזור צהוב" if tier == "AMBER"
                  else "מחוץ לאזור", 25 if tier == "GREEN" else 10 if tier == "AMBER" else 0))

    # walk time — forgiving bands (anything ≤10 min counts as "close")
    if walk_min is not None:
        parts.append((f"הליכה {walk_min:.0f} דק׳",
                      25 if walk_min <= 10 else 20 if walk_min <= 14
                      else 13 if walk_min <= 18 else 7 if walk_min <= 20 else 0))
    else:
        parts.append(("הליכה לא ידועה", 13))

    # price — forgiving absolute bands (₪ per room; >2000 is a hard-drop before scoring)
    if price is None:
        parts.append(("מחיר לא צוין", 10))
    elif price <= 1200:
        parts.append(("מחיר מצוין", 25))
    elif price <= 1500:
        parts.append(("מחיר בתקציב", 22))
    elif price <= 1600:
        parts.append(("מחיר סביר", 20))
    elif price <= 1700:
        parts.append(("מחיר מעט מעל", 18))
    elif price <= 2000:
        parts.append(("מחיר גבוה", 15))
    else:
        parts.append(("מחיר גבוה מאוד", 0))

    # available rooms — the whole apartment free is best
    if avail_rooms and total_mates:
        parts.append(("חדרים פנויים", round(15 * min(1.0, avail_rooms / total_mates))))
    elif avail_rooms:
        parts.append(("חדרים פנויים", 10 if avail_rooms >= 3 else 6))

    # total roommates — 2 best, then 3, then 4
    if total_mates is not None:
        parts.append((f"{total_mates} שותפים",
                      15 if total_mates <= 2 else 10 if total_mates == 3
                      else 5 if total_mates == 4 else 0))
    else:
        parts.append(("שותפים לא ידוע", 5))

    # freshness — centered so it rewards a brand-new post and penalizes a stale
    # repost, without just inflating every score. Unknown age (manual paste) = 0.
    if age_hours is not None:
        parts.append(("טריות",
                      4 if age_hours < 6 else 2 if age_hours < 18
                      else 0 if age_hours < 36 else -4))

    # entry date vs your target move-in month — the SMALLEST factor by design, so
    # it only nudges ties. Same month +4, an adjacent month +2, else 0.
    if config.TARGET_MOVE_IN_MONTH:
        m = _lease_month(lease_start)
        if m is not None:
            diff = min((m - config.TARGET_MOVE_IN_MONTH) % 12,
                       (config.TARGET_MOVE_IN_MONTH - m) % 12)
            parts.append(("תאריך כניסה", 4 if diff == 0 else 2 if diff == 1 else 0))

    # furnished — a one-way bonus (config.FURNISHED_BONUS); unfurnished isn't penalized
    if furnished:
        parts.append(("מרוהט", config.FURNISHED_BONUS))

    # balcony / garden — a major, near-top-tier feature
    if has_balcony:
        parts.append(("מרפסת/גינה", config.BALCONY_BONUS))

    # high floor with NO elevator (unmentioned counts as none): penalty grows
    # exponentially with the floor. No penalty for floor ≤ 1, unknown floor, or a
    # confirmed elevator.
    fnum = _floor_num(floor)
    if fnum is not None and fnum > 1 and has_elevator is not True:
        pen = -round(min(config.FLOOR_PENALTY_CAP,
                         config.FLOOR_PENALTY_BASE ** (fnum - 1)))
        parts.append((f"קומה {fnum} ללא מעלית", pen))

    # penalize uncertainty
    if price is None:
        parts.append(("אי-ודאות מחיר", -6))
    if price_uncertain:
        parts.append(("מחיר מהתגובות", -3))

    return parts


def _max_possible() -> int:
    """Sum of every factor's best-case positive contribution — the denominator that
    rescales the raw sum onto 0–100. Without this, the base factors alone already
    overflow 100 and clamp, hiding balcony/furnished at the top."""
    m = 25 + 25 + 25 + 15 + 15 + 4     # zone, walk, price, rooms, roommates, freshness
    if config.TARGET_MOVE_IN_MONTH:
        m += 4                          # entry-date nudge
    return m + config.FURNISHED_BONUS + config.BALCONY_BONUS


def score(price: Optional[int], walk_min: Optional[float], tier: Optional[str],
          avail_rooms: Optional[int] = None, total_mates: Optional[int] = None,
          price_uncertain: bool = False, age_hours: Optional[float] = None,
          lease_start: Optional[str] = None, furnished: Optional[bool] = None,
          floor: Optional[str] = None, has_elevator: Optional[bool] = None,
          has_balcony: Optional[bool] = None) -> int:
    raw = sum(delta for _, delta in breakdown(
        price, walk_min, tier, avail_rooms, total_mates, price_uncertain,
        age_hours, lease_start, furnished, floor, has_elevator, has_balcony))
    # Rescale onto 0–100 so the top isn't compressed by the clamp — features like
    # balcony/furnished now spread the best listings into distinct scores.
    return max(0, min(100, round(100 * raw / _max_possible())))


def top_factors(parts: list, n: int = 3) -> list:
    """The n most POSITIVE contributions (for a compact 'why this score' line)."""
    return sorted((p for p in parts if p[1] > 0), key=lambda p: p[1], reverse=True)[:n]


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
