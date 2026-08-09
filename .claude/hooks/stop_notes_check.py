#!/usr/bin/env python
"""Stop hook: did this session quietly break the notes corpus?

`CLAUDE.md` used to be one 1,170-line file; its reference half now lives in
`.claude/skills/`, and the only thing keeping that safe is `split_check.py` proving every
claim is still reachable. A checker that is only run when someone remembers to run it is
the same problem the split was meant to solve.

WARNS, NEVER BLOCKS. Exit 0 always. A session ending is the wrong moment to refuse to
stop, and a hook that can strand you is one you disable — after which it protects
nothing. The output is a heads-up, and the fix is a normal next-session task.

Skips itself entirely when nothing under CLAUDE.md or .claude/skills/ changed, so a
session that never touched the notes pays nothing and prints nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def _touched_notes() -> bool:
    """Did anything in this working tree touch the corpus? Cheap pre-filter."""
    try:
        r = subprocess.run(["git", "status", "--porcelain", "--",
                            "CLAUDE.md", ".claude/skills"],
                           capture_output=True, text=True, cwd=ROOT, timeout=15)
        if r.stdout.strip():
            return True
        # also catch work that is already committed this session
        r = subprocess.run(["git", "diff", "--name-only", "HEAD~3..HEAD", "--",
                            "CLAUDE.md", ".claude/skills"],
                           capture_output=True, text=True, cwd=ROOT, timeout=15)
        return bool(r.stdout.strip())
    except Exception:
        return False


def main() -> int:
    try:
        json.load(sys.stdin)
    except Exception:
        pass
    if not _touched_notes():
        return 0

    try:
        r = subprocess.run([sys.executable, ".claude/tools/split_check.py"],
                           capture_output=True, text=True, cwd=ROOT, timeout=60)
    except Exception:
        return 0                      # never let this be the reason a session cannot end

    if r.returncode != 0:
        out = (r.stdout or "").strip().splitlines()
        head = "\n".join(out[:12])
        print("\n⚠️  THE NOTES CORPUS LOST SOMETHING THIS SESSION.\n"
              f"{head}\n"
              "Put each missing claim back in CLAUDE.md or a skill, VERBATIM. Do not "
              "update the baseline — that launders the loss into the new normal.\n"
              "  python .claude/tools/split_check.py",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
