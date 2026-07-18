"""
Conservative Facebook group reader (increment 2).

Reuses the persistent login profile created by login.py — never touches your
password, never injects cookies. It only scrolls and reads: it does NOT post,
comment, message, react, or click anything interactive.

  open_browser()          -> (playwright, context) using the persistent profile
  scrape_group(page, url) -> list of {"text", "permalink"} dicts for one group

Pacing is deliberately slow and randomized (see config.SCRAPER_*). Do not speed
this up — the account is the user's only Facebook account (CLAUDE.md → SAFETY
CONSTRAINTS).

  ⚠️  FB's DOM is unstable. Everything in the "FRAGILE" block below WILL break
  periodically — it's all kept together so you can retune it in one place.
  Nothing else in the codebase depends on FB's HTML.

How the extraction works (learned from the live DOM):
  - A group feed is one `[role="feed"]`; each DIRECT child div is one "story"
    (post). Comments are separate `[role="article"]`s with aria "Comment by".
  - FB VIRTUALIZES the feed: a post that scrolls out of view has its text
    emptied. So we read the currently-rendered stories at EACH scroll step and
    accumulate, rather than once at the end.
  - Story text is noisy: repeated "Facebook" avatar alt-text, single-character
    lines (FB's CSS-scrambled anti-scrape timestamps), and a comments/reactions
    tail. We strip those and cut the tail; the Hebrew post body remains, which
    is all the LLM needs.
"""
from __future__ import annotations

import random
import re
import time
from typing import Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

import config

# ============================ FRAGILE: FB specifics ==========================
# Edit HERE when Facebook changes its DOM / UI strings.

_FEED_SELECTOR = '[role="feed"]'
_STORY_SELECTOR = '[role="feed"] > div'      # each direct child = one post story
_SCROLL_PX = 1100                            # small steps so posts render before we read
_MIN_POST_CHARS = 40                         # shorter than this = not a real post

# A post permalink is the first anchor whose href contains one of these. FB uses
# several formats: /groups/<id>/posts/<id>/, /permalink/, ?story_fbid=, and the
# newer /stories/<set>/<base64>/ form — cover them all. Tracking query junk
# (?comment_id=, __cft__, __tn__) is stripped by _permalink via split("?").
_PERMALINK_HINTS = ("/posts/", "/permalink/", "/stories/", "story_fbid")

# Everything from the first of these markers onward is the comments/reactions
# tail — dropped so we keep just the post body. English (this account's UI) +
# common Hebrew fallbacks in case the UI language changes.
_TAIL_MARKERS = (
    "View more comments", "View 1 more comment", "View previous comments",
    "Write a comment", "Write a public comment", "Write an answer", "All reactions",
    "הצג עוד תגובות", "צפייה בתגובות נוספות", "כתיבת תגובה", "כתוב תגובה",
    "כל התגובות", "כתוב תשובה",
)

# Whole lines dropped as UI chrome / noise.
_DROP_EXACT = {
    "Facebook", "Reply", "Like", "Comment", "Share", "Send", "Follow", "·",
    "Most relevant", "sort group feed by", "See more", "See More", "Active",
    "הגב", "אהבתי", "תגובה", "שיתוף", "עוד", "ראה עוד", "הצג עוד",
}

# Request newest-first. FB group feeds default to "Most relevant", which can keep
# re-showing old popular posts; chronological is what a fresh-listing monitor
# wants. Harmlessly ignored by FB if the param name ever changes.
_SORT_CHRONOLOGICAL = True
_SORT_PARAM = "sorting_setting=CHRONOLOGICAL"
# =============================================================================

_NUM_RE = re.compile(r"^\+?\d[\d,]*$")       # like counts, "+5", "1,234"
_HEBREW_RE = re.compile(r"[֐-׿]")  # at least one Hebrew letter


def open_browser():
    """Launch a non-headless persistent context from the saved login profile.

    Returns (playwright, context). The caller must close BOTH (context first,
    then playwright.stop()) — see main.py.
    """
    p = sync_playwright().start()
    context = p.chromium.launch_persistent_context(
        str(config.SCRAPER_PROFILE_DIR),
        headless=config.SCRAPER_HEADLESS,
        locale="he-IL",
        timezone_id="Asia/Jerusalem",
    )
    return p, context


def _clean_story(raw: str) -> str:
    """Strip FB noise from one story's inner_text and cut the comments tail,
    leaving (mostly) the post body. Author name / a stray inline comment may
    remain — harmless for the LLM, which reads the body and ignores the rest."""
    cut = len(raw)
    for marker in _TAIL_MARKERS:
        i = raw.find(marker)
        if i != -1:
            cut = min(cut, i)
    out = []
    for line in raw[:cut].splitlines():
        s = line.strip().replace("… See more", "").replace("See more", "").strip()
        if not s or s in _DROP_EXACT:
            continue
        if len(s) == 1:            # CSS-scrambled anti-scrape timestamp chars
            continue
        if _NUM_RE.match(s):       # reaction/comment counts
            continue
        out.append(s)
    return "\n".join(out).strip()


def _permalink(story) -> Optional[str]:
    """First anchor in the story that looks like a post permalink, cleaned of
    query junk. None if not found (post still usable — permalink is a bonus)."""
    try:
        for a in story.query_selector_all("a[href]"):
            href = a.get_attribute("href") or ""
            if any(hint in href for hint in _PERMALINK_HINTS):
                if href.startswith("/"):
                    href = "https://www.facebook.com" + href
                return href.split("?")[0]
    except Exception:
        pass
    return None


def scrape_group(page: Page, url: str) -> list[dict]:
    """Open one group and return its visible posts, newest-first.

    Each item: {"text": <cleaned post body>, "permalink": <url or None>}.
    Deduplicated by permalink (falling back to text) WITHIN this group. Reads
    incrementally across scrolls because FB virtualizes the feed.
    """
    if _SORT_CHRONOLOGICAL and "sorting_setting" not in url:
        url = url + ("&" if "?" in url else "?") + _SORT_PARAM

    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector(_FEED_SELECTOR, timeout=15000)
    except PWTimeout:
        print(f"[scraper] no feed appeared for {url} "
              "(login expired? not a member? group layout changed?)")
        return []
    time.sleep(random.uniform(*config.SCRAPER_SCROLL_DELAY))  # let the feed hydrate

    collected: dict[str, dict] = {}
    # read, then scroll — SCRAPER_MAX_SCROLLS scrolls means MAX_SCROLLS+1 reads
    for _ in range(config.SCRAPER_MAX_SCROLLS + 1):
        for story in page.query_selector_all(_STORY_SELECTOR):
            try:
                raw = story.inner_text() or ""
            except Exception:
                continue
            text = _clean_story(raw)
            if len(text) < _MIN_POST_CHARS or not _HEBREW_RE.search(text):
                continue
            link = _permalink(story)
            # Key on the text (stable across scroll passes), not the permalink —
            # FB often renders a post's body before its timestamp/permalink
            # anchor. Backfill the permalink when a later pass exposes it.
            key = text[:80]
            entry = collected.get(key)
            if entry is None:
                collected[key] = {"text": text, "permalink": link}
            elif entry["permalink"] is None and link:
                entry["permalink"] = link
        page.mouse.wheel(0, _SCROLL_PX)
        time.sleep(random.uniform(*config.SCRAPER_SCROLL_DELAY))

    return list(collected.values())
