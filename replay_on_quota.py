"""
Run the clean Gemini re-extract of the top listings AS SOON AS Gemini quota is back.

Scheduled hourly (see the "BGU Replay Quota" task). Each run: if we already ran, or
Gemini quota is still exhausted, it no-ops; the moment quota is available it runs
`replay.py --llm --apply --min-score 70 --prune-orphans` (accurate Gemini re-extract
of the score≥70 listings + orphan cleanup), writes a done-marker, and deletes its own
scheduled task. One-shot, self-cleaning.

    python replay_on_quota.py
"""
from __future__ import annotations
import os
import subprocess
import sys
from datetime import datetime

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import llm

_MARKER = config.DATA_DIR / "replay_quota_done.txt"
_TASK = "BGU Replay Quota"


def _gemini_available() -> bool:
    """True if a minimal Gemini call succeeds (quota is back). False on a quota error
    (still exhausted) or any other error (retry next hour)."""
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        client.models.generate_content(
            model=config.GEMINI_MODEL, contents=["ping"],
            config=types.GenerateContentConfig(max_output_tokens=1))
        return True
    except Exception as exc:
        if llm._is_quota_error(exc):
            print(f"{datetime.now():%H:%M}  gemini quota still exhausted — will retry")
        else:
            print(f"{datetime.now():%H:%M}  gemini probe error (will retry): {exc}")
        return False


def main() -> None:
    if _MARKER.exists():
        print(f"already ran: {_MARKER.read_text().strip()}")
        return
    if not _gemini_available():
        return
    print(f"{datetime.now():%H:%M}  gemini quota is back — running the clean replay")
    r = subprocess.run([sys.executable, "replay.py", "--llm", "--apply",
                        "--min-score", "70", "--prune-orphans"], cwd=_HERE)
    if r.returncode == 0:
        _MARKER.write_text(f"done {datetime.now():%Y-%m-%d %H:%M}")
        # one-shot: remove our own scheduled task so it doesn't keep firing
        subprocess.run(["schtasks", "/delete", "/tn", _TASK, "/f"],
                       capture_output=True)
        print("replay complete; marker written and task removed")
    else:
        print(f"replay failed (exit {r.returncode}) — will retry next hour")


if __name__ == "__main__":
    main()
