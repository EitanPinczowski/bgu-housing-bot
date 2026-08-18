#!/usr/bin/env python
"""PreToolUse guard: refuse a command whose preconditions are not met.

CLAUDE.md, on `replay.py --apply`: "Two preconditions for `--apply`, neither of which
announces itself." OSRM down silently substitutes the straight-line walk estimate, and
the AMBER boundary IS a walk time — so applying then bakes the approximation into every
tier and score. A concurrent scrape leaves the DB half-rewritten. Both failures are
quiet, arrive later, and look like data rather than like a mistake.

That warning is written in three separate places in CLAUDE.md, which is what prose does
instead of enforcing. This enforces it.

Deliberate escape hatch: prefix the command with BGU_SKIP_GUARD=1 when the exception is
considered. It is meant to be typed on purpose, not reached for reflexively.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))

BLOCK = 2          # PreToolUse: exit 2 blocks the call, stderr is fed back as the reason
ALLOW = 0

# (label, command regex, {precondition: why it matters}) — first matching rule wins.
RULES = [
    ("replay --apply",
     r"replay\.py\b(?=.*--apply)",
     {"no_scrape": "a scrape is running: both processes write the same SQLite and a "
                   "collision leaves the DB half-rewritten",
      "osrm_up": "OSRM is down: the AMBER boundary IS a walk time, so applying now bakes "
                 "the straight-line approximation into every tier and score"}),

    ("LLM harness",
     r"\b(model_ab|batch_ab)\.py\b|replay\.py\b(?=.*--llm)",
     {"no_scrape": "a scrape is running: Gemini pacing is per PROCESS but the RPM limit "
                   "is per PROJECT, so two writers issue ~27/min against a limit of 15"}),

    ("DB writer",
     r"\b(warm_cache|resolve_unknowns|link_backfill|backfill_first_seen)\.py\b",
     {"no_scrape": "a scrape is running and this writes the same SQLite"}),

    ("secret staging",
     r"git\s+add\b[^&|;]*(\.env\b|(?:^|[/\s\"'])auth[/\s\"']|(?:^|[/\s\"'])data[/\s\"'])",
     {"never": "`.env`, `auth/` and `data/` are git-ignored on purpose — the code repo is "
               "PUBLIC and git history is permanent, so ~350 landlords' phone numbers "
               "could never be taken back"}),

    # Two corrections, both found by this guard failing on real commands:
    #  * `pytest(?![-\w])`, not `pytest\b` — \b matches at the HYPHEN, so the word
    #    boundary turned `pip install pytest-xdist | tail` into a blocked command. The
    #    rule is about running the RUNNER, never a package named after it.
    #  * ANY pipe, not just one straight into tail/head. `pytest | tr | grep | tail`
    #    slipped through and discards the exit code exactly as much — CLAUDE.md's rule is
    #    "read the count, or drop the pipe", not "avoid one particular pipeline".
    # A THIRD correction, and the same root cause as the first two: matching a WORD where
    # a COMMAND was meant. `grep -l "pytest\|ruff" .github/workflows/*` and
    # `grep -n "pytest" -A3 file | head` were both blocked on 2026-08-18 — neither runs
    # anything. That is the 7th false positive this guard has produced, every one from a
    # pattern that matched text rather than an invocation.
    #
    # `executable_part()` deliberately does NOT strip quoted strings, and its docstring is
    # right that stripping them everywhere would hide a real `docker rmi "osrm_bgu"`. That
    # argument is about the DESTRUCTIVE rules. This rule protects an exit code: a false
    # negative costs a re-run, a false positive blocks correct work, and a guard that
    # blocks correct work is a guard that gets switched off. So require an invocation —
    # start of the command, after a separator, or `python -m pytest`.
    ("pytest exit code",
     r"(?:^|[;&|]\s*|\bpython[\d.]*\s+(?:-\w+\s+)*-m\s+)pytest(?![-\w])[^|]*\|",
     {"never": "piping pytest discards its EXIT CODE (CLAUDE.md) — a failing suite then "
               "reads as a passing one. Read the count, or drop the pipe"}),

    ("OSRM container",
     r"docker\s+(rmi|image\s+rm)\b|docker\s+(system|volume|image)\s+prune\b"
     r"|docker\s+rm\b[^&|;]*osrm_bgu",
     {"never": "this can destroy `osrm_bgu` — a multi-GB rebuild from the Israel PBF"}),

    # THE LESSON IS WIDER THAN THE ONE COMMAND IT WAS ENFORCED ON, and CLAUDE.md already
    # said so. The pytest rule above was written after `pytest -q | tail` hid a failure;
    # the identical mistake then cost two full 26-minute `replay.py` previews and a
    # 10-minute geo_accuracy re-run on 2026-08-10, because these scripts print their
    # SUMMARY FIRST — `replayed / now: / changed:` and the p50/p90/max table — and cap the
    # detail that follows. `| tail` keeps the detail and throws away the answer. Worse
    # when backgrounded: the pipe truncates the output FILE too, so the summary never
    # reaches disk and the whole run has to be repeated.
    #
    # Listed explicitly rather than matched on `*.py`, and required to be INVOKED rather
    # than merely named: `cat replay.py | head -20` and `grep -n x stats.py | head` are
    # both fine and both were blocked by the first version of this rule. That is the same
    # over-broad mistake as the earlier `pytest\b` matching `pytest-xdist` — the fifth
    # false positive this guard has produced, every one from a pattern that matched a
    # WORD instead of a command.
    # `tail` ONLY, NOT `head`. The pytest rule above blocks every pipe because what it
    # protects is the EXIT CODE, which any pipe destroys. Here what is protected is the
    # SUMMARY, and these scripts print it FIRST — so `| head` keeps the answer and is
    # fine, while `| tail` keeps the detail and drops it. Copying `tail|head` across from
    # the pytest rule blocked `seed_anchors.py --dry-run | head -3` within a minute of
    # shipping: the sixth false positive from generalising a pattern without checking
    # both directions.
    ("summary discarded by the pipe",
     r"(?:python[\d.]*\s+(?:-\w+\s+)*|\./)"
     r"(replay|geo_accuracy|stats|doctor|seed_anchors|model_ab|batch_ab"
     r"|unique_report|audit_geocode|group_report)\.py\b[^|]*\|\s*tail\b",
     {"never": "these print their SUMMARY FIRST and cap the detail after it, so `| tail` "
               "keeps the noise and drops the answer — and when backgrounded it truncates "
               "the output file too. Redirect to a file and read it, or use `| head`"}),
]


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?^\2\s*$", re.S | re.M)
_MSG_ARG = re.compile(r"(-m|--message)\s+(['\"])(?:\\.|(?!\2).)*\2", re.S)


def executable_part(cmd: str) -> str:
    """The part of a shell command that will actually RUN, with literal text removed.

    A guard that greps the raw command string cannot tell a command from a sentence
    ABOUT that command. Writing a commit message that quotes `replay.py --apply` — which
    is exactly what a commit recording this guard does — matched the rule and blocked the
    commit. A false block on correct work is how a guard gets switched off, so the two
    obvious carriers of prose are stripped before matching:

      * heredoc bodies (`git commit -F - <<'EOF' … EOF`)
      * quoted `-m` / `--message` arguments

    It stops there on purpose. Stripping EVERY quoted string would also hide a real
    `docker rmi "osrm_bgu"`, and a false negative on an irreversible destroy costs far
    more than a false block on `echo 'never run docker rmi osrm_bgu'` — which stays
    blocked, and is one BGU_SKIP_GUARD=1 away. Commit messages are the only prose
    carrier common enough to be worth the risk of a gap.

    This is deliberately not a shell parser. It removes the cases that carry English;
    anything left is close enough to a command to judge.
    """
    return _MSG_ARG.sub(" ", _HEREDOC.sub(" ", cmd))


def probe(name: str):
    """True = precondition satisfied, False = violated, None = could not tell."""
    if name == "never":
        return False
    if name == "no_scrape":
        import scraper
        running = scraper.run_in_progress()       # True / False / None ("couldn't ask")
        return None if running is None else (not running)
    if name == "osrm_up":
        import osrm
        return osrm.alive()
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return ALLOW                              # nothing to judge; never block on noise
    cmd = (payload.get("tool_input") or {}).get("command", "") or ""

    if "BGU_SKIP_GUARD=1" in cmd:
        return ALLOW

    # ONE COMMAND PER SEGMENT. `a && b | tail` chains two commands; the pipe belongs to
    # `b`. Matching the whole string let the pytest rule claim a pipe several `&&` away —
    # `pytest … && … && python hook.py | tail` was blocked for a pipe on the hook. Split
    # first, then judge each piece on its own. Heredoc bodies are removed BEFORE the
    # split, or their `;` and newlines would shred the message into fake segments.
    segments = re.split(r"&&|\|\||;|\n", executable_part(cmd))

    for label, pattern, checks in RULES:
        if not any(re.search(pattern, seg) for seg in segments):
            continue
        for precondition, reason in checks.items():
            try:
                ok = probe(precondition)
            except Exception as e:
                # A GUARD THAT BREAKS MUST NOT BECOME THE THING THAT STOPS WORK. It
                # exists to prevent a rare quiet corruption; blocking every command
                # because it cannot answer would be the worse failure by far.
                print(f"[guard] {label}: could not check {precondition} ({e}) — allowing",
                      file=sys.stderr)
                continue
            if ok is False:
                print(f"BLOCKED ({label}): {reason}.\n"
                      f"Fix it, or prefix the command with BGU_SKIP_GUARD=1 if this is "
                      f"deliberate.", file=sys.stderr)
                return BLOCK
            if ok is None:
                # Unlike scraper.run_in_progress() and doctor's `scraper progress` row —
                # which REPORT, and where "couldn't ask" rightly means WARN — this GATES
                # a destructive write. An unanswered question is not permission.
                print(f"BLOCKED ({label}): could not determine whether `{precondition}` "
                      f"holds, and this rewrites the DB. Check with `python doctor.py`, "
                      f"then prefix with BGU_SKIP_GUARD=1 to proceed.", file=sys.stderr)
                return BLOCK
        break
    return ALLOW


if __name__ == "__main__":
    sys.exit(main())
