"""The Bash guard's rules, in both directions.

It had no tests, and it has produced **seven false positives**, every one from a pattern
that matched a WORD where a COMMAND was meant: `pytest\\b` caught `pip install
pytest-xdist`, a commit message quoting `replay.py --apply` blocked its own commit,
`| tail` copied onto the summary rule blocked `seed_anchors.py --dry-run | head`, and on
2026-08-18 `grep -l "pytest" .github/workflows/*` was blocked twice.

A false positive is not a harmless inconvenience here: a guard that blocks correct work is
a guard that gets switched off with BGU_SKIP_GUARD=1, and then it protects nothing. So
every rule needs both lists — what it must catch, and what it must let through.
"""
import importlib.util
import pathlib
import re

import pytest

_GUARD = pathlib.Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "guard.py"


def _guard():
    spec = importlib.util.spec_from_file_location("guard_under_test", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rule(label):
    g = _guard()
    return g, next(rx for lbl, rx, _ in g.RULES if lbl == label)


def _matches(g, rx, cmd):
    return bool(re.search(rx, g.executable_part(cmd)))


@pytest.mark.parametrize("cmd", [
    "python -m pytest -q | tail",
    "pytest -q | tail -5",
    "cd x && pytest | head",
    "python -m pytest --randomly-seed=1 | grep FAIL",
    "echo hi; pytest -q | tail",
])
def test_a_piped_pytest_is_still_blocked(cmd):
    """The rule protects the EXIT CODE: any pipe discards it, so a failing suite reads as
    a passing one. Narrowing it to command position must not weaken this."""
    g, rx = _rule("pytest exit code")
    assert _matches(g, rx, cmd), f"should be blocked: {cmd}"


@pytest.mark.parametrize("cmd", [
    'grep -l "pytest\\|ruff" .github/workflows/*',   # blocked twice on 2026-08-18
    'grep -n "pytest" -A3 CLAUDE.md | head',
    "pip install pytest-xdist | tail",               # the original false positive
    'echo "never pipe pytest" | cat',
    "git log --grep=pytest | head",
    "python -m pytest -q",                           # no pipe: nothing to protect
])
def test_merely_mentioning_pytest_is_not_running_it(cmd):
    """Seven false positives came from matching text instead of an invocation. Reading
    ABOUT a command is not running it, and blocking that is how a guard gets disabled."""
    g, rx = _rule("pytest exit code")
    assert not _matches(g, rx, cmd), f"false positive: {cmd}"


def test_the_destructive_rules_still_match_inside_quotes():
    """Deliberate asymmetry, and the reason this fix was scoped to ONE rule.
    `executable_part` does not strip quoted strings, because a false negative on
    `docker rmi "osrm_bgu"` costs a multi-GB rebuild from the Israel PBF while a false
    positive on the pytest rule costs a re-run. Loosening the docker rule the same way
    would trade the cheap error for the expensive one."""
    g, rx = _rule("OSRM container")
    assert _matches(g, rx, 'docker rm -f "osrm_bgu"')
    assert _matches(g, rx, "docker image prune -a")


def test_a_commit_message_quoting_a_command_is_not_that_command():
    """`executable_part` strips heredoc bodies and -m messages — the two carriers of prose.
    A commit recording this guard quoted `replay.py --apply` and blocked itself."""
    g, rx = _rule("replay --apply")
    msg = "git commit -m 'never run replay.py --apply during a scrape'"
    assert not _matches(g, rx, msg)
    assert _matches(g, rx, "python replay.py --apply")
