"""stats.py — the introspection the tuning workflow is gated on.

`run reliability` is the number that decides whether a latency change helped, so it is
the one number that must not be able to flatter itself.
"""
import config
import geocode
import stats
import storage


def _unmapped(monkeypatch, rows, unplaceable=(), bearings=()) -> str:
    monkeypatch.setattr(storage, "unknown_locations", lambda *a, **k: rows)
    monkeypatch.setattr(geocode, "still_unplaceable", lambda n: n in unplaceable)
    monkeypatch.setattr(geocode, "names_only_a_landmark", lambda n: n in bearings)
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    stats._unmapped()
    return "\n".join(out)


def test_a_location_that_resolves_today_is_not_recommended(monkeypatch):
    """`unknown_locations` logs what failed ONCE and nothing ever expires an entry, so
    the section recommended pinning names that resolve perfectly well now. Measured
    2026-08-12: the top entry was `שכונת הפארק` (5 hits) — `geocode_detailed` answers it
    from the static table and those posts are already dropped RED — and 98 of the 182
    logged names were in the same state. Acting on the advice would have added a
    STATIC_TABLE line that changed nothing."""
    out = _unmapped(monkeypatch,
                    [("שכונת הפארק", 5, "2026-07-23 10:34:28"),
                     ("ארניה אוסוולדו", 2, "2026-08-09 09:23:55")],
                    unplaceable={"ארניה אוסוולדו"})
    assert "שכונת הפארק" not in out, out
    assert "ארניה אוסוולדו" in out, out


def test_a_bearing_off_a_landmark_is_never_recommended_for_pinning(monkeypatch):
    """Unplaceable is not the same as pinnable. `אוניברסיטה` and `ליד האוניברסיטה` cannot
    be placed BY DESIGN — "near X" is a bearing, not an address, and nobody rents on the
    campus — so pinning them would rebuild by hand the wrong dots `no_housing_here` and
    `_is_bare_proximity` exist to refuse. They were the 5 most frequent survivors of the
    re-check, i.e. the whole top of a list headed "pin these"."""
    out = _unmapped(monkeypatch,
                    [("אוניברסיטה", 3, "2026-08-11 15:23:17"),
                     ("הרקנוס 37", 1, "2026-08-10 10:00:00")],
                    unplaceable={"אוניברסיטה", "הרקנוס 37"},
                    bearings={"אוניברסיטה"})
    assert "אוניברסיטה" not in out, out
    assert "הרקנוס 37" in out, out
    assert "refused BY DESIGN" in out, out


def test_both_filters_report_what_they_removed(monkeypatch):
    """A filter you cannot audit is one that hides a real gap the day it goes wrong. The
    header carries n-of-N and each excluded group prints its own count, so the section
    can never quietly shrink to nothing."""
    out = _unmapped(monkeypatch,
                    [("שכונת הפארק", 5, "2026-07-23 10:34:28"),
                     ("אוניברסיטה", 3, "2026-08-11 15:23:17"),
                     ("הרקנוס 37", 1, "2026-08-10 10:00:00")],
                    unplaceable={"אוניברסיטה", "הרקנוס 37"},
                    bearings={"אוניברסיטה"})
    assert "1 of 3 logged" in out, out
    assert "(1 logged name(s) resolve today" in out, out
    assert "(1 are a bearing off a landmark" in out, out


def test_the_last_seen_date_is_shown(monkeypatch):
    """The window stays at the full history on purpose (narrowing it is a placebo — the
    table spans 23 days), so the age of an entry has to be visible per row instead."""
    out = _unmapped(monkeypatch, [("הרקנוס 37", 1, "2026-08-10 10:00:00")],
                    unplaceable={"הרקנוס 37"})
    assert "last seen 2026-08-10" in out, out


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


def _runs_log(tmp_path, monkeypatch, search_log: str, runs_log: str) -> str:
    """`_osrm_degraded` reads scraper_runs.log while the rest of the row reads
    search_log.txt — the OSRM warning is only ever written to the former."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "search_log.txt").write_text(search_log, encoding="utf-8")
    (tmp_path / "scraper_runs.log").write_text(runs_log, encoding="utf-8")
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    stats._run_reliability()
    return "\n".join(out)


def test_a_run_with_osrm_down_is_reported_as_degraded(tmp_path, monkeypatch):
    """A run with OSRM down does not fail or warn — it quietly writes worse numbers, and
    the AMBER boundary IS a walk time. 14 of 88 runs all-time, and it was only ever
    visible by grepping the log for it."""
    import scraper
    monkeypatch.setattr(config, "SCRAPER_RUNS_PER_DAY", 6)
    monkeypatch.setattr(scraper, "run_in_progress", lambda: False)
    search = "\n".join([
        "2026-08-05 08:00:02  END    LIVE  100s  posts=5",
        "2026-08-05 10:00:02  END    LIVE  100s  posts=5",
    ])
    # The warning line carries NO date; it is attributed to the timestamp above it.
    runs = "\n".join([
        "geocode misses: 0  ·  ⚠️ OSRM DOWN (used straight-line walk estimate)",
        "2026-08-05 08:00:02  END    LIVE  100s  posts=5",
        "2026-08-05 10:00:02  END    LIVE  100s  posts=5",
    ])
    out = _runs_log(tmp_path, monkeypatch, search, runs)
    assert "STRAIGHT-LINE estimate" in out
    assert "1/2" in out and "50%" in out


def test_no_degraded_row_when_osrm_was_up_all_week(tmp_path, monkeypatch):
    """Silence must mean healthy. A row that prints 0/N every day is one you stop
    reading, and then it cannot tell you anything when it matters."""
    import scraper
    monkeypatch.setattr(config, "SCRAPER_RUNS_PER_DAY", 6)
    monkeypatch.setattr(scraper, "run_in_progress", lambda: False)
    line = "2026-08-05 08:00:02  END    LIVE  100s  posts=5"
    out = _runs_log(tmp_path, monkeypatch, line, line)
    assert "STRAIGHT-LINE" not in out
