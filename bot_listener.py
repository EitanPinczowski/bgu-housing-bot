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

import re
import sqlite3
from datetime import datetime

import requests

import config
import dm_digest
import doctor
import fit
import geocode
import notifier
import pipeline
import query
import sheets
import storage
import weekly_digest

# Auto-pin: /unknowns caches its Overpass suggestions here so a 📌 button can carry a
# short id (Telegram callback_data is capped at 64 bytes — too small for a Hebrew name).
_pending_pins: dict = {}

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
    # 📌 auto-pin from /unknowns: key is a short id into _pending_pins
    if action == "pin":
        sug = _pending_pins.get(key)
        if sug:
            name, lat, lon = sug
            try:
                geocode.add_pin(name, lat, lon)
                _api("answerCallbackQuery", callback_query_id=cb["id"], show_alert=True,
                     text=f"📌 נקבע: {name} → {lat:.4f},{lon:.4f}")
            except Exception as exc:
                _api("answerCallbackQuery", callback_query_id=cb["id"], text=f"שגיאה: {exc}")
        else:
            _api("answerCallbackQuery", callback_query_id=cb["id"], text="ההצעה פגה — הריצו /unknowns שוב")
        return
    # ℹ️ why / 📵 contacted on an alert
    if action == "why":
        _api("answerCallbackQuery", callback_query_id=cb["id"])
        _reply((cb.get("message") or {}).get("chat", {}).get("id"), _why_text(key))
        return
    if action == "contacted":
        storage.set_contacted(key)
        _api("answerCallbackQuery", callback_query_id=cb["id"], show_alert=True,
             text="📵 סומן כ'יצרתי קשר' — לא יופיע שוב ב-/top")
        return
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


def _reply_kb(chat_id, text: str, kb) -> None:
    try:
        _api("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True,
             reply_markup={"inline_keyboard": kb})
    except Exception as exc:
        print("[listener] reply_kb failed:", exc)


def _why_text(key: str) -> str:
    """The fit breakdown (top positive factors) for a listing — the 'ℹ️ למה' reply."""
    with sqlite3.connect(config.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM listings WHERE dedup_key=?", (key,)).fetchone()
    if not r:
        return "לא נמצאה דירה."
    parts = fit.breakdown(
        r["price_per_room"], r["walk_minutes"], r["location_tier"], r["available_rooms"],
        r["total_roommates"], bool(r["price_from_comment"]), None, r["lease_start"],
        (r["furnished"] == 1) if r["furnished"] is not None else None, r["floor"],
        (r["elevator"] == 1) if r["elevator"] is not None else None, r["balcony"], None,
        has_photos=bool(r["images"] and r["images"] != "[]"))
    top = fit.top_factors(parts, n=5)
    eff = storage.effective_score(key, r["score"] or 0)
    out = f"ℹ️ {r['address'] or 'דירה'} — ניקוד {eff}\n" + " · ".join(f"{n} +{d}" for n, d in top)
    negs = [f"{n} {d}" for n, d in parts if d < 0]
    if negs:
        out += "\nנוכה: " + " · ".join(negs)
    return out


def _cmd_top(chat_id, arg) -> None:
    n = int(arg) if arg.strip().isdigit() and 1 <= int(arg) <= 20 else 5
    contacted = storage.contacted_keys()
    rows = [r for r in query.search("", limit=n * 3) if r["dedup_key"] not in contacted][:n]
    _reply(chat_id, "🏆 הדירות הכי טובות:\n\n" + _format_results(rows))


def _cmd_saved(chat_id) -> None:
    rows = storage.saved_listings()
    if not rows:
        _reply(chat_id, "עדיין לא שמרתם דירות (הקישו ⭐ על התראה).")
        return
    for r in rows:
        r["eff_score"] = storage.effective_score(r["dedup_key"], r["score"] or 0)
    _reply(chat_id, "⭐ דירות ששמרתם:\n\n" + _format_results(rows))


def _cmd_doctor(chat_id) -> None:
    lines = ["🩺 בדיקת תלויות:"]
    for name, status, detail, _ in doctor.checks():
        lines.append(f"{doctor._ICON.get(status, '')} {name}: {status} — {detail}")
    _reply(chat_id, "\n".join(lines))


def _cmd_stats(chat_id) -> None:
    vc = storage.verdict_counts()
    drops = storage.drop_reason_counts()[:5]
    out = ["📊 סטטיסטיקת מאגר:", "מצב: " + " · ".join(f"{k} {v}" for k, v in vc.items())]
    if drops:
        out.append("סיבות סינון נפוצות: " + " · ".join(f"{r} ({c})" for r, c in drops))
    _reply(chat_id, "\n".join(out))


def _cmd_unknowns(chat_id) -> None:
    rows = storage.unknown_locations(7)[:6]
    if not rows:
        _reply(chat_id, "אין מקומות שלא מופו 🎉")
        return
    _pending_pins.clear()
    _reply(chat_id, "🗺️ מקומות שלא מופו (הקישו 📌 כדי לקבע את ההצעה):")
    for i, (loc, cnt, _ts) in enumerate(rows):
        sug = dm_digest._suggest(loc)                 # (osm_name, lat, lon) or None (paced)
        if sug:
            _pending_pins[str(i)] = (loc, sug[1], sug[2])
            _reply_kb(chat_id, f"📍 {loc} ×{cnt}\nהצעה: {sug[0]} {sug[1]},{sug[2]}",
                      [[{"text": "📌 קבע", "callback_data": f"pin|{i}"}]])
        else:
            _reply(chat_id, f"📍 {loc} ×{cnt} — אין הצעה (קבעו ידנית עם /pin)")


def _cmd_pin(chat_id, arg) -> None:
    m = re.search(r"(-?\d+\.\d+)\s*[, ]\s*(-?\d+\.\d+)\s*$", arg.strip())
    name = arg[:m.start()].strip().rstrip(",").strip() if m else ""
    if not (m and name):
        _reply(chat_id, "שימוש: /pin <שם> <lat,lon>\nלמשל: /pin כיכר האבות 31.2618,34.7947")
        return
    geocode.add_pin(name, float(m.group(1)), float(m.group(2)))
    _reply(chat_id, f"📌 נקבע: {name} → {float(m.group(1)):.5f},{float(m.group(2)):.5f}")


def _cmd_uncache(chat_id, arg) -> None:
    if not arg.strip():
        _reply(chat_id, "שימוש: /uncache <שם/כתובת>")
        return
    removed = geocode.uncache(arg.strip())
    _reply(chat_id, f"נוקו {len(removed)} רשומות: {removed}" if removed else "לא נמצאה התאמה במטמון.")


def _classify_summary(res) -> str:
    e = res.extract
    st = {"MATCH": "✅ התאמה", "NEEDS_DATA": "⚠️ חוסר מידע", "DROP": "🗑 נפסל",
          "NOT_AD": "לא מודעה"}.get(res.status.value, res.status.value)
    lines = [f"{st} · {res.location_tier or ''} · ניקוד {res.score if res.score is not None else '—'}"]
    if res.reason:
        lines.append(f"סיבה: {res.reason}")
    if e:
        bits = [b for b in (e.street_address_or_neighborhood,
                            f"{e.price_per_room_ils}₪" if e.price_per_room_ils else None,
                            f"{e.available_rooms_count} חד׳" if e.available_rooms_count is not None else None,
                            f"{round(res.walk_minutes)} דק׳" if res.walk_minutes is not None else None) if b]
        if bits:
            lines.append(" · ".join(str(b) for b in bits))
    return "\n".join(lines)


def _cmd_classify(chat_id, arg) -> None:
    if not arg.strip():
        _reply(chat_id, "הדביקו טקסט של מודעה אחרי /classify כדי לבדוק אותה.")
        return
    try:
        res = pipeline.process_post(arg, commit=False)   # pure: no store, no alert
        _reply(chat_id, _classify_summary(res))
    except Exception as exc:
        _reply(chat_id, f"שגיאה בסיווג: {exc}")


_HELP = ("🤖 פקודות:\n"
         "/top [N] — הדירות הכי טובות כרגע\n"
         "/saved — דירות ששמרתם (⭐)\n"
         "/search <שאילתה> — חיפוש חופשי\n"
         "/classify <טקסט מודעה> — לבדוק מודעה שהדבקתם\n"
         "/unknowns — מקומות שלא מופו (כפתור 📌 לקיבוע)\n"
         "/pin <שם> <lat,lon> · /uncache <שם>\n"
         "/stats · /status · /doctor · /sheet · /help")


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
    cid = chat["id"]
    if cmd == "search":
        if not arg.strip():
            _reply(cid, query.HELP)
        else:
            try:
                _reply(cid, _format_results(query.search(arg, limit=10)))
            except Exception as exc:
                _reply(cid, f"שגיאה בחיפוש: {exc}")
    elif cmd == "status":
        _reply(cid, _status_text())
    elif cmd == "top":
        _cmd_top(cid, arg)
    elif cmd == "saved":
        _cmd_saved(cid)
    elif cmd == "doctor":
        _cmd_doctor(cid)
    elif cmd == "stats":
        _cmd_stats(cid)
    elif cmd == "unknowns":
        _cmd_unknowns(cid)
    elif cmd == "pin":
        _cmd_pin(cid, arg)
    elif cmd == "uncache":
        _cmd_uncache(cid, arg)
    elif cmd == "classify":
        _cmd_classify(cid, arg)
    elif cmd == "sheet":
        sid = os.environ.get("GOOGLE_SHEET_ID")
        _reply(cid, f"https://docs.google.com/spreadsheets/d/{sid}" if sid else "גיליון לא מוגדר.")
    else:
        _reply(cid, _HELP)


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
