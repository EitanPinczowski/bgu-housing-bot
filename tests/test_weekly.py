"""weekly_digest._summarize: parse the search log into weekly counts + crash flag."""
from datetime import datetime

import weekly_digest as w


def test_summarize_counts_and_dangling():
    now = datetime(2026, 7, 20, 18, 0, 0)
    lines = [
        "2026-07-20 08:00:00  START  LIVE  groups=17/17",
        "2026-07-20 08:45:00  END    LIVE  900s  posts=100 match=3 needs=9 groups_ok=17/17",
        "2026-07-20 12:00:00  SKIP   random human-like skip",
        "2026-07-20 14:00:00  START  LIVE  groups=17/17",
        "2026-07-20 14:40:00  END    BLOCKED  10s  posts=0 match=0 needs=0 block=/checkpoint",
        "2026-07-20 15:00:00  START  LIVE  groups=17/17",     # dangling: no END, 3h before now
        "2020-01-01 00:00:00  END    LIVE  1s  posts=999 match=9 needs=9",  # older than cutoff
    ]
    s = w._summarize(lines, now, days=7)
    assert s["runs"] == 1
    assert s["skipped"] == 1
    assert s["blocked"] == 1
    assert (s["posts"], s["matches"], s["needs"]) == (100, 3, 9)
    assert s["dangling"] == 1
    # funnel fields absent on these older lines -> stay 0
    assert (s["read"], s["age_skip"], s["seen_skip"]) == (0, 0, 0)


def test_summarize_parses_funnel_fields():
    now = datetime(2026, 7, 20, 18, 0, 0)
    lines = [
        "2026-07-20 08:00:00  START  LIVE  groups=14/14",
        "2026-07-20 08:12:00  END    LIVE  700s  posts=40 match=2 needs=6 "
        "read=180 age_skip=90 seen_skip=50 groups_ok=14/14",
    ]
    s = w._summarize(lines, now, days=7)
    assert (s["read"], s["age_skip"], s["seen_skip"]) == (180, 90, 50)
    assert (s["posts"], s["matches"], s["needs"]) == (40, 2, 6)


def test_in_progress_run_is_not_flagged():
    now = datetime(2026, 7, 20, 16, 30, 0)
    lines = ["2026-07-20 16:15:00  START  LIVE  groups=17/17"]   # only 15 min old
    assert w._summarize(lines, now, days=7)["dangling"] == 0


# --- the unmapped line records what failed ONCE, and nothing re-checked it -----------
#
# Mirrors tests/test_stats.py, which fixed the same bug in the same shape. Measured
# 2026-08-12 at this module's default `days=7`: of 30 logged names 7 resolve today and 7
# are bearings, and since the list is sorted by frequency the dead ones crowd the top —
# 5 of the 8 names this line printed were one or the other.

def _build(monkeypatch, temp_db, rows, unplaceable=(), bearings=()) -> str:
    import geocode
    import storage
    storage._conn().close()                       # create the schema build() counts from
    monkeypatch.setattr(storage, "unknown_locations", lambda *a, **k: rows)
    monkeypatch.setattr(geocode, "still_unplaceable", lambda n: n in unplaceable)
    monkeypatch.setattr(geocode, "names_only_a_landmark", lambda n: n in bearings)
    monkeypatch.setattr(w, "_LOG", temp_db.parent / "missing_search_log.txt")
    return w.build(days=7)


def test_a_resolved_or_bearing_name_is_not_listed(monkeypatch, temp_db):
    out = _build(monkeypatch, temp_db,
                 [("שכונת הפארק", 5, "2026-07-23"), ("אוניברסיטה", 3, "2026-08-11"),
                  ("הרקנוס 37", 1, "2026-08-10")],
                 unplaceable={"אוניברסיטה", "הרקנוס 37"}, bearings={"אוניברסיטה"})
    assert "הרקנוס 37" in out, out
    assert "שכונת הפארק" not in out and "אוניברסיטה" not in out, out
    assert "1/3" in out, out                      # the header carries n-of-N


def test_both_exclusions_stay_visible_even_when_nothing_survives(monkeypatch, temp_db):
    """A line that quietly shrinks to nothing is how a real gap goes unnoticed. This
    report only wastes a line (unlike `/unknowns`, which puts a 📌 on each row), but a
    line that names a fixed problem is what teaches you to stop reading the summary."""
    out = _build(monkeypatch, temp_db,
                 [("שכונת הפארק", 5, "2026-07-23"), ("אוניברסיטה", 3, "2026-08-11")],
                 unplaceable={"אוניברסיטה"}, bearings={"אוניברסיטה"})
    assert "מקומות שלא מופו" not in out, out      # no heading over an empty list
    assert "כבר נפתרו מאז" in out and "לא ניתנים לקיבוע לעולם" in out, out


def test_build_returns_raw_text_for_main_to_escape(monkeypatch, temp_db):
    """`main` does `notifier._esc(build(days))` on the WHOLE string, so `build` must not
    pre-escape — which is why `dm_digest._excluded_lines` returns raw and each caller
    applies its own convention."""
    out = _build(monkeypatch, temp_db, [("שכונת הפארק", 5, "2026-07-23")])
    assert "\\" not in out, out
