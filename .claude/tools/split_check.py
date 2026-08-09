#!/usr/bin/env python
"""Did the CLAUDE.md split lose anything?

CLAUDE.md was one 1,170-line file whose reference half now lives in .claude/skills/.
The move was only safe because it is CHECKABLE: this extracts every atomic CLAIM from
the frozen baseline and asserts each one still appears somewhere in the union of
(current CLAUDE.md + every SKILL.md).

Move-only means that union is a SUPERSET of the baseline. This proves it.

    python .claude/tools/split_check.py          # report; exit 1 if anything is missing
    python .claude/tools/split_check.py --list   # print the extracted claim inventory

Matching is VERBATIM on normalised whitespace, which is deliberately strict: it means a
split commit may not reword anything while it moves it. Rewording is a legitimate later
commit, judged on its own — but then this check is what proves the reword was the only
change. Loosening the match would give up the whole guarantee.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE = ROOT / ".claude" / "reference" / "CLAUDE.md.baseline"

# A "claim" is a span that carries meaning on its own and would be NOTICED if lost.
# Bolded spans are the load-bearing ones — CLAUDE.md's house style puts every rule and
# every measured finding in bold.
OTHER_PATTERNS = (
    re.compile(r"^[ \t]*\|(.+\|.+)\|[ \t]*$", re.M),   # measurement table rows
    re.compile(r"`([a-z_]+\.(?:py|json|cmd))`"),       # every file the notes name
)

MIN_CLAIM_LEN = 10


def norm(s: str) -> str:
    """Collapse whitespace so a re-wrapped paragraph still matches."""
    return re.sub(r"\s+", " ", s).strip()


def _bold_spans(text: str) -> list[str]:
    """Every **bolded** span, by SPLITTING on the delimiter rather than regex-matching
    pairs of it.

    A regex like `\\*\\*([^*]{10,}?)\\*\\*` looks right and is wrong: when a bold span is
    shorter than the minimum length the engine skips it and then pairs that span's
    CLOSING `**` with the next span's OPENING `**`, yielding a "claim" made of the
    ordinary prose between two rules. Those straddle section boundaries, so they report
    MISSING precisely when the split was done correctly — a checker that cries wolf on
    correct work is one you learn to ignore.

    Splitting on `**` makes the segments alternate outside/inside, so a closing marker
    can never be paired with the following opening one. An unbalanced trailing `**`
    just leaves a final outside-segment, which is dropped.
    """
    parts = text.split("**")
    return parts[1::2]          # odd indices are the inside-the-delimiters segments


def claims(text: str) -> set[str]:
    out: set[str] = set()
    for span in _bold_spans(text):
        c = norm(span)
        if len(c) >= MIN_CLAIM_LEN:
            out.add(c)
    for pat in OTHER_PATTERNS:
        for m in pat.findall(text):
            c = norm(m)
            if len(c) >= MIN_CLAIM_LEN:
                out.add(c)
    return out


def corpus() -> str:
    """Everything a session can still reach: CLAUDE.md plus every skill."""
    parts = [(ROOT / "CLAUDE.md").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8")
              for p in sorted(ROOT.glob(".claude/skills/*/SKILL.md"))]
    return norm("\n".join(parts))


def missing_claims() -> tuple[set[str], list[str]]:
    """(every baseline claim, the ones no longer reachable)."""
    want = claims(BASELINE.read_text(encoding="utf-8"))
    have = corpus()
    return want, sorted(c for c in want if c not in have)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not BASELINE.exists():
        print(f"FAIL: no baseline at {BASELINE}\n"
              f"      Freeze one BEFORE editing CLAUDE.md:\n"
              f"      cp CLAUDE.md .claude/reference/CLAUDE.md.baseline")
        return 1

    if "--list" in argv:
        print("\n".join(sorted(claims(BASELINE.read_text(encoding="utf-8")))))
        return 0

    want, missing = missing_claims()
    print(f"{len(want) - len(missing)}/{len(want)} baseline claims still reachable.")
    if missing:
        print(f"\nMISSING {len(missing)} — these were in CLAUDE.md and are now nowhere:\n")
        for c in missing:
            print(f"  - {c[:110]}")
        print("\nPut each back in CLAUDE.md or in a skill. Do not reword while moving.")
        return 1
    print("Move-only: nothing lost.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
