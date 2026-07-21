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
import sys
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


def run(dry_run: bool) -> None:
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
    selected = _select_groups()
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
                posts, gstats = scraper.scrape_group(page, url, already_seen=seen_pred)
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
            for post in posts:
                total_posts += 1
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

            if i < len(selected) - 1:
                delay = random.uniform(*config.SCRAPER_GROUP_DELAY)
                print(f"    ...sleeping {delay:.0f}s before next group")
                time.sleep(delay)
    finally:
        context.close()
        p.stop()
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

    if blocked_reason:
        print(f"run ABORTED — Facebook block: {blocked_reason}")
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

    _log_search("END", f"{end_tag}  {time.monotonic() - started:.0f}s  "
                       f"posts={total_posts} match={matches} needs={needs} "
                       f"read={scan['read']} age_skip={scan['age_skipped']} seen_skip={scan['seen_skipped']} "
                       f"groups_ok={groups_with_posts}/{len(selected)}"
                       + (f"  block={blocked_reason}" if blocked_reason else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="BGU housing Facebook scraper")
    parser.add_argument("--live", action="store_true",
                        help="commit results (store + notify). Default is a dry run.")
    args = parser.parse_args()
    run(dry_run=not args.live)


if __name__ == "__main__":
    main()
