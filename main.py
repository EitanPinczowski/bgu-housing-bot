"""
Scraper orchestrator (increment 2).

Reads a ROTATING subset of your Facebook groups through the saved login
profile, runs each post through the same pipeline as manual mode, and prints a
summary. Intended to run ~2×/day via Windows Task Scheduler.

    python main.py            # DRY RUN — classify + print, write nothing, no alerts
    python main.py --live     # commit: dedup, store, and send Telegram alerts

Dry-run is the default on purpose (CLAUDE.md → SAFETY CONSTRAINTS): you can watch
what it *would* do against a couple of groups before ever letting it write or
notify. Only a subset of groups runs each time (config.SCRAPER_GROUPS_PER_RUN),
and the starting offset rotates across runs so every group gets covered over a
few runs without hammering all of them at once.

Run login.py once first to create the session.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import time
from collections import Counter
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

import config
import llm
import notifier
import pipeline
import scraper
import sheets
import storage

_SCRAPES_PATH = config.DATA_DIR / "group_scrapes.json"   # {url: [iso_ts, ...]}
_SEARCH_LOG = config.DATA_DIR / "search_log.txt"


def _log_search(event: str, detail: str = "") -> None:
    """Append one line to data/search_log.txt — a clean, greppable record of when
    every search STARTs and ENDs (separate from the verbose stdout run log)."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {event:<5}  {detail}".rstrip()
    print(line)
    try:
        with open(_SEARCH_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        print(f"[main] could not write search log: {exc}")


def _load_scrapes() -> dict:
    try:
        return json.loads(_SCRAPES_PATH.read_text())
    except Exception:
        return {}


def _save_scrapes(hist: dict) -> None:
    try:
        _SCRAPES_PATH.write_text(json.dumps(hist))
    except Exception as exc:
        print(f"[main] could not persist scrape history: {exc}")


def _record_scrape(url: str) -> None:
    """Timestamp a successful group read, and prune history older than 24h."""
    hist = _load_scrapes()
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    kept = [t for t in hist.get(url, []) if t >= cutoff]
    kept.append(datetime.now().isoformat())
    hist[url] = kept
    _save_scrapes(hist)


def _scrapes_last_24h(url: str, hist: dict, cutoff_iso: str) -> list:
    return [t for t in hist.get(url, []) if t >= cutoff_iso]


def _select_groups() -> list[str]:
    """Pick the MOST-OVERDUE groups this run — fewest reads in the last 24h,
    oldest first — sized so that across SCRAPER_RUNS_PER_DAY runs every group is
    read at least SCRAPER_MIN_SCRAPES_PER_DAY times. Guarantees coverage instead
    of leaving a quiet group unseen until its posts age out of the 24h window."""
    groups = config.FB_GROUPS
    if not groups:
        return []
    # Scan-all mode: every group each run, in a random order (no clockwork pattern).
    if getattr(config, "SCRAPER_SCAN_ALL_GROUPS", False):
        shuffled = list(groups)
        random.shuffle(shuffled)
        return shuffled
    total = len(groups)
    hist = _load_scrapes()
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    counts = {g: len(_scrapes_last_24h(g, hist, cutoff)) for g in groups}
    last = {g: max(_scrapes_last_24h(g, hist, cutoff), default="") for g in groups}

    # enough groups per run to guarantee the daily minimum, plus a little jitter
    need = math.ceil(total * config.SCRAPER_MIN_SCRAPES_PER_DAY / config.SCRAPER_RUNS_PER_DAY)
    hi = max(need, math.ceil(total * config.SCRAPER_GROUPS_FRACTION[1]))
    n = min(total, random.randint(need, hi))

    # most-overdue first: fewest reads in 24h, then longest since last read
    order = sorted(groups, key=lambda g: (counts[g], last[g]))
    return order[:n]


def _group_depths() -> dict:
    """{group_url: min_posts} — how deeply to read each group this run, from its measured
    MATCH-per-post rate. A group that produces almost nothing is read shallowly (down to
    GROUP_MIN_POSTS_FLOOR) and a productive one gets full depth, so matches per run rise
    without increasing total reads. A group with little history keeps full depth so it's
    never starved before it has had a fair chance."""
    full = config.SCRAPER_MIN_POSTS_PER_GROUP
    if not getattr(config, "GROUP_YIELD_SCALING", False):
        return {}
    try:
        yields = {g: (tot, m) for g, tot, m, _n, _d, _na in storage.group_yield()}
    except Exception:
        return {}
    floor, rich, poor = (config.GROUP_MIN_POSTS_FLOOR, config.GROUP_RICH_RATE,
                         config.GROUP_POOR_RATE)
    depths = {}
    for g in config.FB_GROUPS:
        tot, matches = yields.get(g, (0, 0))
        if tot < config.GROUP_MIN_HISTORY:
            continue                                    # too little history -> full depth
        rate = matches / tot
        if rate >= rich:
            continue                                    # productive -> full depth
        if rate <= poor:
            depths[g] = floor
        else:                                           # scale linearly between the two
            f = (rate - poor) / (rich - poor)
            depths[g] = int(round(floor + f * (full - floor)))
    return depths


def _hot_groups() -> list:
    """The few highest-yield groups, for the fast shallow --hot pass. Ranked by measured
    MATCH-per-post (needs some history); falls back to the configured order."""
    configured = list(config.FB_GROUPS)
    try:
        rows = [(m / tot, g) for g, tot, m, *_ in storage.group_yield()
                if tot >= config.GROUP_MIN_HISTORY and g in configured]
    except Exception:
        rows = []
    rows.sort(reverse=True)
    hot = [g for _rate, g in rows[:config.HOT_GROUP_COUNT]]
    return hot or configured[:config.HOT_GROUP_COUNT]


TEARDOWN_TIMEOUT_SEC = 30


def _bounded_teardown(context, p) -> None:
    """Close the browser, but never let closing it become the thing that hangs the bot.

    Each step gets its own thread and its own deadline, because a HANG is not catchable:
    `context.close()` on a dead Playwright pipe simply never returns, and a plain
    try/except would sail straight past it into a permanent wait. The threads are daemons,
    so a stuck one cannot stop the interpreter exiting either.

    Abandoning a half-closed browser is safe: `scraper.reap_orphan_browsers()` clears
    leftovers on the profile path at the start of the next run, and that is a much smaller
    problem than a held lock — a leftover browser costs one cleanup, a held lock costs
    every scheduled run until someone notices."""
    import threading
    for label, fn in (("context.close", context.close), ("playwright.stop", p.stop)):
        done = threading.Event()

        def work(fn=fn, label=label, done=done):
            try:
                fn()
            except Exception as exc:
                print(f"[main] {label} failed during teardown: {exc}")
            finally:
                done.set()

        threading.Thread(target=work, daemon=True).start()
        if not done.wait(TEARDOWN_TIMEOUT_SEC):
            print(f"[main] {label} did not return in {TEARDOWN_TIMEOUT_SEC}s — "
                  "abandoning it and releasing the lock anyway")


def run(dry_run: bool, hot: bool = False) -> None:
    config.validate()                 # fail fast on a broken config, before opening a browser
    mode = "DRY RUN" if dry_run else "LIVE"
    # Occasionally skip a live run so the cadence isn't clockwork (see config).
    if not dry_run and random.random() < config.SCRAPER_SKIP_RUN_PROBABILITY:
        _log_search("SKIP", "random human-like skip")
        print("skipping this run (random human-like skip)")
        return
    started = time.monotonic()
    # Single-instance guard: never open the browser while another scraper/backfill
    # session holds the profile (two sessions deadlock Chromium's profile lock).
    if not scraper.acquire_lock():
        _log_search("SKIP", "another scraper session is running (lock held)")
        print("[main] another scraper/browser session is already running — skipping this run")
        return
    # …and give up on ourselves if we stop making progress, rather than sitting on the
    # lock and starving every later run (measured: one hang blocked six hours of them).
    scraper.start_self_watchdog()
    if hot:
        # Fast shallow pass over only the best groups — see config.HOT_* (net volume is
        # LOWER than before, because yield-scaling trimmed the normal runs).
        selected = _hot_groups()
        depths = {g: config.HOT_MIN_POSTS for g in selected}
        print(f"[main] HOT pass: {len(selected)} top-yield group(s), "
              f"{config.HOT_MIN_POSTS} posts each")
    else:
        selected = _select_groups()
        depths = _group_depths()      # read low-yield groups shallowly (see _group_depths)
        if depths:
            print(f"[main] yield-scaled depth for {len(depths)} low-yield group(s): "
                  + ", ".join(f"{u.rstrip('/').split('/')[-1]}={d}" for u, d in depths.items()))
    _log_search("START", f"{'LIVE' if not dry_run else 'DRY'}  groups={len(selected)}/{len(config.FB_GROUPS)}")
    print(f"=== BGU housing scraper — {mode} ===")
    print(f"groups this run ({len(selected)}/{len(config.FB_GROUPS)}): {selected}\n")
    if not selected:
        print("No groups configured in config.FB_GROUPS — nothing to do.")
        _log_search("END", f"{'LIVE' if not dry_run else 'DRY'}  0s  no groups configured")
        scraper.release_lock()
        return

    counts: Counter[str] = Counter()
    scan: Counter[str] = Counter()  # read / age_skipped / seen_skipped across groups
    total_posts = 0
    groups_with_posts = 0          # for failure detection (0 across all => trouble)
    blocked_reason = None          # set if FB shows a checkpoint/login wall
    fallback_capped = None         # set if the run stopped on the local-fallback cap

    # On a LIVE run, let the scraper skip posts already processed in an earlier run
    # (so an all-seen group stops scrolling fast). Uses the exact keys the pipeline's
    # pre-LLM dedup uses. None on a dry run, so a preview still surfaces everything.
    seen_pred = None
    if not dry_run:
        def seen_pred(text, url):
            if url and storage.is_url_seen(url):
                return True
            return storage.is_seen(pipeline._text_sig(pipeline._strip_bidi(text)))

    # Batch mode: don't ping per-post; collect the run's matches and send one ranked,
    # capped batch to the group at the end (see notifier.send_batch).
    batch = (not dry_run) and getattr(config, "SCRAPER_BATCH_ALERTS", False)
    alertable: list = []
    posts_with_link = 0            # how many returned posts captured a real permalink

    p, context = scraper.open_browser()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        for i, url in enumerate(selected):
            print(f"--- group {i + 1}/{len(selected)}: {url}")
            try:
                posts, gstats = scraper.scrape_group(page, url, already_seen=seen_pred,
                                                     min_posts=depths.get(url))
            except scraper.FacebookBlock as exc:
                # A checkpoint/login wall — stop the ENTIRE run, do not retry.
                blocked_reason = str(exc)
                print(f"[main] FACEBOOK BLOCK: {blocked_reason} — aborting run")
                break
            except Exception as exc:
                # one bad group must not kill the whole run
                print(f"[main] group failed, skipping: {exc}")
                continue
            scan.update(gstats)
            print(f"    {len(posts)} fresh posts (read {gstats['read']}, "
                  f"age-skip {gstats['age_skipped']}, seen-skip {gstats['seen_skipped']})")
            _record_scrape(url)          # count this read toward the daily coverage
            if posts:
                groups_with_posts += 1
            # POSTS ARE EXTRACTED IN BATCHES, because the free tier meters REQUESTS
            # per day and five of these posts fit in one (see llm.extract_many).
            # `pending` holds posts that survived the pre-LLM gates and are waiting
            # for a batch to fill up.
            pending: list = []

            def flush(pending=pending, url=url):
                """Extract the buffered posts in one request, then classify each."""
                nonlocal total_posts, posts_with_link, fallback_capped
                if not pending:
                    return
                extracts = llm.extract_many([(p["text"], p.get("comments") or "")
                                             for p in pending])
                for post, e in zip(pending, extracts):
                    _handle(post, url, extract=e)
                pending.clear()

            def _handle(post, url, extract=None):
                nonlocal total_posts, posts_with_link
                total_posts += 1
                # progress heartbeat: a slow LLM (local fallback can take ~200s/post) is
                # fine, a run that stops beating for STALL_MINUTES is wedged and gets
                # cleared by the next run / flagged by doctor.
                scraper.beat(f"post {total_posts}")
                if post.get("permalink"):
                    posts_with_link += 1
                try:
                    res = pipeline.process_post(
                        post["text"],
                        source_url=post.get("permalink"),
                        group=url,
                        images=post.get("images") or [],
                        comments=post.get("comments") or "",
                        age_hours=post.get("age_hours"),
                        commit=not dry_run,
                        alert=not batch,        # batch: defer the ping to run's end
                        extract=extract,        # already extracted, in a batch
                    )
                    counts[res.status.value] += 1
                    if res.status.value in ("MATCH", "NEEDS_DATA"):
                        if batch:
                            alertable.append(res)
                        icon = "✅" if res.preferred else "🟡" if res.status.value == "MATCH" else "⚠️"
                        print(f"    {icon} {res.status.value} — {res.reason}"
                              f"{' — ' + post['permalink'] if post.get('permalink') else ''}")
                except Exception as exc:
                    print(f"[main] pipeline error on a post: {exc}")
                    counts["ERROR"] += 1

            for post in posts:
                # THE LOCAL FALLBACK IS A LIFEBOAT, NOT THE ENGINE. Once Gemini's
                # quota is gone every post costs ~63s locally, and a run that keeps
                # going holds the scraper lock for hours — on 2026-08-03 that cost
                # the 10:00 and 12:00 runs, including the one that would have had
                # fresh quota. Stop while the day is still salvageable. Nothing is
                # lost: these posts were never marked seen, so the next run reads
                # them, and by then the quota may have reset (10:00 Israel).
                # Checked at the TOP of the iteration on purpose: the early-verdict
                # path below ends in `continue`, so a check at the bottom would be
                # skipped by exactly the posts that are cheapest to notice it on.
                if llm.fallback_budget_spent():
                    fallback_capped = (
                        f"local fallback cap reached ({llm.fallback_used} posts) — "
                        f"ending the run so the next one can start")
                    print(f"[main] {fallback_capped}")
                    break
                # Ask the cheap gates FIRST and don't put their posts in a batch:
                # they already spare ~27% of posts an LLM call, and batching them
                # would pay for what we just saved. `pre_llm_verdict` is pure, and
                # process_post re-runs it anyway, so this cannot skip dedup.
                early = pipeline.pre_llm_verdict(
                    post["text"], source_url=post.get("permalink"), group=url,
                    images=post.get("images") or [], commit=not dry_run)
                if early is not None or pipeline.is_ocr_post(post["text"],
                                                            post.get("images")):
                    # OCR posts also go singly: they need the image path, which is
                    # Gemini-only and separately capped per run.
                    _handle(post, url)
                    continue
                pending.append(post)
                if len(pending) >= config.LLM_BATCH_SIZE:
                    flush()

            # A partial batch must never cross a group boundary — otherwise the last
            # few posts of a group wait for the next group to fill the buffer, and
            # the final group's remainder is dropped on the floor entirely.
            flush()

            if fallback_capped:
                break

            if i < len(selected) - 1:
                delay = random.uniform(*config.SCRAPER_GROUP_DELAY)
                print(f"    ...sleeping {delay:.0f}s before next group")
                time.sleep(delay)
    finally:
        # TEARDOWN MUST NEVER KEEP THE LOCK. These three ran bare, in order, so the first
        # one to raise OR HANG skipped the rest — and `release_lock` is last.
        # Measured 2026-08-04: Playwright's node subprocess died mid-run with EPIPE at
        # group 11/15, `context.close()` never returned, and the python process sat alive
        # holding the lock. The 17:00 hot pass and the 00:46 full run both logged
        # "another scraper session is running"; the 00:46 launcher then found the holder
        # unkillable and gave up on the lock entirely. The scraper's self-watchdog does
        # not help here — it aborts a run that stops making PROGRESS, and this one had
        # already finished scraping and was dying in cleanup.
        # The lock is an OS file lock, so releasing it is what frees the next run.
        _bounded_teardown(context, p)
        scraper.release_lock()      # browser closed → profile free for the next run

    # --- summary ---
    matches = counts.get("MATCH", 0)
    needs = counts.get("NEEDS_DATA", 0)
    print("\n=== summary ===")
    print(f"mode: {mode}")
    print(f"posts processed: {total_posts} (groups with posts: {groups_with_posts}/{len(selected)})")
    print(f"funnel: read {scan['read']} · age-skip {scan['age_skipped']} · "
          f"seen-skip {scan['seen_skipped']} · processed {total_posts}")
    _nolink_pct = round(100 * (total_posts - posts_with_link) / total_posts) if total_posts else 0
    print(f"post links: {posts_with_link}/{total_posts} captured · {_nolink_pct}% without a link")
    for status in ("MATCH", "NEEDS_DATA", "DROP", "NOT_AD", "ERROR"):
        if counts.get(status):
            print(f"  {status}: {counts[status]}")
    if llm.fallback_used:
        print(f"  (served by local fallback: {llm.fallback_used} — Gemini quota was hit)")
    # dependency health (#41): geocode misses + whether OSRM was reachable this run
    print(f"geocode misses: {pipeline.geocode.misses}"
          + ("  ·  ⚠️ OSRM DOWN (used straight-line walk estimate)" if pipeline.osrm.osrm_down else ""))

    if blocked_reason:
        print(f"run ABORTED — Facebook block: {blocked_reason}")
    if fallback_capped:
        # Say it in the summary AND name the groups that went unread, so a short
        # run is never mistaken for a quiet day on Facebook.
        print(f"run ENDED EARLY — {fallback_capped}")
        print(f"  groups not scanned this run: {len(selected) - (i + 1)} of "
              f"{len(selected)} — their posts are unmarked and the next run reads them")
    if not dry_run:
        if blocked_reason:
            # A checkpoint/login wall — the account needs a manual re-login. This
            # is the one condition where you must act before the next run.
            notifier.send(notifier._esc(
                "⛔ פייסבוק חסמה את הסריקה (מסך אימות/התחברות). אל תריצו שוב — "
                f"היכנסו ידנית והריצו login.py. סיבה: {blocked_reason}"),
                target="primary")
        # Failure detection: zero posts across EVERY group almost always means
        # the session was logged out or FB changed its DOM — not a quiet day.
        # Send a distinct warning so silence stays trustworthy.
        elif groups_with_posts == 0:
            notifier.send(notifier._esc(
                "⚠️ הסקרייפר לא קרא אף פוסט מאף קבוצה. ייתכן שפייסבוק ניתקה את "
                "החיבור (הריצו שוב את login.py) או ששינתה מבנה. בדקו את הלוג."),
                target="primary")
        else:
            # Send the run's matches as ONE ranked, capped batch to the group (see
            # notifier.send_batch) instead of one ping per post.
            if batch and alertable:
                sent = notifier.send_batch(alertable, target="group",
                                           top_k=getattr(config, "SCRAPER_ALERT_TOP_K", 5))
                print(f"[main] batched alerts: sent {sent} of {len(alertable)} to the group")
            # Heartbeat digest — so silence means something broke, and you get a
            # one-line pulse of each run.
            fb = f" · {llm.fallback_used} במודל מקומי" if llm.fallback_used else ""
            quota = "\n⚠️ מכסת Gemini אזלה — עברנו למודל מקומי איטי" if llm._primary_exhausted else ""
            funnel = (f"\n🔎 נסרקו {scan['read']} · דילוג ישן {scan['age_skipped']} · "
                      f"דילוג נראו {scan['seen_skipped']} · לעיבוד {total_posts}")
            notifier.send(notifier._esc(
                f"🏠 סריקה הושלמה: {total_posts} פוסטים · {matches} התאמות · "
                f"{needs} חוסר-מידע · {groups_with_posts}/{len(selected)} קבוצות" + fb + quota + funnel),
                target="primary")
        # Reconcile the sheet with the DB (catches any rows a per-post append
        # dropped to a rate-limit blip), then keep it ordered best-first.
        added = sheets.sync_from_db()
        if added:
            print(f"[main] sheet sync: appended {added} missing rows")
        sheets.sort_by_score()
        pruned = storage.prune_old_posts(config.POST_ARCHIVE_RETENTION_DAYS)
        if pruned:
            print(f"[main] archive prune: lightened {pruned} old posts")

    end_tag = "BLOCKED" if blocked_reason else ("LIVE" if not dry_run else "DRY")
    if hot and not blocked_reason:
        end_tag += "-HOT"             # so run-freshness checks can tell the passes apart

    _log_search("END", f"{end_tag}  {time.monotonic() - started:.0f}s  "
                       f"posts={total_posts} match={matches} needs={needs} "
                       f"read={scan['read']} age_skip={scan['age_skipped']} seen_skip={scan['seen_skipped']} "
                       f"groups_ok={groups_with_posts}/{len(selected)}"
                       + (f"  block={blocked_reason}" if blocked_reason else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="BGU housing Facebook scraper")
    parser.add_argument("--live", action="store_true",
                        help="commit results (store + notify). Default is a dry run.")
    parser.add_argument("--hot", action="store_true",
                        help="fast SHALLOW pass over only the top-yield groups, to see a "
                             "great listing sooner. Costs far less than a normal run.")
    args = parser.parse_args()
    run(dry_run=not args.live, hot=args.hot)


if __name__ == "__main__":
    main()
