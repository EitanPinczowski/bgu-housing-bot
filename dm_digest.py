"""
Daily DM-only digest — sent to your PRIVATE chat, never the group.

Right now it reports the locations the bot extracted from posts but couldn't map
(so they went NEEDS_DATA/UNKNOWN and were likely silenced). Pinning the frequent
ones to geocode.STATIC_TABLE closes that whole area's gap — this is exactly how
"הבלוק" was being missed.

    python dm_digest.py [days]      # default 1 (the day)
"""
from __future__ import annotations
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import notifier
import storage


def build(days: int = 1) -> str | None:
    rows = storage.unknown_locations(days)
    if not rows:
        return None
    head = f"🗺️ מקומות שלא הצלחתי למפות ({days} ימים אחרונים) — שווה להוסיף לטבלת הגאוקוד:"
    lines = [notifier._esc(head), ""]
    for loc, cnt, _ in rows[:25]:
        lines.append(notifier._esc(f"• {loc} ×{cnt}"))
    return "\n".join(lines)


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    text = build(days)
    if not text:
        print("no unknown locations to report")
        return
    notifier.send(text, target="primary")     # your DM only, never the group
    print("sent DM digest")


if __name__ == "__main__":
    main()
