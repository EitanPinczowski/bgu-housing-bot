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
import random
import sys
import time
from collections import Counter

from dotenv import load_dotenv

load_dotenv()

import config
import notifier
import pipeline
import scraper

_ROTATION_PATH = config.DATA_DIR / "rotation.json"


def _select_groups() -> list[str]:
    """Return the next rotating subset of FB_GROUPS and advance the saved
    offset. Wraps around so every group is covered over successive runs."""
    groups = config.FB_GROUPS
    if not groups:
        return []
    n = min(config.SCRAPER_GROUPS_PER_RUN, len(groups))

    offset = 0
    try:
        offset = int(json.loads(_ROTATION_PATH.read_text()).get("offset", 0))
    except Exception:
        offset = 0
    offset %= len(groups)

    # take n groups starting at offset, wrapping around
    selected = [groups[(offset + i) % len(groups)] for i in range(n)]

    try:
        _ROTATION_PATH.write_text(json.dumps({"offset": (offset + n) % len(groups)}))
    except Exception as exc:
        print(f"[main] could not persist rotation offset: {exc}")
    return selected


def run(dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "LIVE"
    selected = _select_groups()
    print(f"=== BGU housing scraper — {mode} ===")
    print(f"groups this run ({len(selected)}/{len(config.FB_GROUPS)}): {selected}\n")
    if not selected:
        print("No groups configured in config.FB_GROUPS — nothing to do.")
        return

    counts: Counter[str] = Counter()
    total_posts = 0
    groups_with_posts = 0          # for failure detection (0 across all => trouble)

    p, context = scraper.open_browser()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        for i, url in enumerate(selected):
            print(f"--- group {i + 1}/{len(selected)}: {url}")
            try:
                posts = scraper.scrape_group(page, url)
            except Exception as exc:
                # one bad group must not kill the whole run
                print(f"[main] group failed, skipping: {exc}")
                continue
            print(f"    {len(posts)} posts read")
            if posts:
                groups_with_posts += 1
            for post in posts:
                total_posts += 1
                try:
                    res = pipeline.process_post(
                        post["text"],
                        source_url=post.get("permalink"),
                        group=url,
                        image_url=post.get("image"),
                        commit=not dry_run,
                    )
                    counts[res.status.value] += 1
                    if res.status.value in ("MATCH", "NEEDS_DATA"):
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

    # --- summary ---
    matches = counts.get("MATCH", 0)
    needs = counts.get("NEEDS_DATA", 0)
    print("\n=== summary ===")
    print(f"mode: {mode}")
    print(f"posts processed: {total_posts} (groups with posts: {groups_with_posts}/{len(selected)})")
    for status in ("MATCH", "NEEDS_DATA", "DROP", "NOT_AD", "ERROR"):
        if counts.get(status):
            print(f"  {status}: {counts[status]}")

    if not dry_run:
        # Failure detection: zero posts across EVERY group almost always means
        # the session was logged out or FB changed its DOM — not a quiet day.
        # Send a distinct warning so silence stays trustworthy.
        if groups_with_posts == 0:
            notifier.send(notifier._esc(
                "⚠️ הסקרייפר לא קרא אף פוסט מאף קבוצה. ייתכן שפייסבוק ניתקה את "
                "החיבור (הריצו שוב את login.py) או ששינתה מבנה. בדקו את הלוג."))
        else:
            # Heartbeat digest — so silence means something broke, and you get a
            # one-line pulse of each run.
            notifier.send(notifier._esc(
                f"🏠 סריקה הושלמה: {total_posts} פוסטים · {matches} התאמות · "
                f"{needs} חוסר-מידע · {groups_with_posts}/{len(selected)} קבוצות"))


def main() -> None:
    parser = argparse.ArgumentParser(description="BGU housing Facebook scraper")
    parser.add_argument("--live", action="store_true",
                        help="commit results (store + notify). Default is a dry run.")
    args = parser.parse_args()
    run(dry_run=not args.live)


if __name__ == "__main__":
    main()
