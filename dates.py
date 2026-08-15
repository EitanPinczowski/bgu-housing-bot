"""
Shared Hebrew date parsing — one source of truth for month names and the DD.MM
pattern, imported by both fit.py (lease-month scoring) and pipeline.py (lease-date
normalization). They used to keep separate copies that drifted (fit's regex lacked
the hyphen pipeline added); keeping it here prevents that.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Optional


def utc_now() -> datetime:
    """Naive UTC — the clock every stored timestamp in this project is written on.

    SQLite's `CURRENT_TIMESTAMP` (which writes `first_seen`) is **UTC**, and `posted_at`
    is derived from it, so anything comparing against either must be UTC too.
    `datetime.now()` is LOCAL, which is UTC+3 here.

    THIS MISTAKE HAS NOW BEEN MADE TWICE, in two modules, which is why the helper is here
    rather than private to one of them. `storage.record_post` computed `posted_at` from
    the local clock and put publication 3 hours AHEAD of the sighting it is compared
    against, corrupting the detection-lag metric (fixed 2026-08-14). `replay._age_hours`
    then made the same comparison in the other direction and inflated every replayed
    age by 3 hours — invisible while the freshness bands were 6/18/36 hours apart and
    material the moment they became 1/3/7 DAYS, because a 21-hour-old post reads as
    24 and loses the top band."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# Hebrew month names -> number, for lease dates written as words ("בספטמבר").
HE_MONTHS = {
    "ינואר": 1, "פברואר": 2, "מרץ": 3, "אפריל": 4, "מאי": 5, "יוני": 6,
    "יולי": 7, "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12,
}

# "1.9" / "01/10" / "1-9" -> (day, month). Hyphen included (Israeli day-first).
DATE_RE = re.compile(r"\b(\d{1,2})[-./](\d{1,2})")


def month_of(lease_start: Optional[str]) -> Optional[int]:
    """Best-effort month (1–12) from a free-text lease-start string, else None."""
    if not lease_start:
        return None
    s = str(lease_start)
    m = DATE_RE.search(s)
    if m:
        mon = int(m.group(2))
        return mon if 1 <= mon <= 12 else None
    for name, num in HE_MONTHS.items():
        if name in s:
            return num
    return None


# --- the Gemini free-tier quota window --------------------------------------------
# Google's free daily buckets reset at MIDNIGHT US PACIFIC, which is 10:00 in Israel —
# not local midnight. Measured 2026-08-03: the 08:00 run was RESOURCE_EXHAUSTED while
# the 11:09 run did 233 fresh posts on Gemini, so the reset falls between them, and the
# 08:00 run is always spending the PREVIOUS day's leftovers.
#
# Anything that counts calls against the daily allowance MUST key on this window and
# not on date.today(): a calendar-day counter would zero itself at midnight, hand the
# 08:00 run a full budget it does not have, and be worse than no counter at all.
QUOTA_RESET_HOUR_LOCAL = 10


def quota_window(now=None) -> str:
    """Which daily quota window `now` (local time) falls in, as 'YYYY-MM-DD'.

    The window starting at 10:00 on the 3rd is named '2026-08-03' and runs until 10:00
    on the 4th, so 09:59 on the 4th still belongs to '2026-08-03'."""
    from datetime import datetime, timedelta
    now = now or datetime.now()
    start = now if now.hour >= QUOTA_RESET_HOUR_LOCAL else now - timedelta(days=1)
    return start.strftime("%Y-%m-%d")


def quota_window_resets_at(now=None):
    """When the current window ends (a local datetime) — for showing 'resets in Xh'."""
    from datetime import datetime, time, timedelta
    now = now or datetime.now()
    day = now.date() if now.hour < QUOTA_RESET_HOUR_LOCAL else now.date() + timedelta(days=1)
    return datetime.combine(day, time(QUOTA_RESET_HOUR_LOCAL, 0))
