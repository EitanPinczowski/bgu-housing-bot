#!/usr/bin/env python
"""PostToolUse: lint the file that was just edited.

`.pre-commit-config.yaml` already runs ruff, and names the bug class it is there for:
"Catches undefined names / unused imports — the class of bug that let the
OVERPASS_URL -> OVERPASS_URLS rename slip through." But a pre-commit hook only fires at
commit time, which can be an hour and twenty edits later, by which point the undefined
name has been built on.

This runs the same linter against the one file that just changed, so the feedback
arrives while the edit is still the thing being thought about.

`ruff` is NOT on PATH in this environment — only `python -m ruff` works — so it is
invoked through sys.executable rather than as a bare command.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

BLOCK = 2          # PostToolUse: exit 2 feeds stderr back so the finding can be fixed
ALLOW = 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return ALLOW

    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.endswith(".py") or not os.path.exists(path):
        return ALLOW

    project = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    try:
        r = subprocess.run(
            [sys.executable, "-m", "ruff", "check", path],
            capture_output=True, text=True, timeout=15, cwd=project,
        )
    except Exception as e:
        # Same rule as guard.py: a linter that cannot run must not become the thing that
        # stops work. Say so and get out of the way.
        print(f"[ruff] could not run ({e}) — skipping", file=sys.stderr)
        return ALLOW

    if r.returncode == 0:
        return ALLOW

    out = (r.stdout or r.stderr or "").strip()
    if not out:
        return ALLOW
    print(f"ruff found problems in {os.path.basename(path)}:\n{out}", file=sys.stderr)
    return BLOCK


if __name__ == "__main__":
    sys.exit(main())
