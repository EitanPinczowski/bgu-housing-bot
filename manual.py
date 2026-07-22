"""
Manual mode — the risk-free way to use the whole pipeline.

Copy a Facebook post's text, paste it here, and press Enter then type END on a
new line (or Ctrl-Z then Enter on Windows). The post is parsed, filtered,
routed, stored, and — if it's a MATCH or NEEDS_DATA — sent to Telegram.
No browser, no scraping, no account risk.

    python manual.py
"""
from __future__ import annotations
import sys

from dotenv import load_dotenv

load_dotenv()

import pipeline
from models import Status

BANNER = """
BGU Housing Bot — manual mode
Paste a post, then a line containing only:  END   (or Ctrl-Z, Enter on Windows)
Type  QUIT  as the first line to exit.
"""


def read_post() -> str | None:
    lines: list[str] = []
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not lines and line.strip().upper() == "QUIT":
            return None
        if line.strip().upper() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def main() -> None:
    import config
    config.validate()                 # fail fast on a broken config
    print(BANNER)
    while True:
        print("\n--- paste post below ---")
        text = read_post()
        if text is None:
            print("bye.")
            return
        if not text:
            continue
        res = pipeline.process_post(text)
        icon = {"MATCH": "✅", "NEEDS_DATA": "⚠️", "DROP": "🗑", "NOT_AD": "🚫"}.get(res.status.value, "")
        print(f"\n{icon} {res.status.value} — {res.reason}")
        if res.walk_minutes is not None:
            print(f"   walk: {res.walk_minutes:.0f} min")
        if res.status in (Status.MATCH, Status.NEEDS_DATA):
            print("   (saved + Telegram alert sent if configured)")


if __name__ == "__main__":
    main()
