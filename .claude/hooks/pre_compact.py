#!/usr/bin/env python
"""PreCompact hook: carry the live state across a compaction.

A long session's state gets summarised, and operational facts are exactly the kind that
survive summarisation as ASSERTIONS while quietly ceasing to be true. "OSRM is down" was
correct at 20:40 and wrong by 20:52 this session; "gemini 480/480" resets at 10:00 Israel
time, not at midnight. Carrying either across a compaction as though still current is the
same failure as CLAUDE.md's hand-dated status block, on a shorter timescale.

So: re-probe at the boundary and state the values WITH the time they were taken, rather
than letting the pre-compaction half's numbers be inherited silently.

Reuses session_start.py rather than duplicating its probes — there is one implementation
of "what is true right now" and this is not a second one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime

ROOT = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def main() -> int:
    try:
        json.load(sys.stdin)
    except Exception:
        pass
    hook = os.path.join(ROOT, ".claude", "hooks", "session_start.py")
    if not os.path.exists(hook):
        return 0
    try:
        r = subprocess.run([sys.executable, hook], capture_output=True, text=True,
                           cwd=ROOT, timeout=40,
                           env={**os.environ, "CLAUDE_PROJECT_DIR": ROOT,
                                "PYTHONUTF8": "1"})
    except Exception:
        return 0
    body = (r.stdout or "").strip()
    if not body:
        return 0
    stamp = datetime.now().strftime("%H:%M")
    print(f"Live state RE-MEASURED at {stamp}, after compaction. Any operational number "
          f"from earlier in this session is stale — use these:\n{body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
