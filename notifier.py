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
        # FB lazy-loads the permalink for comment-less posts (anti-scraping), so
        # we couldn't capture it. Best consolation: SEARCH the group for the
        # post's own text, which usually lands right on it. Fall back to opening
        # the group newest-first if we have no text to search.
        q = ""
        if e:
            q = (e.summary_hebrew or e.street_address_or_neighborhood or "").strip()[:60]
        if q:
            gurl = res.group.rstrip("/") + "/search/?q=" + quote(q)
            label = "חיפוש הפוסט בקבוצה"
        else:
            gurl = res.group + ("&" if "?" in res.group else "?") + "sorting_setting=CHRONOLOGICAL"
            label = "פתיחת הקבוצה \\(הפוסט קרוב לראש\\)"
        lines.append(f"🔗 [{label}]({_esc_url(gurl)})")

    if res.status == Status.NEEDS_DATA and res.reason:
        lines.append("")
        lines.append("_" + _esc(res.reason) + "_")
    return "\n".join(lines)


def _creds():
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def _keyboard(dedup_key):
    """The ⭐/🗑 triage buttons for a listing (handled by bot_listener.py)."""
    if not dedup_key:
        return None
    return {"inline_keyboard": [[
        {"text": "⭐ מעניין", "callback_data": f"save|{dedup_key}"},
        {"text": "🗑 הסר", "callback_data": f"dismiss|{dedup_key}"},
    ]]}


def send(text: str, reply_markup=None) -> bool:
    token, chat_id = _creds()
    if not token or not chat_id:
        print("[notifier] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping send.")
        return False
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as exc:
        print(f"[notifier] send failed: {exc}")
        return False


def send_photo(photo_url: str, caption: str, reply_markup=None) -> bool:
    """Send the alert as a photo with the details as caption (max 1024 chars —
    our alerts are well under). Returns False if it fails, so the caller can
    fall back to a plain text message (FB image URLs can expire / be blocked)."""
    token, chat_id = _creds()
    if not token or not chat_id:
        return False
    payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption,
               "parse_mode": "MarkdownV2"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto", json=payload, timeout=20)
        r.raise_for_status()
        return True
    except Exception as exc:
        print(f"[notifier] send_photo failed ({exc}); falling back to text")
        return False


def send_media_group(photo_urls: list, caption: str) -> bool:
    """Send 2–10 photos as an album, with the details as the first photo's
    caption. Returns False on failure so the caller can fall back."""
    token, chat_id = _creds()
    if not token or not chat_id:
        return False
    media = []
    for i, url in enumerate(photo_urls[:10]):
        item = {"type": "photo", "media": url}
        if i == 0:
            item["caption"] = caption
            item["parse_mode"] = "MarkdownV2"
        media.append(item)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMediaGroup",
            json={"chat_id": chat_id, "media": media}, timeout=30,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        print(f"[notifier] send_media_group failed ({exc})")
        return False


def _send_alert(res: PipelineResult) -> None:
    """Send one listing alert: an album if there are several photos, a single
    photo if there's one, else text — each falling back to the next if it fails,
    so the alert always gets through."""
    text = format_alert(res)
    kb = _keyboard(res.dedup_key)
    imgs = res.images or []
    if len(imgs) >= 2 and send_media_group(imgs, text):
        # albums can't carry buttons — send them as a small follow-up message
        if kb:
            send("👆 פעולות לדירה שלמעלה:", reply_markup=kb)
        return
    if len(imgs) >= 1 and send_photo(imgs[0], text, reply_markup=kb):
        return
    send(text, reply_markup=kb)


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
