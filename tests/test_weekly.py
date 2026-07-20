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


def test_in_progress_run_is_not_flagged():
    now = datetime(2026, 7, 20, 16, 30, 0)
    lines = ["2026-07-20 16:15:00  START  LIVE  groups=17/17"]   # only 15 min old
    assert w._summarize(lines, now, days=7)["dangling"] == 0
