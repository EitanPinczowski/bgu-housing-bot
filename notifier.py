"""Telegram alerts. Token + chat id come from the environment (.env),
never from code."""
from __future__ import annotations
import os
from urllib.parse import quote

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

    # Map link: prefer a SEARCH of the actual address text — Google geocodes it
    # far better than our static table / Nominatim, which only gave an approximate
    # (often wrong) pin. Fall back to our coords only if there's no address text.
    map_url = None
    if e.street_address_or_neighborhood:
        map_url = ("https://www.google.com/maps/search/?api=1&query="
                   + quote(f"{e.street_address_or_neighborhood}, באר שבע"))
    elif res.lat is not None and res.lon is not None:
        map_url = f"https://www.google.com/maps?q={res.lat},{res.lon}"
    if map_url:
        lines.append(f"🗺️ [מפה]({_esc_url(map_url)})")
    if e.lease_start_date:
        lines.append(f"📅 כניסה: {_esc(e.lease_start_date)}")
    if e.contact_phone_or_link:
        lines.append(f"📞 {_esc(e.contact_phone_or_link)}")

    # Always give a tappable link: the post permalink if we caught it, else the
    # group — so an alert is never a dead end.
    if res.source_url:
        lines.append(f"🔗 [צפייה בפוסט]({_esc_url(res.source_url)})")
    elif res.group:
        # FB lazy-loads the post permalink for comment-less posts (anti-scraping),
        # so we couldn't capture it — open the group newest-first instead, where a
        # last-24h post sits near the top.
        gurl = res.group + ("&" if "?" in res.group else "?") + "sorting_setting=CHRONOLOGICAL"
        lines.append(f"🔗 [פתיחת הקבוצה \\(הפוסט קרוב לראש\\)]({_esc_url(gurl)})")

    if res.status == Status.NEEDS_DATA and res.reason:
        lines.append("")
        lines.append("_" + _esc(res.reason) + "_")
    return "\n".join(lines)


def _creds():
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def send(text: str) -> bool:
    token, chat_id = _creds()
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


def send_photo(photo_url: str, caption: str) -> bool:
    """Send the alert as a photo with the details as caption (max 1024 chars —
    our alerts are well under). Returns False if it fails, so the caller can
    fall back to a plain text message (FB image URLs can expire / be blocked)."""
    token, chat_id = _creds()
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            json={"chat_id": chat_id, "photo": photo_url, "caption": caption,
                  "parse_mode": "MarkdownV2"},
            timeout=20,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        print(f"[notifier] send_photo failed ({exc}); falling back to text")
        return False


def _send_alert(res: PipelineResult) -> None:
    """Send one listing alert — as a photo if we have one, else as text; a photo
    that fails to send falls back to text so the alert still gets through."""
    text = format_alert(res)
    if res.image_url and send_photo(res.image_url, text):
        return
    send(text)


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
        _send_alert(res)
    elif res.status == Status.NEEDS_DATA and config.NOTIFY_ON_NEEDS_DATA:
        if config.NEEDS_DATA_ONLY_PROMISING and not _promising_near_miss(res):
            return  # saved to SQLite by the pipeline, just not pinged
        _send_alert(res)
