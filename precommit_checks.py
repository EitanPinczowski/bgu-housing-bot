"""Pre-commit safety nets that run under the INSTALLED python, not a venv copy.

    python precommit_checks.py secrets  FILE...
    python precommit_checks.py large    FILE...   (--maxkb, default 2048)
    python precommit_checks.py conflict FILE...
    python precommit_checks.py parse    FILE...   (json / yaml validity)

WHY THIS EXISTS. Smart App Control was switched on for this machine (2026-08-18,
`VerifiedAndReputablePolicyState: 1`) and began refusing every pre-commit hook with
`[WinError 4551] An Application Control policy has blocked this file`, so `git commit`
could not complete at all. The blocked hooks each ran from a pre-commit venv holding a
COPIED `python.exe`; a copy carries no signature and no reputation, so SAC refuses it.
`ruff` survived because its binary is provisioned differently.

Smart App Control has **no allow-list and no per-folder exclusion** — that is Microsoft's
design — and turning it off is irreversible without reinstalling Windows. So the fix is to
stop needing a venv: these run through `language: system`, i.e. the same signed
`AppData\\Local\\Python\\...\\python.exe` that pre-commit itself already launches.

WHAT WAS LOST, AND WHAT WAS NOT. `gitleaks` is a Go program that pre-commit builds from
source; there is no way to run it here without a toolchain SAC would also refuse. The
`secrets` check below is **narrower than gitleaks** — it looks for the specific shapes this
project actually handles rather than gitleaks' full rule set. That is a real reduction in
coverage and is recorded here rather than glossed over.

It is not the only guard. `.gitignore` keeps `.env`, `auth/` and `data/` out, and
`.claude/hooks/guard.py` blocks `git add` of any of them — because this repo is PUBLIC and
its `data/` holds roughly 350 landlords' phone numbers, which git history would make
permanent.
"""
from __future__ import annotations

import json
import os
import re
import sys

# Built from fragments so this file does not match its own patterns when scanned.
_KEY = "key"
_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE " + _KEY.upper() + "-----"),
     "a private key block"),
    # LENGTHS ARE MINIMUMS, NOT EXACT. Pinning {35} made both of these miss a planted test
    # key that was one character off — the failure mode a scanner cannot afford, because it
    # then reports clean and you believe it.
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
     "a Google API key (GEMINI_API_KEY shape)"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}"),
     "a Telegram bot token"),
    (re.compile(r'"private_' + _KEY + r'_id"\s*:'),
     "a Google service-account JSON"),
    (re.compile(r"[A-Za-z0-9_\-]+@[a-z0-9\-]+\.iam\.gserviceaccount\.com"),
     "a service-account client_email"),
    # A literal assignment, not a lookup: os.environ / getenv / a placeholder are fine.
    #
    # NO \b BEFORE THE KEYWORD. Underscore is a word character, so `\bapi` cannot match
    # inside GEMINI_API_KEY and `\btoken` cannot match inside TELEGRAM_BOT_TOKEN — the two
    # names this project actually uses. Both planted secrets sailed straight through until
    # this was found by TESTING the scanner rather than trusting it.
    (re.compile(r"""(?i)[a-z0-9_]*(api[_-]?""" + _KEY + r"""|token|secret|password)\s*[:=]\s*"""
                r"""['"](?!.*(?:YOUR|EXAMPLE|CHANGEME|<|\{|\$))[^'"\s]{16,}['"]"""),
     "a hard-coded credential"),
]

_CONFLICT = re.compile(r"^(<{7}|>{7}|={7})(\s|$)", re.M)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def secrets(paths: list[str]) -> int:
    bad = 0
    me = os.path.basename(__file__)
    for p in paths:
        if os.path.basename(p) == me:
            continue                       # the scanner names the shapes it looks for
        text = _read(p)
        if text is None:
            continue
        for rx, what in _PATTERNS:
            m = rx.search(text)
            if m:
                line = text[:m.start()].count("\n") + 1
                print(f"{p}:{line}: looks like {what}")
                bad += 1
                break
    if bad:
        print("\nThis repo is PUBLIC and git history is permanent. Move the value into "
              ".env (git-ignored) and read it with os.environ.")
    return bad


def large(paths: list[str], maxkb: int = 2048) -> int:
    bad = 0
    for p in paths:
        try:
            kb = os.path.getsize(p) / 1024
        except OSError:
            continue
        if kb > maxkb:
            print(f"{p}: {kb:.0f} KB exceeds {maxkb} KB")
            bad += 1
    return bad


def conflict(paths: list[str]) -> int:
    bad = 0
    for p in paths:
        text = _read(p)
        if text and _CONFLICT.search(text):
            print(f"{p}: contains a merge-conflict marker")
            bad += 1
    return bad


def parse(paths: list[str]) -> int:
    """JSON validity. YAML is checked only if PyYAML is importable — it is not a project
    dependency, and a missing optional library must not block a commit."""
    bad = 0
    for p in paths:
        text = _read(p)
        if text is None:
            continue
        if p.endswith(".json"):
            try:
                json.loads(text)
            except Exception as exc:
                print(f"{p}: invalid JSON — {exc}")
                bad += 1
        elif p.endswith((".yaml", ".yml")):
            try:
                import yaml
            except ImportError:
                continue
            try:
                yaml.safe_load(text)
            except Exception as exc:
                print(f"{p}: invalid YAML — {exc}")
                bad += 1
    return bad


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    mode, args = argv[1], argv[2:]
    maxkb = 2048
    if "--maxkb" in args:
        i = args.index("--maxkb")
        maxkb = int(args[i + 1])
        args = args[:i] + args[i + 2:]
    fns = {"secrets": secrets, "conflict": conflict, "parse": parse}
    if mode == "large":
        return 1 if large(args, maxkb) else 0
    if mode not in fns:
        print(f"unknown check: {mode}")
        return 2
    return 1 if fns[mode](args) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
