#!/usr/bin/env python
"""One line of live bot state, rendered constantly.

READS A CACHE, NEVER PROBES. This runs on every render, and the underlying probes are
not free: `osrm.alive()` is a 3s timeout when the server is down, and `run_in_progress`
was 750ms before psutil. A statusline that shells out on every render is worse than no
statusline.

`.claude/hooks/session_start.py` writes the cache. That means the line can go stale, so
it SHOWS ITS AGE past a few minutes rather than quietly presenting old values as current
— the same reason CLAUDE.md's hand-dated status block was replaced by a measured one.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

DIM = "\033[2m"
RED = "\033[31m"
YEL = "\033[33m"
GRN = "\033[32m"
OFF = "\033[0m"


def _load():
    root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    for rel in ("data/.claude_state.json",):
        p = os.path.join(root, rel)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return None


def main() -> int:
    try:
        json.load(sys.stdin)              # the harness passes session context; unused
    except Exception:
        pass
    try:
        s = _load()
    except Exception:
        s = None
    if not s:
        print(f"{DIM}bgu · no state yet (session_start writes it){OFF}")
        return 0

    bits = []

    scrape = (s.get("scrape") or "").lower()
    if "running" in scrape:
        bits.append(f"{YEL}scrape RUNNING{OFF}")      # do not write the DB
    elif "unknown" in scrape:
        bits.append(f"{YEL}scrape ?{OFF}")
    else:
        bits.append(f"{DIM}scrape idle{OFF}")

    osrm = (s.get("osrm") or "").lower()
    bits.append(f"{GRN}osrm up{OFF}" if osrm.startswith("up") else f"{RED}OSRM DOWN{OFF}")

    if s.get("listings"):
        bits.append(f"{DIM}{s['listings']}{OFF}")

    gem = s.get("gemini") or ""
    if "of" in gem:                        # "…3.5-flash-lite=480, … of 480 (resets …)"
        try:
            used = gem.split("=")[1].split(",")[0].strip()
            cap = gem.split(" of ")[1].split(" ")[0]
            colour = RED if int(used) >= int(cap) else DIM
            bits.append(f"{colour}gemini {used}/{cap}{OFF}")
        except Exception:
            pass

    # How old is all of this? Silent while fresh; loud once it cannot be trusted.
    try:
        age_min = (datetime.now()
                   - datetime.fromisoformat(s["generated"])).total_seconds() / 60
        if age_min > 30:
            bits.append(f"{YEL}({age_min / 60:.0f}h old){OFF}")
        elif age_min > 5:
            bits.append(f"{DIM}({age_min:.0f}m old){OFF}")
    except Exception:
        pass

    print(f"{DIM}bgu{OFF} " + f"{DIM} · {OFF}".join(bits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
