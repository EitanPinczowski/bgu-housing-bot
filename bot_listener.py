"""
Persistent Telegram listener for the alert buttons (⭐ מעניין / 🗑 הסר).

Runs forever, long-polling for button taps. On a tap it records your choice
('saved' / 'dismissed') in SQLite and in the Google Sheet, shows a toast, and
replaces the buttons with the chosen state. This is the only process that reads
Telegram updates — the scraper/digest/watchdog only send.

    python bot_listener.py

Meant to autostart at login (see README). If it's not running, taps simply
queue on Telegram's side and are processed the next time it starts.
"""
from __future__ import annotations
import os
import time

from dotenv import load_dotenv

# Load .env by this file's own path so it works when autostarted from any cwd.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import sqlite3
from datetime import datetime

import requests

import config
import fit
import notifier
import query
import sheets
import storage
import weekly_digest

_MARK = {"save": "saved", "dismiss": "dismissed"}
_DONE = {"save": "⭐ נשמר", "dismiss": "🗑 הוסר"}
# how to show a mark that's already on record, when telling a repeat voter "no"
_DONE_BY_MARK = {"saved": "⭐ מעניין", "dismissed": "🗑 הוסר"}


def _api(method: str, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    return requests.post(f"https://api.telegram.org/bot{token}/{method}",
                         json=params, timeout=45)


def _update_tally(cb: dict, key: str) -> None:
    """Rewrite the vote buttons on THIS message to show live counts, e.g.
    '⭐ מעניין (3)' / '🗑 הסר (1)'. Reuses the message's existing keyboard so the
    map/WhatsApp/post URL buttons are preserved; only the two vote buttons change."""
    msg = cb.get("message") or {}
    kb = (msg.get("reply_markup") or {}).get("inline_keyboard")
    if not kb or "message_id" not in msg:
        return
    counts = storage.mark_counts(key)
    changed = False
    for row in kb:
        for btn in row:
            data = btn.get("callback_data", "")
            if data == f"save|{key}":
                btn["text"] = f"⭐ מעניין ({counts['saved']})"
                changed = True
            elif data == f"dismiss|{key}":
                btn["text"] = f"🗑 הסר ({counts['dismissed']})"
                changed = True
    if changed:
        try:
            _api("editMessageReplyMarkup", chat_id=msg["chat"]["id"],
                 message_id=msg["message_id"], reply_markup={"inline_keyboard": kb})
        except Exception as exc:
            print("[listener] tally update failed:", exc)


def _handle(cb: dict) -> None:
    action, _, key = (cb.get("data") or "").partition("|")
    mark = _MARK.get(action)
    user = str((cb.get("from") or {}).get("id", ""))
    if not (mark and key and user):
        _api("answerCallbackQuery", callback_query_id=cb["id"])   # clear the spinner
        return
    # One vote per user per apartment — final. If they've already voted, tell
    # them what they picked and change nothing (set_mark returns False, atomically).
    if not storage.set_mark(key, user, mark):
        prior = storage.get_user_mark(key, user)
        _api("answerCallbackQuery", callback_query_id=cb["id"], show_alert=True,
             text=f"כבר הצבעת על דירה זו · {_DONE_BY_MARK.get(prior, '')}")
        return
    # Reflect the group's net adjustment in the sheet's score. Buttons stay in
    # place so OTHER people can still cast their one vote.
    adj = storage.mark_adjustment(key)
    adj_s = f"+{adj}" if adj >= 0 else str(adj)
    try:
        sheets.set_mark(key, adj_s, storage.effective_score(key))
    except Exception as exc:
        print("[listener] sheet update failed:", exc)
    _update_tally(cb, key)   # show the new counts on the buttons
    _api("answerCallbackQuery", callback_query_id=cb["id"], text=f"{_DONE[action]} · ניקוד {adj_s}")


# --- DM text commands (/search, /status) -----------------------------------------
def _reply(chat_id, text: str) -> None:
    try:
        _api("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)
    except Exception as exc:
        print("[listener] reply failed:", exc)


def _osrm_ok() -> bool:
    try:
        r = requests.get(f"{config.OSRM_BASE_URL}/route/v1/foot/34.79,31.25;34.80,31.26",
                         params={"overview": "false"}, timeout=6)
        return r.ok and r.json().get("code") == "Ok"
    except Exception:
        return False


def _status_text() -> str:
    try:
        lines = open(config.DATA_DIR / "search_log.txt", encoding="utf-8").read().splitlines()
    except Exception:
        lines = []
    s = weekly_digest._summarize(lines, datetime.now(), days=1)
    with sqlite3.connect(config.DB_PATH) as c:
        listings = c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        matches = c.execute("SELECT COUNT(*) FROM listings WHERE status='MATCH'").fetchone()[0]
        votes = c.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
    out = ["📟 סטטוס הבוט (24 שעות אחרונות):",
           f"• ריצות: {s['runs']} · דילוגים: {s['skipped']} · חסימות: {s['blocked']}",
           f"• פוסטים: {s['posts']} · התאמות: {s['matches']} · חוסר-מידע: {s['needs']}"]
    if s["read"]:
        out.append(f"• נסרקו {s['read']} · דילוג ישן {s['age_skip']} · דילוג נראו {s['seen_skip']}")
    if s["dangling"]:
        out.append(f"⚠️ {s['dangling']} ריצות שלא הסתיימו (ייתכן קריסה)")
    out.append(f"🗃️ מאגר: {listings} מודעות ({matches} התאמות) · {votes} הצבעות")
    out.append(f"🗺️ OSRM: {'✅ פעיל' if _osrm_ok() else '❌ כבוי'}")
    return "\n".join(out)


def _format_results(rows) -> str:
    if not rows:
        return "לא נמצאו דירות תואמות. נסו מסננים אחרים.\n\n" + query.HELP
    out = [f"נמצאו {len(rows)} דירות (מהטובה):"]
    for r in rows:
        stars = fit.stars(r["eff_score"])
        price = f'{r["price_per_room"]}₪' if r["price_per_room"] else "מחיר?"
        addr = r["address"] or "כתובת?"
        rooms = r["available_rooms"] if r["available_rooms"] is not None else "?"
        walk = f' · {round(r["walk_minutes"])} דק׳' if r["walk_minutes"] is not None else ""
        line = f'{stars} {addr} · {price} · {rooms} חד׳{walk}'
        if r.get("source_url"):
            line += f'\n{r["source_url"]}'
        out.append(line)
    return "\n\n".join(out)


def _handle_message(msg: dict) -> None:
    """DM-only text commands. Group messages and non-owner DMs are ignored."""
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return                                        # DM-only, never the group
    if str(chat.get("id")) not in set(notifier._recipients("primary")):
        return                                        # only the owner's DM
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lstrip("/").lower().split("@")[0]        # tolerate /search@BotName
    if cmd == "search":
        if not arg.strip():
            _reply(chat["id"], query.HELP)
            return
        try:
            _reply(chat["id"], _format_results(query.search(arg, limit=10)))
        except Exception as exc:
            _reply(chat["id"], f"שגיאה בחיפוש: {exc}")
    elif cmd == "status":
        _reply(chat["id"], _status_text())
    else:
        _reply(chat["id"], "פקודות זמינות: /search <שאילתה> · /status\n\n" + query.HELP)


def main() -> None:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("[listener] TELEGRAM_BOT_TOKEN not set — nothing to do.")
        return
    print("[listener] started; waiting for button taps and /search /status…")
    offset = None
    while True:
        try:
            r = _api("getUpdates", offset=offset, timeout=30,
                     allowed_updates=["callback_query", "message"]).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    try:
                        _handle(upd["callback_query"])
                    except Exception as exc:
                        print("[listener] handle error:", exc)
                elif "message" in upd:
                    try:
                        _handle_message(upd["message"])
                    except Exception as exc:
                        print("[listener] message error:", exc)
        except Exception as exc:
            print("[listener] poll error:", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
