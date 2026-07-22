"""
Shared Hebrew date parsing — one source of truth for month names and the DD.MM
pattern, imported by both fit.py (lease-month scoring) and pipeline.py (lease-date
normalization). They used to keep separate copies that drifted (fit's regex lacked
the hyphen pipeline added); keeping it here prevents that.
"""
from __future__ import annotations
import re
from typing import Optional

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
