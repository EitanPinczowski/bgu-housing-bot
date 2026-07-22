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

    # Fit rating: stars + the numeric score (0–100), shown for matches and
    # near-misses alike so you can see how strong each one is at a glance.
    sc = res.score if res.score is not None else fit.score(
        e.price_per_room_ils, res.walk_minutes, res.location_tier,
        e.available_rooms_count, e.total_roommates_in_apt, e.price_from_comment)
    header += f"  {fit.stars(sc)} \\({sc}\\)"
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

    if getattr(e, "floor", None):
        lines.append(f"🏢 קומה {_esc(e.floor)}")
    if getattr(e, "furnished", None):
        lines.append("🛋️ מרוהט")
    if getattr(e, "balcony_or_garden", None):
        lines.append(f"🌿 {_esc(e.balcony_or_garden)}")
    if e.lease_start_date:
        lines.append(f"📅 כניסה: {_esc(e.lease_start_date)}")
    if e.contact_phone_or_link:
        lines.append(f"📞 {_esc(e.contact_phone_or_link)}")
    # Map / WhatsApp / post links are rendered as BUTTONS (see _alert_keyboard).

    # "why this score" — the top few positive factors plus any notable penalty (e.g.
    # a high floor with no elevator), from the same breakdown the score is summed from.
    _bd = fit.breakdown(
        e.price_per_room_ils, res.walk_minutes, res.location_tier,
        e.available_rooms_count, e.total_roommates_in_apt, e.price_from_comment,
        furnished=getattr(e, "furnished", None), lease_start=e.lease_start_date,
        floor=getattr(e, "floor", None), has_elevator=getattr(e, "has_elevator", None),
        has_balcony=getattr(e, "balcony_or_garden", None))
    factors = fit.top_factors(_bd)
    factors += sorted((p for p in _bd if p[1] < 0), key=lambda p: p[1])[:2]   # notable penalties
    if factors:
        lines.append("📊 " + _esc(" · ".join(f"{lbl} {d:+d}" for lbl, d in factors)))

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


def _recipients(target: str) -> list:
    """Pick chat ids by role. Telegram group/channel ids are negative, personal
    DMs positive — so we route by sign, not position:
      'group'   -> listings go to the shared group only
      'primary' -> your own DM only (operational pings, the daily DM digest)
      'all'     -> everyone
    Each falls back to the full list if that role isn't configured, so a message
    is never silently dropped."""
    ids = _chat_ids()
    if not ids:
        return []
    if target == "primary":
        return [c for c in ids if not c.lstrip().startswith("-")] or ids
    if target == "group":
        return [c for c in ids if c.lstrip().startswith("-")] or ids
    return ids


def _unesc(s) -> str:
    """Strip the MarkdownV2 escape backslashes _esc added, for a plain-text resend."""
    return re.sub(r"\\([" + re.escape(r"_*[]()~`>#+-=|{}.!") + r"])", r"\1", "" if s is None else str(s))


def _plain_payload(payload: dict) -> dict:
    """The same payload with MarkdownV2 dropped and its text/caption de-escaped — a
    readable plain-text fallback when Telegram rejects the formatted version."""
    p = {k: v for k, v in payload.items() if k != "parse_mode"}
    if "text" in p:
        p["text"] = _unesc(p["text"])
    if "caption" in p:
        p["caption"] = _unesc(p["caption"])
    if isinstance(p.get("media"), list):
        p["media"] = [{k: (_unesc(v) if k == "caption" else v)
                       for k, v in item.items() if k != "parse_mode"} for item in p["media"]]
    return p


def _try_send(token: str, method: str, body: dict, timeout: int):
    """(response_json, http_status). response_json is None on failure; status is the
    HTTP code when the server answered (e.g. 400 for a bad MarkdownV2), else None."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json(), r.status_code
    except Exception as exc:
        return None, getattr(getattr(exc, "response", None), "status_code", None)


def _post_to_all(method: str, payload: dict, timeout: int, target: str = "all"):
    """Send `payload` to the chosen recipients (see _recipients). Returns the
    first successful response JSON (truthy) or None (falsy), so callers can both
    test success and read file_ids out of it. On a 400 (Telegram rejected the
    MarkdownV2 formatting — a stray unescaped char), the same content is resent as
    PLAIN TEXT so a formatting slip never silently loses an alert."""
    token, ids = _token(), _recipients(target)
    if not token or not ids:
        print("[notifier] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping send.")
        return None
    formatted = "parse_mode" in payload or method == "sendMediaGroup"
    first_ok = None
    for cid in ids:
        resp, status = _try_send(token, method, {**payload, "chat_id": cid}, timeout)
        if resp is None and status == 400 and formatted:
            print(f"[notifier] {method} to {cid}: 400 (bad formatting) — resending as plain text")
            resp, status = _try_send(token, method, {**_plain_payload(payload), "chat_id": cid}, timeout)
        if resp is None:
            print(f"[notifier] {method} to {cid} failed (status {status})")
        elif first_ok is None:
            first_ok = resp
    return first_ok


def _largest_photo_id(photo_sizes) -> str | None:
    """The file_id of the largest rendition in a Telegram PhotoSize array."""
    return photo_sizes[-1].get("file_id") if photo_sizes else None


def _file_ids_from_response(resp) -> list:
    """Reusable photo file_ids from a sendPhoto (one Message) or sendMediaGroup
    (list of Messages) response. Empty list if none / on any shape surprise."""
    if not resp or not resp.get("ok"):
        return []
    result = resp.get("result")
    out = []
    if isinstance(result, list):                       # sendMediaGroup
        for msg in result:
            fid = _largest_photo_id(msg.get("photo") or [])
            if fid:
                out.append(fid)
    elif isinstance(result, dict):                     # sendPhoto
        fid = _largest_photo_id(result.get("photo") or [])
        if fid:
            out.append(fid)
    return out


def _remember_file_ids(res, ids) -> None:
    """Persist captured file_ids so later re-posts (morning/evening top-N) keep
    their photos even after the Facebook image URLs expire. Best-effort."""
    if not ids or not getattr(res, "dedup_key", None):
        return
    try:
        import storage   # local import keeps notifier free of an import-time dep
        storage.set_file_ids(res.dedup_key, ids)
    except Exception as exc:
        print(f"[notifier] could not cache file_ids: {exc}")


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


def send(text: str, reply_markup=None, target: str = "all") -> bool:
    payload = {"text": text, "parse_mode": "MarkdownV2"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post_to_all("sendMessage", payload, 15, target=target)


def send_photo(photo_url: str, caption: str, reply_markup=None,
               target: str = "all") -> bool:
    """Send the alert as a photo with the details as caption. Returns False if it
    reaches no one, so the caller can fall back to a text message."""
    payload = {"photo": photo_url, "caption": caption, "parse_mode": "MarkdownV2"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post_to_all("sendPhoto", payload, 20, target=target)


def send_media_group(photo_urls: list, caption: str, target: str = "all") -> bool:
    """Send 2–10 photos as an album, details as the first photo's caption."""
    media = []
    for i, url in enumerate(photo_urls[:10]):
        item = {"type": "photo", "media": url}
        if i == 0:
            item["caption"] = caption
            item["parse_mode"] = "MarkdownV2"
        media.append(item)
    return _post_to_all("sendMediaGroup", {"media": media}, 30, target=target)


def _send_alert(res: PipelineResult, target: str = "group") -> None:
    """Send one listing alert: an album if there are several photos, a single
    photo if there's one, else text — each falling back to the next if it fails,
    so the alert always gets through. Listings default to the GROUP; pass
    target='primary' to preview in your own DM without touching the group."""
    text = format_alert(res)
    kb = _alert_keyboard(res)
    imgs = res.images or []
    if len(imgs) >= 2:
        resp = send_media_group(imgs, text, target=target)
        if resp:
            _remember_file_ids(res, _file_ids_from_response(resp))
            # albums can't carry buttons — send them as a small follow-up message
            if kb:
                send("👆 פעולות לדירה שלמעלה:", reply_markup=kb, target=target)
            return
    if len(imgs) >= 1:
        resp = send_photo(imgs[0], text, reply_markup=kb, target=target)
        if resp:
            _remember_file_ids(res, _file_ids_from_response(resp))
            return
    send(text, reply_markup=kb, target=target)


def is_alertworthy(res: PipelineResult) -> bool:
    """Is this listing worth pinging: MATCH or NEEDS_DATA whose fit score reaches
    config.MIN_ALERT_SCORE. The single gate shared by notify() and send_batch()."""
    if res.status == Status.MATCH and not config.NOTIFY_ON_MATCH:
        return False
    if res.status == Status.NEEDS_DATA and not config.NOTIFY_ON_NEEDS_DATA:
        return False
    if res.status not in (Status.MATCH, Status.NEEDS_DATA):
        return False
    return res.score is not None and res.score >= config.MIN_ALERT_SCORE


def send_batch(results, target: str = "group", top_k=None) -> int:
    """End-of-run batch: send the alert-worthy results best-first, capped at top_k,
    behind one header — so a run pings once (+ up to K rich alerts) instead of once
    per match. The uncapped remainder stays saved and shows in the top-N digest.
    Returns how many alerts were sent."""
    worthy = sorted((r for r in results if is_alertworthy(r)),
                    key=lambda r: r.score or 0, reverse=True)
    if not worthy:
        return 0
    k = len(worthy) if top_k is None else max(1, min(top_k, len(worthy)))
    head = f"🏠 {len(worthy)} דירות חדשות בסריקה — הטובות ראשונות"
    if len(worthy) > k:
        head += f" (מוצגות {k})"
    send(_esc(head), target=target)
    for r in worthy[:k]:
        _send_alert(r, target=target)
    return k


def notify(res: PipelineResult) -> None:
    """Ping only listings worth looking at: MATCH or NEEDS_DATA whose fit score
    reaches config.MIN_ALERT_SCORE. Everything else is still saved by the
    pipeline (SQLite/Sheets) — it just doesn't buzz your phone."""
    if res.status == Status.MATCH and not config.NOTIFY_ON_MATCH:
        return
    if res.status == Status.NEEDS_DATA and not config.NOTIFY_ON_NEEDS_DATA:
        return
    if res.status not in (Status.MATCH, Status.NEEDS_DATA):
        return
    if res.score is None or res.score < config.MIN_ALERT_SCORE:
        return  # below the quality gate — saved, but not pinged
    _send_alert(res)
