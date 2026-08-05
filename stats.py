"""
Funnel stats from the local post archive — what the filters are actually doing.
No browser, no network.

    python stats.py

Shows how many archived posts (those that reached the LLM) landed in each verdict
(MATCH / NEEDS_DATA / DROP / NOT_AD), WHY posts were dropped, and store totals.
The archive fills as the scraper runs; see replay.py to re-test against it.
"""
from __future__ import annotations
import os
import re
import sqlite3
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
try:
    sys.stdout.reconfigure(encoding="utf-8")   # Hebrew reasons/addresses
except Exception:
    pass

import config
import storage


def main() -> None:
    vc = storage.verdict_counts()
    total = sum(vc.values())
    print(f"=== archive: {total} posts (reached the LLM) ===")
    for v in ("MATCH", "NEEDS_DATA", "DROP", "NOT_AD"):
        if vc.get(v):
            pct = round(100 * vc[v] / total) if total else 0
            print(f"  {v:11} {vc[v]:4}  ({pct}%)")

    drops = storage.drop_reason_counts()
    if drops:
        print("--- why dropped ---")
        for reason, c in drops:
            print(f"  {c:4}  {reason}")

    with sqlite3.connect(config.DB_PATH) as con:
        listings = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        matches = con.execute("SELECT COUNT(*) FROM listings WHERE status='MATCH'").fetchone()[0]
        votes = con.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
    print("--- store ---")
    print(f"  listings: {listings} ({matches} MATCH)   votes: {votes}")

    gy = storage.group_yield()
    if gy:
        print("--- per-group yield (match | needs | drop | total) — drop dead groups ---")
        for g, tot, m, n, d, _na in gy:
            gid = g.rstrip("/").split("/")[-1].split("?")[0]
            flag = "   ← 0 matches, candidate to drop from FB_GROUPS" if m == 0 else ""
            print(f"  {gid:>18}   {m:>3} | {n:>3} | {d:>3} | {tot:>3}{flag}")

    uk = storage.unknown_locations(days=3650)
    if uk:
        print("--- top unmapped locations (pin these) ---")
        for loc, cnt, _ in uk[:8]:
            print(f"  {cnt:3}  {loc}")

    _detection_lag()
    _run_reliability()
    _alert_gate()


def _alert_gate() -> None:
    """Where MIN_ALERT_SCORE cuts the MATCH score distribution, and what the votes say.

    A bot that stops pinging is one you stop trusting; a bot that pings everything is one
    you mute. Neither is visible from the threshold alone — you have to see the shape it
    cuts. Measured 2026-08-05: median MATCH 73 against a gate of 75, letting the top 45%
    through at ~1-5 alerts on a normal day, and the curve is SMOOTH across 75 (no valley
    to snap to). The only evidence that could justify a different number is which flats
    the group actually stars, and that was n=3 — so the row prints its own n and the
    threshold stays put until the votes can carry an opinion."""
    with sqlite3.connect(config.DB_PATH) as con:
        scores = [r[0] for r in con.execute(
            "SELECT score FROM listings WHERE status='MATCH' AND score IS NOT NULL")]
        voted = con.execute(
            "SELECT m.mark, l.score FROM marks m JOIN listings l ON l.dedup_key=m.dedup_key "
            "WHERE l.score IS NOT NULL").fetchall()
    if not scores:
        return
    gate = config.MIN_ALERT_SCORE
    scores.sort()
    over = sum(1 for s in scores if s >= gate)
    print(f"--- alert gate (MIN_ALERT_SCORE={gate}) ---")
    print(f"  MATCH n={len(scores)}   median {scores[len(scores)//2]}   "
          f"{over} over the gate ({round(100*over/len(scores))}%)")
    step = 10
    for lo in range(0, 101, step):
        n = sum(1 for s in scores if lo <= s < lo + step)
        mark = " <- gate" if lo <= gate < lo + step else ""
        print(f"  {lo:3}-{lo+step-1:3} {'#' * min(n, 60)} {n}{mark}")
    kept = [s for k, s in voted if k == "saved"]
    binned = [s for k, s in voted if k == "dismissed"]
    print(f"  votes: {len(kept)} saved, {len(binned)} dismissed"
          + ("" if len(voted) >= 20 else "   ← too few to move the gate on"))
    if kept and len(voted) >= 20 and min(kept) < gate:
        print(f"    ⚠ a saved listing scored {min(kept)}, below the gate — it would not "
              f"have been alerted")


def _detection_lag() -> None:
    """How long a listing sits on Facebook before we see it.

    The `--hot` pass exists to cut this to ~30-40 min and that had never actually been
    measured. Every figure is printed WITH its n: the first attempt at this had n=6,
    which is worth nothing, and a lag number quoted without its sample size is how you
    end up believing a feature works."""
    with sqlite3.connect(config.DB_PATH) as con:
        rows = con.execute("SELECT posted_at, first_seen FROM posts "
                           "WHERE posted_at IS NOT NULL AND first_seen IS NOT NULL").fetchall()
    lags = []
    discarded = 0
    for posted, seen in rows:
        try:
            d = (datetime.strptime(seen, "%Y-%m-%d %H:%M:%S")
                 - datetime.strptime(posted, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
        except (TypeError, ValueError):
            continue
        if 0 <= d < 60 * 48:                 # ignore clock skew and absurd outliers
            lags.append(d)
        else:
            discarded += 1
    print("--- time to detect (post published -> we saw it) ---")
    # SAY HOW MUCH WAS THROWN AWAY. `posted_at` is rewritten whenever a post is seen
    # again, but `first_seen` is not — and `sig` is a content signature, so a landlord
    # REPOSTING the same text lands on the same archive row and pushes `posted_at` past
    # `first_seen`. The lag then goes negative and is dropped here. That was 65% of rows
    # on 2026-08-05 (1,968 of 3,027), discarded without a word: the surviving sample is
    # posts that were only ever published once, which is a different population from
    # "all posts" and must not be quoted as if it were the whole archive.
    if discarded:
        print(f"  ({discarded} of {discarded + len(lags)} rows unusable — a repost moves "
              f"posted_at past first_seen; these are posts seen once)")
    if len(lags) < 5:
        print(f"  n={len(lags)} — too few to mean anything yet")
        return
    lags.sort()
    p = lambda q: lags[int(q * (len(lags) - 1))]        # noqa: E731 - local helper
    print(f"  n={len(lags)}   median {p(.5):.0f} min   p25 {p(.25):.0f}   "
          f"p75 {p(.75):.0f}   worst {lags[-1]:.0f}")
    print(f"  within 1h: {sum(1 for x in lags if x <= 60)}   "
          f"1-3h: {sum(1 for x in lags if 60 < x <= 180)}   "
          f"over 3h: {sum(1 for x in lags if x > 180)}")


def _run_reliability() -> None:
    """Completed scrapes per day against the target. Measured 2026-07-30 at ~5 of 7
    (a 28% loss) — sleeping through a scheduled slot is invisible otherwise, which is
    exactly the failure setup_always_on.cmd addresses.

    **A SKIP IS A RUN THAT DID NOT HAPPEN, AND MUST NEVER COUNT AS ONE.** This counted
    `END|SKIP` together, so the metric read HEALTHIEST exactly when runs were being lost:
    2026-08-03 was reported as 11 runs / 119% of target while 5 of them were `lock held`
    and only 4 full scrapes actually ran. That is the one number that decides whether a
    latency change helped, and it could not fail.

    The two skip reasons are not the same thing and are counted apart: the ~1-in-8
    `random human-like skip` is DESIGNED (`SCRAPER_SKIP_RUN_PROBABILITY`) and is not a
    fault, while `lock held` is a wedged run eating the whole slot — 17 of them in the
    8 days to 2026-08-05, which is the real reason listings were found late."""
    path = config.DATA_DIR / "search_log.txt"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    full: dict = {}
    hot: dict = {}
    lost: dict = {}
    by_design: dict = {}
    for line in lines:
        m = re.match(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\s+(END|SKIP)\s*(.*)", line)
        if not m:
            continue
        day, kind, rest = m.group(1), m.group(2), m.group(3)
        for d in (full, hot, lost, by_design):
            d.setdefault(day, 0)
        if kind == "END":
            if "LIVE-HOT" in rest:
                hot[day] += 1
            else:
                full[day] += 1
        elif "random" in rest:
            by_design[day] += 1
        else:
            lost[day] += 1
    if not full:
        return
    days = sorted(full)[-7:]
    target = config.SCRAPER_RUNS_PER_DAY
    done = sum(full[d] for d in days)
    want = target * len(days)
    print(f"--- run reliability (last {len(days)} logged days, target {target} full/day) ---")
    print("  " + "  ".join(f"{d[5:]}:{full[d]}+{hot[d]}h" for d in days))
    pct = round(100 * done / want) if want else 0
    print(f"  {done}/{want} full runs = {pct}%   (+{sum(hot[d] for d in days)} hot passes)")
    n_lost = sum(lost[d] for d in days)
    if n_lost:
        print(f"  {n_lost} slot(s) LOST to a held lock / other fault"
              f" — a wedged run eats the slot it holds")
    print(f"  {sum(by_design[d] for d in days)} skipped by design (random human-like)")
    if pct < 90:
        print("   ← missed runs; check `python doctor.py` wake timers")


if __name__ == "__main__":
    main()
