"""
Per-group yield report — which Facebook groups actually produce apartments, and how
deeply each is being read.

Prune/keep decisions should come from THIS, not memory: a group was once dropped for
"0 matches ever" that in fact had the best match rate in the archive.

    python group_report.py

Shows, per group: posts archived, MATCH count, match rate, the scan depth it will get
on the next run (see main._group_depths), and whether it's currently configured.
"""
from __future__ import annotations
import sys

from dotenv import load_dotenv

load_dotenv()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import main
import storage


def main_report() -> None:
    depths = main._group_depths()
    full = config.SCRAPER_MIN_POSTS_PER_GROUP
    configured = set(config.FB_GROUPS)
    rows = storage.group_yield()
    if not rows:
        print("no archived posts yet — run the scraper first")
        return
    scored = []
    for g, tot, m, n, d, na in rows:
        rate = (m / tot) if tot else 0.0
        scored.append((rate, g, tot, m, n, d, na))
    scored.sort(reverse=True)

    print(f"{'group':>18}  {'posts':>5} {'MATCH':>5} {'rate':>6} {'depth':>5}  status")
    for rate, g, tot, m, n, d, na in scored:
        gid = g.rstrip("/").split("/")[-1]
        depth = depths.get(g, full)
        status = "active" if g in configured else "NOT configured"
        flag = ""
        if g not in configured and rate > 0:
            flag = "  <- producing matches but not scanned!"
        elif g in configured and tot >= config.GROUP_MIN_HISTORY and rate <= config.GROUP_POOR_RATE:
            flag = "  <- low yield, read shallowly"
        print(f"{gid:>18}  {tot:>5} {m:>5} {rate:>5.1%} {depth:>5}  {status}{flag}")

    active = [s for s in scored if s[1] in configured]
    tot_posts = sum(s[2] for s in active)
    tot_match = sum(s[3] for s in active)
    print(f"\nactive groups: {len(active)}  ·  posts {tot_posts}  ·  MATCH {tot_match}"
          f"  ·  overall rate {(tot_match / tot_posts if tot_posts else 0):.1%}")


if __name__ == "__main__":
    main_report()
