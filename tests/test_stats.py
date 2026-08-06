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


def test_a_run_that_starts_and_never_ends_is_counted(tmp_path, monkeypatch):
    """It is not a SKIP (it took the slot) and not an END (it produced nothing), so a
    metric built on those two calls the day merely quiet. The 14:00 full run on
    2026-08-05 did exactly this. Measured over 7 days: 7 of them."""
    import scraper
    monkeypatch.setattr(config, "SCRAPER_RUNS_PER_DAY", 6)
    monkeypatch.setattr(scraper, "run_in_progress", lambda: False)
    out = _log(tmp_path, monkeypatch, "\n".join([
        "2026-08-05 08:00:02  START  LIVE  groups=15/15",
        "2026-08-05 09:00:02  END    LIVE  100s  posts=5",
        "2026-08-05 14:00:03  START  LIVE  groups=15/15",     # never ends
    ]))
    assert "1 run(s) STARTED and never finished" in out, out


def test_the_run_in_flight_right_now_is_not_called_a_crash(tmp_path, monkeypatch):
    """A healthy scrape mid-run has a START and no END yet; accusing it would make the
    row cry on every machine that is currently working."""
    import scraper
    monkeypatch.setattr(config, "SCRAPER_RUNS_PER_DAY", 6)
    monkeypatch.setattr(scraper, "run_in_progress", lambda: True)
    out = _log(tmp_path, monkeypatch, "2026-08-05 15:00:03  START  LIVE  groups=3/15\n"
                                      "2026-08-05 08:00:02  END    LIVE  100s  posts=5")
    assert "STARTED and never finished" not in out, out


def test_a_watchdog_abort_is_its_own_category(tmp_path, monkeypatch):
    """An abort is deliberate and frees the slot, so it must not be reported as one
    more anonymous crash — and must not be counted twice, since `os._exit` also leaves
    it with a START and no END."""
    import scraper
    monkeypatch.setattr(config, "SCRAPER_RUNS_PER_DAY", 6)
    monkeypatch.setattr(scraper, "run_in_progress", lambda: False)
    out = _log(tmp_path, monkeypatch, "\n".join([
        "2026-08-05 08:00:02  START  LIVE  groups=15/15",
        "2026-08-05 10:00:02  ABORT  run has taken 150 min (limit 120)",
        "2026-08-05 12:00:02  START  LIVE  groups=15/15",
        "2026-08-05 12:30:02  END    LIVE  100s  posts=5",
    ]))
    assert "1 run(s) ABORTED by the watchdog" in out, out
    assert "STARTED and never finished" not in out, "counted twice"


def test_the_longest_run_is_reported_and_flagged_near_the_ceiling(tmp_path, monkeypatch):
    """MAX_RUN_MINUTES kills a run that crawls, but a legitimate full run measured 88
    min against a 120 limit. Surface the squeeze before a healthy run is killed."""
    import scraper
    monkeypatch.setattr(config, "SCRAPER_RUNS_PER_DAY", 6)
    monkeypatch.setattr(config, "MAX_RUN_MINUTES", 120)
    monkeypatch.setattr(scraper, "run_in_progress", lambda: False)
    out = _log(tmp_path, monkeypatch, "\n".join([
        "2026-08-05 08:00:02  END    LIVE  1800s  posts=50",     # 30 min
        "2026-08-05 18:00:02  END    LIVE  5286s  posts=137",    # 88 min
    ]))
    assert "longest completed run 88 min (ceiling 120)" in out, out
    assert "close to the ceiling" in out


def test_a_comfortable_run_is_not_flagged(tmp_path, monkeypatch):
    import scraper
    monkeypatch.setattr(config, "SCRAPER_RUNS_PER_DAY", 6)
    monkeypatch.setattr(config, "MAX_RUN_MINUTES", 120)
    monkeypatch.setattr(scraper, "run_in_progress", lambda: False)
    out = _log(tmp_path, monkeypatch,
               "2026-08-05 08:00:02  END    LIVE  1800s  posts=50")
    assert "longest completed run 30 min" in out
    assert "close to the ceiling" not in out


def test_a_run_past_the_ceiling_is_not_called_close_to_it(tmp_path, monkeypatch):
    """The window still holds the 509-minute run that slept through the night. That is
    what the ceiling is FOR — describing it as "close to" the limit is nonsense."""
    import scraper
    monkeypatch.setattr(config, "SCRAPER_RUNS_PER_DAY", 6)
    monkeypatch.setattr(config, "MAX_RUN_MINUTES", 120)
    monkeypatch.setattr(scraper, "run_in_progress", lambda: False)
    out = _log(tmp_path, monkeypatch,
               "2026-08-05 09:15:22  END    LIVE-HOT  30543s  posts=154")
    assert "longest completed run 509 min" in out
    assert "would now be ABORTED" in out
    assert "close to the ceiling" not in out
