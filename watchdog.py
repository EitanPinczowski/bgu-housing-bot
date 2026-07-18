"""
Dependency health-check. Pings Telegram if something the scraper relies on is
down, so you can fix it BEFORE a scheduled run silently degrades.

    python watchdog.py

Schedule it a bit before each scrape (e.g. :30 past the hour). It's headless
(no browser) so it can run any time. Facebook-login loss is already caught by
the scraper itself (the "0 posts across all groups" alert), so this focuses on
the background services.
"""
from __future__ import annotations
import os

from dotenv import load_dotenv

load_dotenv()

import requests

import config
import notifier


def _osrm_ok() -> bool:
    try:
        r = requests.get(
            f"{config.OSRM_BASE_URL}/route/v1/foot/34.79,31.25;34.8015,31.2622",
            params={"overview": "false"}, timeout=8)
        return r.json().get("code") == "Ok"
    except Exception:
        return False


def _ollama_ok() -> bool:
    # Only relevant if the local model is the configured fallback.
    if getattr(config, "LLM_FALLBACK_PROVIDER", None) != "openai_compatible":
        return True
    base = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
    base = base.rsplit("/v1", 1)[0]
    try:
        return requests.get(f"{base}/api/tags", timeout=8).status_code == 200
    except Exception:
        return False


def main() -> None:
    problems = []
    if not _osrm_ok():
        problems.append("OSRM לא מגיב — לא יהיו זמני הליכה בהתראות (הבוט עדיין עובד).")
    if not _ollama_ok():
        problems.append("Ollama לא מגיב — אין מודל גיבוי אם מכסת Gemini תיגמר.")

    if problems:
        notifier.send(notifier._esc("🩺 בדיקת תלויות מצאה בעיה:\n- " + "\n- ".join(problems)))
        print("ALERT sent:", problems)
    else:
        print("watchdog: all dependencies OK")


if __name__ == "__main__":
    main()
