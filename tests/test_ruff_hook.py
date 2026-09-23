"""The PostToolUse ruff hook: a linter that cannot run must not block work.

In a fresh cloud session ruff was not installed, `python -m ruff` exited 1 with
"No module named ruff", and the hook reported that as a problem IN THE FILE — so every
.py written was blocked for a finding that did not exist. Real findings must still block.
"""
import importlib.util
import io
import json
import pathlib
import subprocess

_HOOK = pathlib.Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "ruff_edited.py"


def _hook():
    spec = importlib.util.spec_from_file_location("ruff_hook_under_test", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(mod, monkeypatch, path, *, rc, stdout="", stderr=""):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_input": {"file_path": str(path)}})))
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return mod.main(), calls


def test_ruff_not_installed_is_allowed_without_running(tmp_path, monkeypatch, capsys):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    mod = _hook()
    monkeypatch.setattr(mod, "_ruff_installed", lambda: False)
    code, calls = _run(mod, monkeypatch, f, rc=1, stderr="No module named ruff")
    assert code == mod.ALLOW
    assert calls == []
    assert "not installed" in capsys.readouterr().err


def test_no_module_named_ruff_from_the_run_is_allowed(tmp_path, monkeypatch, capsys):
    """Backstop: the spec check passed but the interpreter still could not load ruff."""
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    mod = _hook()
    monkeypatch.setattr(mod, "_ruff_installed", lambda: True)
    code, _ = _run(mod, monkeypatch, f, rc=1, stderr="/usr/bin/python: No module named ruff\n")
    assert code == mod.ALLOW
    err = capsys.readouterr().err
    assert "not installed" in err
    assert "found problems" not in err


def test_a_real_finding_still_blocks(tmp_path, monkeypatch, capsys):
    f = tmp_path / "x.py"
    f.write_text("import os\n")
    mod = _hook()
    monkeypatch.setattr(mod, "_ruff_installed", lambda: True)
    code, _ = _run(mod, monkeypatch, f, rc=1, stdout="x.py:1:8: F401 `os` imported but unused")
    assert code == mod.BLOCK
    assert "found problems in x.py" in capsys.readouterr().err
