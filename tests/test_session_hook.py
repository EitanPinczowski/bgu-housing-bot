"""The SessionStart banner must measure the checkout the BOT runs from.

`data/` is git-ignored, so a linked git worktree gets its own empty one. Every probe in
the hook derives its path from `config`, which is rooted at the checkout — so a session
opened in a worktree reported `listings: 0 (0 MATCH)` for three days while production held
598. An unlabelled zero is worse than no number at all: the hook exists precisely because
measured state is supposed to beat a hand-typed summary.
"""
import importlib.util
import os
import sys
from pathlib import Path

_HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "session_start.py"


def _load():
    """Import the hook by path — it lives outside the package and is not importable."""
    spec = importlib.util.spec_from_file_location("session_start_hook", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_normal_checkout_is_not_retargeted(monkeypatch):
    """`_main_checkout` must return None outside a worktree, or the hook would rewrite
    config's paths on every ordinary session for no reason."""
    hook = _load()
    root = str(_HOOK.parent.parent.parent)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", root)
    assert hook._main_checkout() is None


def test_a_worktree_resolves_to_the_main_checkout(monkeypatch):
    """`git rev-parse --git-common-dir` is the discriminator: in a linked worktree it
    points at the MAIN repo's `.git`, while `--git-dir` points inside
    `.git/worktrees/<name>`. Faked here so the test needs no real worktree."""
    hook = _load()
    here = os.path.join("C:", os.sep, "repo", ".claude", "worktrees", "wt")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", here)

    class _R:
        returncode = 0
        stdout = os.path.join("C:", os.sep, "repo", ".git")

    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: _R())
    assert hook._main_checkout() == os.path.join("C:", os.sep, "repo")


def test_the_cache_is_written_locally_even_when_retargeted(monkeypatch, tmp_path):
    """A worktree session must NOT write `.claude_state.json` into the production data
    directory the live bot is reading. The probes retarget; the cache write does not."""
    hook = _load()
    import config
    local, main = tmp_path / "wt_data", tmp_path / "main_data"
    local.mkdir()
    main.mkdir()
    monkeypatch.setattr(hook, "_LOCAL_DATA_DIR", local)
    monkeypatch.setattr(config, "DATA_DIR", main)
    hook._write_cache({"listings": "598 (323 MATCH)"})
    assert (local / ".claude_state.json").exists(), "cache must land in THIS checkout"
    assert not (main / ".claude_state.json").exists(), "must not touch production data/"


def test_retarget_points_config_at_the_main_checkout(monkeypatch, tmp_path):
    """The probes read `config.DB_PATH`; retargeting is what makes them measure the real
    database instead of the worktree's empty one.

    THE SETATTRS BELOW ARE LOAD-BEARING. `_retarget` mutates `config` for the life of the
    process — which is correct in the hook, a short-lived process of its own, and lethal
    in a test run. Without them this test left `DB_PATH` pointing at a deleted `tmp_path`
    and **23 unrelated tests failed** with `unable to open database file`, in whichever
    files happened to run afterwards. `monkeypatch` records the value at setattr time and
    restores it at teardown even though `_retarget` overwrites it in between; same shape
    as conftest's `_median_gap` and `_dead_mirrors` guards."""
    hook = _load()
    import config
    monkeypatch.setattr(config, "DATA_DIR", config.DATA_DIR)
    monkeypatch.setattr(config, "DB_PATH", config.DB_PATH)
    hook._retarget(str(tmp_path))
    assert config.DATA_DIR == tmp_path / "data"
    assert config.DB_PATH == tmp_path / "data" / "listings.sqlite"


def test_the_hook_never_stops_a_session(monkeypatch):
    """Every probe is individually wrapped, and so is the retargeting: this runs on every
    session start, so it must never be the reason one cannot begin."""
    hook = _load()
    monkeypatch.setattr(hook, "_main_checkout",
                        lambda: (_ for _ in ()).throw(RuntimeError("git exploded")))
    for name in ("_scrape", "_osrm", "_listings", "_quota", "_last_run", "_notes_drift"):
        monkeypatch.setattr(hook, name,
                            lambda: (_ for _ in ()).throw(RuntimeError("probe exploded")))
    monkeypatch.setattr(hook, "_write_cache", lambda values: None)
    assert hook.main() == 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
