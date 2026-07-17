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

  ⚠️  FB's DOM is unstable. The selectors below WILL break periodically. They are
  intentionally kept together and small so you can retune them in one place when
  posts stop coming through. Nothing else in the codebase depends on FB's HTML.
"""
from __future__ import annotations

import random
import time
from typing import Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

import config

# --- FB selectors — the fragile part. Edit HERE when the DOM changes. ---------
_ARTICLE_SELECTOR = '[role="article"]'
# A post permalink is the first anchor whose href looks like one of these.
_PERMALINK_HINTS = ("/posts/", "/permalink/", "story_fbid")
# ------------------------------------------------------------------------------


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


def _extract_permalink(article) -> Optional[str]:
    """First anchor in the post that looks like a permalink; cleaned of query
    junk. Returns None if none found (post still usable — permalink is a bonus)."""
    try:
        for a in article.query_selector_all("a[href]"):
            href = a.get_attribute("href") or ""
            if any(hint in href for hint in _PERMALINK_HINTS):
                if href.startswith("/"):
                    href = "https://www.facebook.com" + href
                return href.split("?")[0]
    except Exception:
        pass
    return None


def scrape_group(page: Page, url: str) -> list[dict]:
    """Open one group, scroll a few times, and return its visible posts.

    Each item: {"text": <cleaned inner_text>, "permalink": <url or None>}.
    Deduplicated by text WITHIN this group (FB repeats articles as you scroll).
    """
    page.goto(url, wait_until="domcontentloaded")
    # let the feed hydrate before the first scroll
    try:
        page.wait_for_selector(_ARTICLE_SELECTOR, timeout=15000)
    except PWTimeout:
        print(f"[scraper] no articles appeared for {url} (login expired? group layout changed?)")
        return []

    for _ in range(config.SCRAPER_MAX_SCROLLS):
        page.mouse.wheel(0, 2500)
        time.sleep(random.uniform(*config.SCRAPER_SCROLL_DELAY))

    posts: list[dict] = []
    seen_text: set[str] = set()
    for article in page.query_selector_all(_ARTICLE_SELECTOR):
        try:
            text = (article.inner_text() or "").strip()
        except Exception:
            continue
        # collapse whitespace-heavy FB chrome; skip tiny/empty fragments
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if len(text) < 40 or text in seen_text:
            continue
        seen_text.add(text)
        posts.append({"text": text, "permalink": _extract_permalink(article)})
    return posts
