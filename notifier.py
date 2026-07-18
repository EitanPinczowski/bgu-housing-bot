"""Telegram alerts. Token + chat id come from the environment (.env),
never from code."""
from __future__ import annotations
import os

import requests

import config
from models import PipelineResult, Status


def _esc(text) -> str:
    """Escape MarkdownV2 reserved characters."""
    s = "" if text is None else str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, "\\" + ch)
    return s


def _esc_url(url) -> str:
    """Escape a URL for the (...) part of a MarkdownV2 inline link. Only ')' and
    '\\' need escaping there — NOT '.'/'-', so the link stays valid/clickable."""
    s = "" if url is None else str(url)
    return s.replace("\\", "\\\\").replace(")", "\\)")


def format_alert(res: PipelineResult) -> str:
    e = res.extract
    if res.status == Status.MATCH:
        header = "✅ *דירה מתאימה*" if res.preferred else "🟡 *דירה מתאימה* \\(קרוב לאזור\\)"
    else:
        header = "⚠️ *דירה — חסרים פרטים*"

    lines = [header]
    if e.summary_hebrew:
        lines.append(_esc(e.summary_hebrew))
    lines.append("")  # spacer between the summary and the details

    price = f'{e.price_per_room_ils} ש"ח לחדר' if e.price_per_room_ils is not None else "מחיר לא צוין"
    rooms = e.available_rooms_count if e.available_rooms_count is not None else "?"
    mates = e.total_roommates_in_apt if e.total_roommates_in_apt is not None else "?"

    lines.append(f"💰 {_esc(price)}")
    lines.append(f"🛏 {_esc(rooms)} חדרים פנויים · {_esc(mates)} שותפים בדירה")
    lines.append(f"📍 {_esc(e.street_address_or_neighborhood or 'לא צוין')}")

    if res.walk_minutes is not None:
        gate = f" מ{res.walk_gate}" if res.walk_gate else ""
        lines.append(f"🚶 {_esc(f'{res.walk_minutes:.0f} דק׳ הליכה' + gate)}")
    if e.lease_start_date:
        lines.append(f"📅 כניסה: {_esc(e.lease_start_date)}")
    if e.contact_phone_or_link:
        lines.append(f"📞 {_esc(e.contact_phone_or_link)}")

    # Always give a tappable link: the post permalink if we caught it, else the
    # group — so an alert is never a dead end.
    if res.source_url:
        lines.append(f"🔗 [צפייה בפוסט]({_esc_url(res.source_url)})")
    elif res.group:
        lines.append(f"🔗 [פתיחת הקבוצה]({_esc_url(res.group)})")

    if res.status == Status.NEEDS_DATA and res.reason:
        lines.append("")
        lines.append("_" + _esc(res.reason) + "_")
    return "\n".join(lines)


def send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[notifier] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping send.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        print(f"[notifier] send failed: {exc}")
        return False


def _promising_near_miss(res: PipelineResult) -> bool:
    """A NEEDS_DATA worth pinging: geocoded in/near the green zone AND with
    enough rooms free — a good place that merely didn't state a price. Anything
    ungeocodable (UNKNOWN) or short on rooms is stored but not pinged."""
    e = res.extract
    in_zone = res.location_tier in ("GREEN", "AMBER")
    enough_rooms = (e is not None and e.available_rooms_count is not None
                    and e.available_rooms_count >= config.MIN_AVAILABLE_ROOMS)
    return in_zone and enough_rooms


def notify(res: PipelineResult) -> None:
    if res.status == Status.MATCH and config.NOTIFY_ON_MATCH:
        send(format_alert(res))
    elif res.status == Status.NEEDS_DATA and config.NOTIFY_ON_NEEDS_DATA:
        if config.NEEDS_DATA_ONLY_PROMISING and not _promising_near_miss(res):
            return  # saved to SQLite by the pipeline, just not pinged
        send(format_alert(res))
