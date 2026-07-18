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

import requests

import sheets
import storage

_MARK = {"save": "saved", "dismiss": "dismissed"}
_DONE = {"save": "⭐ נשמר", "dismiss": "🗑 הוסר"}


def _api(method: str, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    return requests.post(f"https://api.telegram.org/bot{token}/{method}",
                         json=params, timeout=45)


def _handle(cb: dict) -> None:
    action, _, key = (cb.get("data") or "").partition("|")
    mark = _MARK.get(action)
    if mark and key:
        storage.set_mark(key, mark)
        try:
            sheets.set_mark(key, mark)
        except Exception as exc:
            print("[listener] sheet update failed:", exc)
        _api("answerCallbackQuery", callback_query_id=cb["id"], text=_DONE[action])
        msg = cb.get("message") or {}
        if msg:
            _api("editMessageReplyMarkup",
                 chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                 reply_markup={"inline_keyboard": [[{"text": _DONE[action],
                                                     "callback_data": "noop"}]]})
    else:
        _api("answerCallbackQuery", callback_query_id=cb["id"])   # clear the spinner


def main() -> None:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("[listener] TELEGRAM_BOT_TOKEN not set — nothing to do.")
        return
    print("[listener] started; waiting for button taps…")
    offset = None
    while True:
        try:
            r = _api("getUpdates", offset=offset, timeout=30,
                     allowed_updates=["callback_query"]).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    try:
                        _handle(upd["callback_query"])
                    except Exception as exc:
                        print("[listener] handle error:", exc)
        except Exception as exc:
            print("[listener] poll error:", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
