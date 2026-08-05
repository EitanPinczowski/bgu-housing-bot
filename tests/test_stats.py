"""stats.py — the introspection the tuning workflow is gated on.

`run reliability` is the number that decides whether a latency change helped, so it is
the one number that must not be able to flatter itself.
"""
import config
import stats


def _log(tmp_path, monkeypatch, text: str) -> str:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "search_log.txt").write_text(text, encoding="utf-8")
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    stats._run_reliability()
    return "\n".join(out)


def test_a_skipped_run_is_not_a_completed_run(tmp_path, monkeypatch, capsys):
    """It counted END|SKIP together, so the metric read HEALTHIEST exactly when runs were
    being lost: 2026-08-03 reported 11 runs / 119% of a 6/day target while 5 of them were
    `lock held` and only 4 scrapes actually ran."""
    monkeypatch.setattr(config, "SCRAPER_RUNS_PER_DAY", 6)
    text = "\n".join([
        "2026-08-03 08:00:02  END    LIVE  100s  posts=5",
        "2026-08-03 10:00:02  SKIP   another scraper session is running (lock held)",
        "2026-08-03 12:00:02  SKIP   another scraper session is running (lock held)",
        "2026-08-03 14:00:02  SKIP   another scraper session is running (lock held)",
        "2026-08-03 16:00:02  SKIP   random human-like skip",
        "2026-08-03 18:00:02  END    LIVE-HOT  50s  posts=2",
    ])
    out = _log(tmp_path, monkeypatch, text)
    assert "1/6 full runs = 17%" in out, out
    assert "3 slot(s) LOST" in out, out          # the lock-held ones, named as losses
    assert "1 skipped by design" in out, out     # the random one is not a fault
    assert "+1 hot" in out, out                  # a hot pass is not a full run


def test_the_random_skip_is_not_reported_as_a_fault(tmp_path, monkeypatch):
    """~1 in 8 runs is skipped on purpose so the schedule isn't clockwork. Counting that
    as a failure would make a healthy day look broken and train the user to ignore it."""
    monkeypatch.setattr(config, "SCRAPER_RUNS_PER_DAY", 1)
    out = _log(tmp_path, monkeypatch, "\n".join([
        "2026-08-03 08:00:02  END    LIVE  100s  posts=5",
        "2026-08-03 10:00:02  SKIP   random human-like skip",
    ]))
    assert "0 slot(s) LOST" not in out and "LOST" not in out
    assert "1 skipped by design" in out, out
