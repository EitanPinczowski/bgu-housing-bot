"""Telegram alerts. Token + chat id come from the environment (.env),
never from code."""
from __future__ import annotations
import os
import re
from urllib.parse import quote

import requests

import config
import fit
from models import PipelineResult, Status


def _contact_link(contact):
    """A tappable WhatsApp/chat link for the contact, or None. Handles Israeli
    mobiles (05X… -> wa.me/9725X…) and existing wa.me/m.me/t.me links."""
    if not contact:
        return None
    c = contact.strip()
    if c.startswith("http"):
        return c if any(d in c for d in ("wa.me", "whatsapp", "m.me", "t.me")) else None
    digits = re.sub(r"\D", "", c)
    if len(digits) == 10 and digits.startswith("05"):
        return "https://wa.me/972" + digits[1:]
    if len(digits) == 12 and digits.startswith("972"):
        return "https://wa.me/" + digits
    return None


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

    if res.status == Status.MATCH:                       # fit score (#4)
        sc = res.score if res.score is not None else fit.score(
            e.price_per_room_ils, res.walk_minutes, res.location_tier,
            e.available_rooms_count, e.total_roommates_in_apt, e.price_from_comment)
        header += "  " + fit.stars(sc)
    lines = [header]
    if e.summary_hebrew:
        lines.append(_esc(e.summary_hebrew))
    lines.append("")  # spacer between the summary and the details

    if e.price_per_room_ils is not None:
        price = f'{e.price_per_room_ils} ש"ח לחדר'
        if e.price_from_comment:
            price += " (מהתגובות — ייתכן שאינו מדויק)"
    else:
        price = "מחיר לא צוין"
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
    # Map / WhatsApp / post links are rendered as BUTTONS (see _alert_keyboard).

    if res.status == Status.NEEDS_DATA and res.reason:
        lines.append("")
        lines.append("_" + _esc(res.reason) + "_")
    return "\n".join(lines)


def _map_url(res) -> str | None:
    """Google Maps SEARCH of the address (Google geocodes it well), else coords."""
    e = res.extract
    if e and e.street_address_or_neighborhood:
        return ("https://www.google.com/maps/search/?api=1&query="
                + quote(f"{e.street_address_or_neighborhood}, באר שבע"))
    if res.lat is not None and res.lon is not None:
        return f"https://www.google.com/maps?q={res.lat},{res.lon}"
    return None


def _post_button(res):
    """(label, url) for the post: the permalink if captured, else a group search
    for the post text (FB hides permalinks for comment-less posts), else None."""
    if res.source_url:
        return ("🔗 צפייה בפוסט", res.source_url)
    if res.group:
        e = res.extract
        q = ((e.summary_hebrew or e.street_address_or_neighborhood or "") if e else "").strip()[:60]
        if q:
            return ("🔍 חיפוש בקבוצה", res.group.rstrip("/") + "/search/?q=" + quote(q))
        return ("🔗 פתיחת הקבוצה",
                res.group + ("&" if "?" in res.group else "?") + "sorting_setting=CHRONOLOGICAL")
    return None


def _token():
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def _chat_ids():
    """Recipients. TELEGRAM_CHAT_ID may be a COMMA-SEPARATED list, so alerts can
    also go to a friend and/or a shared group — a good way to share the search."""
    return [c.strip() for c in (os.environ.get("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()]


def _post_to_all(method: str, payload: dict, timeout: int, primary_only: bool = False) -> bool:
    """Send `payload` to every recipient (or just the first — your own DM — when
    primary_only, for operational pings a shared group shouldn't get). True if at
    least one delivery succeeded."""
    token, ids = _token(), _chat_ids()
    if not token or not ids:
        print("[notifier] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping send.")
        return False
    if primary_only:
        ids = ids[:1]
    ok = False
    for cid in ids:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          json={**payload, "chat_id": cid}, timeout=timeout).raise_for_status()
            ok = True
        except Exception as exc:
            print(f"[notifier] {method} to {cid} failed: {exc}")
    return ok


def _alert_keyboard(res):
    """All action buttons for an alert: map / WhatsApp / post as URL buttons,
    plus ⭐/🗑 triage as callback buttons (handled by bot_listener.py)."""
    e = res.extract
    rows = []
    url_row = []
    murl = _map_url(res)
    if murl:
        url_row.append({"text": "🗺️ מפה", "url": murl})
    wa = _contact_link(e.contact_phone_or_link) if e else None
    if wa:
        url_row.append({"text": "💬 וואטסאפ", "url": wa})
    if url_row:
        rows.append(url_row)
    pb = _post_button(res)
    if pb:
        rows.append([{"text": pb[0], "url": pb[1]}])
    if res.dedup_key:
        rows.append([
            {"text": "⭐ מעניין", "callback_data": f"save|{res.dedup_key}"},
            {"text": "🗑 הסר", "callback_data": f"dismiss|{res.dedup_key}"},
        ])
    return {"inline_keyboard": rows} if rows else None


def send(text: str, reply_markup=None, primary_only: bool = False) -> bool:
    payload = {"text": text, "parse_mode": "MarkdownV2"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post_to_all("sendMessage", payload, 15, primary_only=primary_only)


def send_photo(photo_url: str, caption: str, reply_markup=None,
               primary_only: bool = False) -> bool:
    """Send the alert as a photo with the details as caption. Returns False if it
    reaches no one, so the caller can fall back to a text message."""
    payload = {"photo": photo_url, "caption": caption, "parse_mode": "MarkdownV2"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post_to_all("sendPhoto", payload, 20, primary_only=primary_only)


def send_media_group(photo_urls: list, caption: str, primary_only: bool = False) -> bool:
    """Send 2–10 photos as an album, details as the first photo's caption."""
    media = []
    for i, url in enumerate(photo_urls[:10]):
        item = {"type": "photo", "media": url}
        if i == 0:
            item["caption"] = caption
            item["parse_mode"] = "MarkdownV2"
        media.append(item)
    return _post_to_all("sendMediaGroup", {"media": media}, 30, primary_only=primary_only)


def _send_alert(res: PipelineResult, primary_only: bool = False) -> None:
    """Send one listing alert: an album if there are several photos, a single
    photo if there's one, else text — each falling back to the next if it fails,
    so the alert always gets through. primary_only keeps it in your own DM (for
    testing without spamming a shared group)."""
    text = format_alert(res)
    kb = _alert_keyboard(res)
    imgs = res.images or []
    if len(imgs) >= 2 and send_media_group(imgs, text, primary_only=primary_only):
        # albums can't carry buttons — send them as a small follow-up message
        if kb:
            send("👆 פעולות לדירה שלמעלה:", reply_markup=kb, primary_only=primary_only)
        return
    if len(imgs) >= 1 and send_photo(imgs[0], text, reply_markup=kb, primary_only=primary_only):
        return
    send(text, reply_markup=kb, primary_only=primary_only)


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
