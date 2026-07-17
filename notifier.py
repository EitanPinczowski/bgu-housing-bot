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


def format_alert(res: PipelineResult) -> str:
    e = res.extract
    if res.status == Status.MATCH:
        header = "✅ *MATCH*" if res.preferred else "🟡 *MATCH \\(nearby\\)*"
    else:
        header = "⚠️ *NEEDS DATA*"
    walk = f"{res.walk_minutes:.0f} דק׳ הליכה" if res.walk_minutes is not None else "מרחק לא ידוע"
    lines = [
        header,
        _esc(e.summary_hebrew or ""),
        "",
        f"💰 {_esc(e.price_per_room_ils or '?')} ש\"ח לחדר",
        f"🛏 {_esc(e.available_rooms_count or '?')} חדרים פנויים · {_esc(e.total_roommates_in_apt or '?')} שותפים",
        f"📍 {_esc(e.street_address_or_neighborhood or '?')} · {_esc(walk)}",
        f"📅 {_esc(e.lease_start_date or '?')}",
        f"📞 {_esc(e.contact_phone_or_link or '?')}",
    ]
    if res.source_url:
        lines.append(f"🔗 {_esc(res.source_url)}")
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


def notify(res: PipelineResult) -> None:
    if res.status == Status.MATCH and config.NOTIFY_ON_MATCH:
        send(format_alert(res))
    elif res.status == Status.NEEDS_DATA and config.NOTIFY_ON_NEEDS_DATA:
        send(format_alert(res))
