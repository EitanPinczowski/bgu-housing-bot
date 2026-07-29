"""Heartbeat + wedged-run recovery.

Context: on 2026-07-27 a run worked for ~7h (local-LLM fallback) and then wedged for
~30h, silently blocking every later run. The guard therefore keys on PROGRESS, never on
elapsed time — a slow-but-working run must be left completely alone.
"""
import time

import config
import scraper


def _hb(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "_HEARTBEAT_PATH", tmp_path / "scraper.heartbeat")


def test_beat_and_age(tmp_path, monkeypatch):
    _hb(tmp_path, monkeypatch)
    assert scraper.heartbeat_age() is None            # nothing recorded yet
    scraper.beat("working")
    age = scraper.heartbeat_age()
    assert age is not None and age < 5
    assert scraper.heartbeat_pid() == __import__("os").getpid()
    assert scraper.is_wedged() is False


def test_wedged_only_after_the_stall_threshold(tmp_path, monkeypatch):
    _hb(tmp_path, monkeypatch)
    path = tmp_path / "scraper.heartbeat"
    # a run that reported progress 5 minutes ago is healthy...
    path.write_text(f"4242 {time.time() - 5 * 60:.0f} x", encoding="utf-8")
    assert scraper.is_wedged() is False
    # ...and one silent for longer than STALL_MINUTES is wedged
    path.write_text(f"4242 {time.time() - (config.STALL_MINUTES + 1) * 60:.0f} x",
                    encoding="utf-8")
    assert scraper.is_wedged() is True


def test_slow_but_progressing_run_is_never_killed(tmp_path, monkeypatch):
    """THE regression this design exists to prevent: a local-LLM run legitimately ran
    268 minutes. It must NOT be treated as wedged just for being long."""
    _hb(tmp_path, monkeypatch)
    path = tmp_path / "scraper.heartbeat"
    # started ~4.5h ago but reported progress 30 seconds ago
    path.write_text(f"4242 {time.time() - 30:.0f} post 900", encoding="utf-8")
    assert scraper.is_wedged() is False

    killed = []
    monkeypatch.setattr(scraper.subprocess if hasattr(scraper, "subprocess") else scraper,
                        "run", lambda *a, **k: killed.append(a), raising=False)
    assert scraper._clear_wedged_holder() is False     # refuses to touch a live worker
    assert killed == []


def test_clear_wedged_holder_kills_a_stalled_run(tmp_path, monkeypatch):
    _hb(tmp_path, monkeypatch)
    (tmp_path / "scraper.heartbeat").write_text(
        f"4242 {time.time() - (config.STALL_MINUTES + 5) * 60:.0f} stuck", encoding="utf-8")
    calls = {}
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **k: calls.update(cmd=cmd) or None)
    assert scraper._clear_wedged_holder() is True
    assert calls["cmd"][:2] == ["taskkill", "/PID"] and "4242" in calls["cmd"]
    assert "/T" in calls["cmd"]                        # kills its Chromium children too
