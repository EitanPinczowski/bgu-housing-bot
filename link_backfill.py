"""
One-off LIVE link backfill for the top listings.

~85% of listings never captured a real post permalink (FB hides it), so their
alerts fall back to a group link. For the alert-worthy ones (score >= MIN) that are
still missing a link, this recovers the real permalink via Facebook's GROUP SEARCH
(which reaches older posts the feed no longer shows): search the post's phone/text,
match a result to the target, reconstruct the canonical link (scraper._permalink_
and_age), and write it onto the listing + its archived post. Then rebuild the Sheet.

Best-effort: only posts still FINDABLE via search get a link; genuinely gone ones
won't. Conservative like the scraper — read-only (search + read, no posts/clicks),
long delays, checkpoint-abort. Run it deliberately, and NOT while another scraper
session (a run or a dry run) is using the same Chrome profile.

    python link_backfill.py            # backfill score >= 70 (default)
    python link_backfill.py 80         # a different minimum score
"""
from __future__ import annotations
import os
import random
import re
import sys
import time
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import pipeline
import scraper
import sheets
import storage
from models import ListingExtract

_MAX_SEARCHES = 60          # hard cap on group-search page loads per run
_RESULTS_PER_SEARCH = 6     # only inspect the first few results


def _digits(s) -> str:
    return re.sub(r"\D", "", s or "")


def _query(address, contact) -> str:
    """The most distinctive search term for a listing: its phone (unique) else the
    address (trimmed of quotes)."""
    d = _digits(contact)
    if len(d) >= 9:
        return d
    return re.sub(r"[\"'׳״]", "", address or "").strip()


def _match(story_text: str, match_sig, phone_digits: str) -> bool:
    """Does this search-result story correspond to the target listing? True if the
    post text signatures match, or the target's phone appears in the story text."""
    txt = pipeline._strip_bidi(story_text or "")
    if match_sig and pipeline._text_sig(txt) == match_sig:
        return True
    if phone_digits and len(phone_digits) >= 9 and phone_digits in _digits(txt):
        return True
    return False


def _index() -> dict:
    """dedup_key -> {'sig': archived_sig, 'msig': recomputed text-sig} from the
    archive, so a target listing can be matched to a live post by its text."""
    idx: dict = {}
    for p in storage.all_posts():                 # newest first — first key wins
        if not p["parsed_json"]:
            continue
        try:
            e = ListingExtract.model_validate_json(p["parsed_json"])
        except Exception:
            continue
        key = storage.make_dedup_key(e)
        if key in idx:
            continue
        raw = p["raw_text"] or ""
        idx[key] = {"sig": p["sig"],
                    "msig": pipeline._text_sig(pipeline._strip_bidi(raw)) if raw else None}
    return idx


def _targets(min_score: int) -> list:
    """(dedup_key, group_url, query, phone_digits) for score>=min listings with no link."""
    import sqlite3
    with sqlite3.connect(config.DB_PATH) as c:
        rows = c.execute(
            "SELECT dedup_key, \"group\", address, contact FROM listings "
            "WHERE score >= ? AND (source_url IS NULL OR source_url='') "
            "AND \"group\" IS NOT NULL AND \"group\" != ''", (min_score,)).fetchall()
    out = []
    for key, group, address, contact in rows:
        q = _query(address, contact)
        if q:
            out.append((key, group, q, _digits(contact)))
    return out


def _search_url(group_url: str, query: str):
    m = re.search(r"/groups/(\d+)", group_url)
    if not m:
        return None
    return f"https://www.facebook.com/groups/{m.group(1)}/search/?q={quote(query)}"


def run(min_score: int = 70) -> None:
    targets = _targets(min_score)
    idx = _index()
    print(f"link backfill: {len(targets)} score>={min_score} listings missing a link")
    if not targets:
        return
    found = 0
    p, context = scraper.open_browser()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        for i, (key, group, query, phone) in enumerate(targets[:_MAX_SEARCHES]):
            url = _search_url(group, query)
            if not url:
                continue
            info = idx.get(key) or {}
            match_sig = info.get("msig")
            print(f"--- {i + 1}/{min(len(targets), _MAX_SEARCHES)}: search '{query}' in {group}")
            try:
                page.goto(url, wait_until="domcontentloaded")
                if (reason := scraper._blocked_reason(page)):
                    print(f"[backfill] FACEBOOK BLOCK: {reason} — aborting")
                    break
                time.sleep(random.uniform(*config.SCRAPER_SCROLL_DELAY))
                hit = None
                for story in scraper._stories(page)[:_RESULTS_PER_SEARCH]:
                    try:
                        text = scraper._clean_story(story.inner_text() or "")
                    except Exception:
                        continue
                    if _match(text, match_sig, phone):
                        hit = scraper._permalink_and_age(story, group)[0]
                        if hit:
                            break
                if hit:
                    storage.set_source_url(key, hit)
                    if info.get("sig"):
                        storage.set_post_source_url(info["sig"], hit)
                    found += 1
                    print(f"    ✓ {hit}")
                else:
                    print("    (no match found in search results)")
            except Exception as exc:
                print(f"[backfill] search failed, skipping: {exc}")
            if i < len(targets) - 1:
                time.sleep(random.uniform(*config.SCRAPER_GROUP_DELAY))
    finally:
        context.close()
        p.stop()

    print(f"\nbackfilled {found}/{len(targets)} links")
    if found:
        n = sheets.rebuild_from_db()
        sheets.sort_by_score()
        print(f"sheet rebuilt ({n} rows)")


def main() -> None:
    min_score = int(sys.argv[1]) if len(sys.argv) > 1 else 70
    run(min_score)


if __name__ == "__main__":
    main()
