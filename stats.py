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
import geocode
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

    _unmapped()
    _detection_lag()
    _run_reliability()
    _alert_gate()


# The whole `unknown_locations` history, deliberately — NARROWING THIS WOULD BE A
# PLACEBO. It reads like the obvious guard against stale advice ("it failed once, two
# years ago"), but `record_unknown_location` refreshes `last_seen` on every re-sighting,
# so a name only ages out once it stops appearing entirely. Measured 2026-08-12: the
# table holds 182 rows spanning **23 days**, so every window from 30d to 3650d prints
# the identical list. The staleness was never the window — it was that nothing re-checked
# the names. `_unmapped` re-checks, and prints `last seen` so age is visible per row
# instead of being enforced by an arbitrary cutoff.
_UNMAPPED_WINDOW_DAYS = 3650


def _unmapped() -> None:
    """Locations the geocoder could not map — RE-CHECKED against today's geocoder.

    The log records what failed ONCE. Nothing expires an entry, so this section used to
    recommend pinning names that resolve perfectly well now: on 2026-08-12 its top entry
    was `שכונת הפארק` (5 hits), which the static table already answers, and acting on it
    would have added a STATIC_TABLE line that changed nothing. 98 of the 182 logged names
    were in that state — over half the list was work already done.

    UNPLACEABLE IS NOT THE SAME AS PINNABLE, and the difference was the whole top of the
    list. `אוניברסיטה`, `ליד האוניברסיטה`, `מול שער האוניברסיטה` cannot be placed BY
    DESIGN — "near the university" is a bearing, not an address (user, 2026-08-01), and
    nobody rents on the campus — so pinning them would rebuild by hand the exact wrong
    dots that `no_housing_here` and `_is_bare_proximity` exist to refuse. 16 of the 84
    survivors are that, including the 5 most frequent. They are counted, not listed:
    the action there is none, and a heading that says "pin these" must not be able to
    point at a name that must never be pinned.

    A suggestion you have to verify by hand is worth less than no suggestion, so every
    filter stays visible: the header carries `n of N` and both excluded groups print
    their count rather than vanishing."""
    uk = storage.unknown_locations(days=_UNMAPPED_WINDOW_DAYS)
    if not uk:
        return
    pin, resolved, by_design = geocode.pinnable_unknowns(uk)
    print(f"--- top unmapped locations (pin these) — {len(pin)} of {len(uk)} logged "
          f"are still unplaceable AND worth pinning ---")
    for loc, cnt, last in pin[:8]:
        print(f"  {cnt:3}  {loc}   (last seen {(last or '')[:10]})")
    if resolved:
        print(f"  ({resolved} logged name(s) resolve today — nothing to pin, not shown)")
    if by_design:
        print(f"  ({by_design} are a bearing off a landmark — refused BY DESIGN, "
              f"never pin these)")


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


def _osrm_degraded(days: list) -> None:
    """How many completed runs scored their walk times on the STRAIGHT-LINE estimate.

    A run with OSRM down does not fail, warn, or alert — it quietly writes worse numbers,
    because the bot is deliberately built to classify without the router. The AMBER
    boundary IS a walk time, so those runs' tiers and scores are approximations that look
    exactly like measurements afterwards.

    Measured over the whole log on 2026-08-09: 14 of 88 completed runs (16%), and this
    was only ever visible by grepping for it. `run_scraper.cmd` had run
    `docker start osrm_bgu` before every one of them — a container restart cannot help
    when the Docker ENGINE is down, which is what those runs actually hit.

    Reads `scraper_runs.log`, not `search_log.txt`: the warning is only written there. The
    line carries no date of its own and is emitted DURING the run, so it appears ABOVE
    that run's own END — it is therefore attributed to the NEXT END, not the previous
    timestamp. Attributing backwards happens to give the same answer whenever the run's
    START is also in the file, which is why it passed on real data and failed on a
    fixture; the run it belongs to is the one it precedes.

    That file is a best-effort copy (`type "%RUNLOG%" >> ...`), so both halves of the
    ratio are counted from it and the share stays internally consistent even if a run is
    missing from it entirely."""
    path = config.DATA_DIR / "scraper_runs.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    down: dict = {}
    ended: dict = {}
    pending = False
    for line in lines:
        m = re.match(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}", line)
        if m and re.search(r"\s+END\b", line):
            day = m.group(1)
            ended[day] = ended.get(day, 0) + 1
            if pending:
                down[day] = down.get(day, 0) + 1
                pending = False
        elif "OSRM DOWN" in line:
            pending = True
    n_down = sum(down.get(d, 0) for d in days)
    n_end = sum(ended.get(d, 0) for d in days)
    if not n_end:
        return
    if n_down:
        print(f"  {n_down}/{n_end} run(s) scored walk times on the STRAIGHT-LINE estimate "
              f"({round(100 * n_down / n_end)}%) — OSRM was down; tiers and scores from "
              f"those runs are approximations")
    total_down, total_end = sum(down.values()), sum(ended.values())
    if total_end and total_down:
        print(f"     all time: {total_down}/{total_end} "
              f"({round(100 * total_down / total_end)}%)")


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
    8 days to 2026-08-05, which is the real reason listings were found late.

    A RUN THAT STARTS AND NEVER ENDS IS COUNTED NOWHERE by END/SKIP alone, and it is a
    real loss: the 14:00 full run on 2026-08-05 logged START, wrote no END, and was gone
    from the process table an hour later. It is not a SKIP (it took the slot) and not an
    END (it produced nothing), so a metric built on those two says the day was merely
    quiet. `START - END` per day names it. Today's still-running scrape is excluded, or
    the row would accuse the healthy run currently in flight."""
    path = config.DATA_DIR / "search_log.txt"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    full: dict = {}
    hot: dict = {}
    lost: dict = {}
    by_design: dict = {}
    started: dict = {}
    aborted: dict = {}
    longest: dict = {}
    for line in lines:
        s = re.match(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\s+START\b", line)
        if s:
            started[s.group(1)] = started.get(s.group(1), 0) + 1
            continue
        a = re.match(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\s+ABORT\s*(.*)", line)
        if a:
            aborted[a.group(1)] = aborted.get(a.group(1), 0) + 1
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\s+(END|SKIP)\s*(.*)", line)
        if not m:
            continue
        day, kind, rest = m.group(1), m.group(2), m.group(3)
        for d in (full, hot, lost, by_design):
            d.setdefault(day, 0)
        if kind == "END":
            d = re.search(r"\b(\d+)s\b", rest)          # "END LIVE 5286s posts=137"
            if d:
                longest[day] = max(longest.get(day, 0), int(d.group(1)) / 60)
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
    # a run in flight right now has a START and no END yet, and is not a crash
    in_flight = 0
    try:
        import scraper
        in_flight = 1 if scraper.run_in_progress() else 0
    except Exception:                              # never let this break the report
        in_flight = 0
    n_aborted = sum(aborted.get(d, 0) for d in days)
    if n_aborted:
        print(f"  {n_aborted} run(s) ABORTED by the watchdog (stalled, or past "
              f"MAX_RUN_MINUTES) — deliberate, and the slot was freed")
    # an aborted run also has a START and no END; don't accuse it twice
    crashed = (sum(max(0, started.get(d, 0) - full[d] - hot[d]) for d in days)
               - in_flight - n_aborted)
    if crashed > 0:
        print(f"  {crashed} run(s) STARTED and never finished — took the slot, produced "
              f"nothing, and are invisible to END/SKIP")
    _osrm_degraded(days)
    # HOW CLOSE IS A HEALTHY RUN TO THE CEILING THAT WOULD KILL IT? MAX_RUN_MINUTES
    # aborts a run that crawls, but a legitimate full run measured 88 min against a
    # 120 limit on 2026-08-05 — real margin, not much of it, and August is peak posting
    # season. Say so while there is still time to raise the ceiling deliberately,
    # rather than discovering it when a healthy run is killed.
    # 70%, not 75%: the run that motivated this was 88 min against 120, which is 73% —
    # a threshold that would not have flagged its own motivating case is decoration.
    worst = max((longest.get(d, 0) for d in days), default=0)
    cap = getattr(config, "MAX_RUN_MINUTES", 0)
    if worst:
        # Over the ceiling and close to it are different messages. The window still
        # contains the 509-minute run that slept through the night — that one is what
        # the ceiling is FOR, and calling it "close to" the limit would be nonsense.
        if cap and worst > cap:
            note = "   ← would now be ABORTED (the ceiling is newer than this run)"
        elif cap and worst > cap * 0.70:
            note = "   ← close to the ceiling"
        else:
            note = ""
        print(f"  longest completed run {worst:.0f} min"
              + (f" (ceiling {cap})" if cap else "") + note)
    if pct < 90:
        print("   ← missed runs; check `python doctor.py` wake timers")


if __name__ == "__main__":
    main()
