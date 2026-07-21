"""
Conservative Facebook group reader (increment 2).

Reuses the persistent login profile created by login.py — never touches your
password, never injects cookies. It only scrolls and reads: it does NOT post,
comment, message, react, or click anything interactive.

  open_browser()          -> (playwright, context) using the persistent profile
  scrape_group(page, url, already_seen=None) -> (list of post dicts, stats) for one group

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

import datetime as dt
import os
import random
import re
import time
from typing import Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

import config

try:
    import msvcrt          # Windows byte-range file lock (single-instance guard)
except ImportError:        # non-Windows: lock is a no-op (dev only)
    msvcrt = None


# --- single-instance lock -----------------------------------------------------
# TWO scraper/browser sessions on the SAME persistent Chrome profile deadlock
# (Chromium's profile lock) — this once hung a manual dry run against a scheduled
# --live run. Any process that opens the browser (main.py, link_backfill.py,
# login.py) must hold this exclusive lock first; a second one refuses to start.
# The OS releases the lock when the holder exits (even if killed), so it never
# goes stale.
_LOCK_PATH = config.DATA_DIR / "scraper.lock"
_lock_fh = None


def acquire_lock() -> bool:
    """True if we got the exclusive scraper lock; False if another session holds it
    (the caller should then exit WITHOUT opening a browser)."""
    global _lock_fh
    if _lock_fh is not None:
        return True
    if msvcrt is None:
        return True
    try:
        fh = open(_LOCK_PATH, "a+")
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)   # exclusive on byte 0
    except OSError:
        try:
            fh.close()
        except Exception:
            pass
        return False
    _lock_fh = fh
    return True


def release_lock() -> None:
    global _lock_fh
    if _lock_fh is None:
        return
    try:
        _lock_fh.seek(0)
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        _lock_fh.close()
    except Exception:
        pass
    _lock_fh = None

# ============================ FRAGILE: FB specifics ==========================
# Edit HERE when Facebook changes its DOM / UI strings.

_FEED_SELECTOR = '[role="feed"]'
# Each direct feed child is one post story. FB churns class names, so try a few
# post-container selectors in order and use the first that yields elements.
_STORY_SELECTORS = ('[role="feed"] > div', '[role="article"]', 'div[aria-posinset]')
_SCROLL_PX = 1100                            # small steps so posts render before we read
_MIN_POST_CHARS = 40                         # shorter than this = not a real post

# A post permalink is the first anchor whose href contains one of these. FB uses
# several formats: /groups/<id>/posts/<id>/, /permalink/, ?story_fbid=, and the
# newer /stories/<set>/<base64>/ form — cover them all. Tracking query junk
# (?comment_id=, __cft__, __tn__) is stripped by _permalink via split("?").
_PERMALINK_HINTS = ("/posts/", "/permalink/", "/stories/", "story_fbid", "/share/")
# Facebook rarely exposes a clean permalink anchor on a post, but the post's ID sits
# in OTHER anchors of the same feed unit — reaction/comment links (which we used to
# skip for carrying comment_id) contain /groups/{gid}/posts/{pid}/, and share links
# carry story_fbid={pid}. With the group id (the URL being scraped) we reconstruct
# the canonical permalink. These pull the ids out of any such href.
_GID_RE = re.compile(r"/groups/(\d+)")
_PID_RE = re.compile(r"/(?:posts|permalink)/(\d+)")
_STORYFBID_RE = re.compile(r"story_fbid=(\d+)")
_STORY_RE = re.compile(r"/stories/\d+")          # already a valid permalink form

# Post photos: the biggest <img> in the story is the apartment photo. Skip small
# avatars/emoji and non-photo CDN assets. Min side keeps out avatars (~40px).
_IMG_MIN_SIDE = 130
_IMG_SKIP = ("emoji", "/rsrc.php/", "static.xx", "safe_image")   # avatars/UI assets

# "See more" expander labels (English UI here; Hebrew fallbacks just in case).
_SEE_MORE_LABELS = ("See more", "See More", "ראה עוד", "הצג עוד", "עוד")

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

# Post age from the timestamp link's short relative text ("13h", "3d", "July 5").
# FB shows minutes/hours under 24h, then days, then a date — so the unit alone
# gives the age. The absolute date also sits in that link's aria-label, but the
# relative text is cleaner and locale-simpler.
_TS_UNIT_HOURS = {"s": 1 / 3600, "m": 1 / 60, "h": 1.0, "d": 24.0, "w": 168.0, "y": 8760.0}
_TS_REL = re.compile(r"^(\d+)\s*([smhdwy])\b", re.I)         # "13h", "3d", "45m"
_TS_NOW = re.compile(r"^(just now|now|a few seconds|a minute|an hour)\b", re.I)
_TS_DATE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d"
                      r"|yesterday", re.I)                    # "July 5" / "Yesterday"
# Absolute-date fallback from the timestamp link's aria-label, e.g.
# "Friday, July 17, 2026 at 11:19 PM". Relative text is preferred (it's timezone
# independent); this catches posts whose relative text didn't render.
_TS_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_TS_ABS = re.compile(r"([a-z]{3,})\s+(\d{1,2})(?:,\s*(\d{4}))?[^\d]*?"
                     r"(\d{1,2}):(\d{2})\s*([ap])m", re.I)
# =============================================================================

_NUM_RE = re.compile(r"^\+?\d[\d,]*$")       # like counts, "+5", "1,234"
_HEBREW_RE = re.compile(r"[֐-׿]")  # at least one Hebrew letter

# --- Facebook block / checkpoint detection (part of the FRAGILE surface) ------
# If FB decides the account looks automated it redirects to a checkpoint /
# login / "confirm it's you" page instead of the feed. Hammering that page is
# what escalates a soft warning into a hard block, so we detect it and ABORT the
# whole run (main.py alerts you to re-login) rather than scrolling a dead page.
_BLOCK_URL_MARKERS = ("/checkpoint", "login.php", "/login/", "login/?",
                      "two_step_verification", "/confirmemail", "/recover",
                      "account_disabled", "/help/contact")
# A visible password field means we were bounced to the logged-out login screen.
_BLOCK_DOM_SELECTOR = 'input[name="pass"], input[name="encpass"], input[type="password"]'


class FacebookBlock(Exception):
    """Raised when FB shows a checkpoint/login/verification wall instead of the
    feed. main.py stops the run and warns you — do NOT retry into it."""


def _blocked_reason(page) -> Optional[str]:
    """A human-readable reason if the page is a checkpoint/login wall, else None."""
    url = (page.url or "").lower()
    for m in _BLOCK_URL_MARKERS:
        if m in url:
            return f"redirected to {m}"
    try:
        if page.query_selector(_BLOCK_DOM_SELECTOR):
            return "login form present (session logged out)"
    except Exception:
        pass
    return None
# -----------------------------------------------------------------------------


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


def _age_from_aria(aria: str) -> Optional[float]:
    """Hours since the absolute date in a timestamp aria-label, or None.
    Compared against local now — the machine clock and FB's rendered time are
    both Israel time, so a few hours' slack at the boundary is the worst case."""
    s = (aria.replace(" ", " ").replace(" ", " ")
             .replace("‎", "").replace("‏", ""))
    m = _TS_ABS.search(s)
    if not m:
        return None
    mon = _TS_MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    day = int(m.group(2))
    year = int(m.group(3)) if m.group(3) else dt.datetime.now().year
    hour = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "p" else 0)
    try:
        when = dt.datetime(year, mon, day, hour, int(m.group(5)))
    except ValueError:
        return None
    return (dt.datetime.now() - when).total_seconds() / 3600.0


def _post_age_hours(story) -> Optional[float]:
    """Post age in hours from its timestamp link — see _permalink_and_age."""
    return _permalink_and_age(story)[1]


def _images(story, limit: int = 6) -> list[str]:
    """Up to `limit` apartment-photo URLs in the story, largest first — skipping
    avatars/emoji/UI assets and anything too small to be a real photo."""
    scored = []
    try:
        for img in story.query_selector_all("img"):
            src = img.get_attribute("src") or ""
            if not src.startswith("http") or any(s in src for s in _IMG_SKIP):
                continue
            box = img.bounding_box()
            if not box or box["width"] < _IMG_MIN_SIDE or box["height"] < _IMG_MIN_SIDE:
                continue
            scored.append((box["width"] * box["height"], src))
    except Exception:
        pass
    scored.sort(key=lambda x: x[0], reverse=True)
    seen, out = set(), []
    for _, src in scored:
        if src not in seen:
            seen.add(src)
            out.append(src)
        if len(out) >= limit:
            break
    return out


_CMT_DROP = {"Reply", "Like", "Facebook", "Follow", "See more", "Author"}


def _comments(story, limit: int = 4) -> str:
    """Text of the first few visible comments (people often post the price
    there). Comments are nested [role=article]s with an aria 'Comment by …'."""
    out = []
    try:
        for art in story.query_selector_all('[role="article"]'):
            if not (art.get_attribute("aria-label") or "").startswith("Comment"):
                continue
            lines = [l.strip() for l in (art.inner_text() or "").splitlines()
                     if l.strip() and len(l.strip()) > 1 and l.strip() not in _CMT_DROP]
            t = " ".join(lines)
            if t:
                out.append(t)
            if len(out) >= limit:
                break
    except Exception:
        pass
    return "\n".join(out)


def _expand_see_more(page) -> None:
    """Click visible "See more" links to expand truncated posts before reading.
    Bounded and best-effort — a failed/stale click is ignored."""
    for label in _SEE_MORE_LABELS:
        try:
            buttons = page.get_by_text(label, exact=True).all()
        except Exception:
            continue
        for btn in buttons[:12]:            # bound the number of clicks per pass
            try:
                btn.click(timeout=800, no_wait_after=True)
            except Exception:
                pass


def _clean_href(href: str) -> str:
    """Absolute URL with tracking/query stripped (drops __cft__/__tn__/comment_id/set)."""
    if href.startswith("/"):
        href = "https://www.facebook.com" + href
    return href.split("?")[0]


def _permalink(story) -> Optional[str]:
    """First anchor that looks like a post permalink (not a comment link), cleaned.
    The fallback used by _permalink_and_age; None if none found (permalink is a bonus)."""
    try:
        for a in story.query_selector_all("a[href]"):
            href = a.get_attribute("href") or ""
            if "comment_id" not in href and any(hint in href for hint in _PERMALINK_HINTS):
                return _clean_href(href)
    except Exception:
        pass
    return None


def _post_id(href: str):
    """(group_id, post_id) recovered from any post-bearing href, either None."""
    gid = (m.group(1) if (m := _GID_RE.search(href)) else None)
    pid = (m.group(1) if (m := _PID_RE.search(href)) else None)
    if not pid and (m := _STORYFBID_RE.search(href)):
        pid = m.group(1)
    return gid, pid


def _permalink_and_age(story, group_url: Optional[str] = None):
    """(permalink, age_hours) for one post. Facebook seldom exposes a clean
    permalink anchor (comment-less posts especially), but the post's ID sits in the
    feed unit's OTHER anchors — reaction/comment/share links — so we recover it and
    rebuild the canonical /groups/{gid}/posts/{pid}/ (gid known from the scraped URL).
    Preference: the timestamp anchor's own permalink href → reconstruct from the
    timestamp anchor's id → reconstruct from any anchor's id → a /stories/ link →
    the first hint anchor. age comes from the timestamp anchor as before."""
    link_ts = link_any = link_story = None
    ts_gid = ts_pid = any_gid = any_pid = None
    hover_cands = []                            # (priority, anchor) to hover if no link found
    age = None
    url_gid = _GID_RE.search(group_url or "")
    url_gid = url_gid.group(1) if url_gid else None
    try:
        anchors = (story.query_selector_all('a[role="link"]')
                   or story.query_selector_all("a[href]"))
        for a in anchors:
            href = a.get_attribute("href") or ""
            is_ts = False
            t = (a.inner_text() or "").strip()
            if t and len(t) <= 25:              # timestamps are short ("13h", "July 5")
                if (m := _TS_REL.match(t)):
                    age = int(m.group(1)) * _TS_UNIT_HOURS[m.group(2).lower()]
                    is_ts = True
                elif _TS_NOW.match(t):
                    age = 0.0
                    is_ts = True
                elif _TS_DATE.search(t):
                    age = 1e9                    # a bare date => older than any cutoff
                    is_ts = True
            if not is_ts:
                aria = a.get_attribute("aria-label") or ""
                if aria and (h := _age_from_aria(aria)) is not None:
                    age = h
                    is_ts = True
            # Hover candidate: the timestamp/permalink anchor renders its href lazily.
            # It has NO post id yet and isn't a profile/photo link; under Hebrew locale
            # its text is scrambled so we can't match it by text — identify it
            # structurally (a bare '#'/'?…' href) and hover it below if we find no link.
            if (not _post_id(href)[1] and "/user/" not in href
                    and "/photo" not in href and "fbid=" not in href):
                prio = -1 if is_ts else (0 if (not href or href[0] in "#?") else 1)
                hover_cands.append((prio, a))
            # a clean permalink anchor (best case, kept verbatim)
            hint = "comment_id" not in href and any(x in href for x in _PERMALINK_HINTS)
            if is_ts and link_ts is None and hint:
                link_ts = _clean_href(href)
            if link_any is None and hint:
                link_any = _clean_href(href)
            if link_story is None and _STORY_RE.search(href):
                link_story = _clean_href(href)
            # recover the post id from ANY anchor (incl. comment/reaction links)
            gid, pid = _post_id(href)
            if pid:
                if is_ts and ts_pid is None:
                    ts_gid, ts_pid = gid, pid
                if any_pid is None:
                    any_gid, any_pid = gid, pid
    except Exception:
        pass

    def _canon(gid, pid):
        gid = gid or url_gid
        return f"https://www.facebook.com/groups/{gid}/posts/{pid}/" if (gid and pid) else None

    # Reconstruct from the id FIRST — it survives query-based links like
    # permalink.php?story_fbid=… whose id _clean_href would strip. Fall back to a
    # clean hint anchor, then a /stories/ link, then the old first-hint capture.
    link = (_canon(ts_gid, ts_pid)
            or link_ts
            or _canon(any_gid, any_pid)
            or link_story
            or link_any)
    # Last resort: hover the timestamp-style candidates so FB fills in the lazily
    # rendered permalink href, then read it (best candidates — is_ts, then bare '?'/'#'
    # hrefs — first). Bounded per post and per run.
    if (link is None and group_url
            and getattr(config, "SCRAPER_HOVER_FOR_LINK", False)
            and _hover_used < getattr(config, "SCRAPER_MAX_HOVERS_PER_RUN", 0)):
        ordered = [a for _, a in sorted(hover_cands, key=lambda x: x[0])]
        hlink, hage = _hover_reveal(ordered, url_gid)
        link = hlink
        if age is None:                          # hover also fixes Hebrew-locale age
            age = hage
    return link, age


_hover_used = 0   # hovers spent this run (bounded by SCRAPER_MAX_HOVERS_PER_RUN)


def _hover_reveal(anchors, url_gid):
    """Hover up to SCRAPER_HOVER_MAX_PER_POST candidate anchors: FB populates the
    timestamp link's lazily-rendered permalink href AND pops a date tooltip. Returns
    (link, age_hours) — the first real link found, and the age parsed from the tooltip
    (its English date works even under he-IL, where the on-page timestamp is scrambled).
    Bounded/guarded — a flaky hover just yields (None, None) and the fallback stays."""
    global _hover_used
    per_post = getattr(config, "SCRAPER_HOVER_MAX_PER_POST", 3)
    run_cap = getattr(config, "SCRAPER_MAX_HOVERS_PER_RUN", 0)
    link = age = None
    for a in anchors[:per_post]:
        if _hover_used >= run_cap:
            break
        _hover_used += 1
        try:
            a.hover(timeout=1500)
            time.sleep(getattr(config, "SCRAPER_HOVER_WAIT_SEC", 0.6))
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        if age is None:                          # read the date tooltip this hover popped
            try:
                tip = a.evaluate("() => { const t = document.querySelector('[role=\"tooltip\"]');"
                                 " return t ? t.textContent : ''; }") or ""
            except Exception:
                tip = ""
            if tip:
                age = _age_from_aria(tip)         # a profile-name tooltip won't parse -> None
        gid, pid = _post_id(href)
        if pid:
            gid = gid or url_gid
            if gid:
                link = f"https://www.facebook.com/groups/{gid}/posts/{pid}/"
        elif "comment_id" not in href and any(x in href for x in _PERMALINK_HINTS):
            link = _clean_href(href)
        elif _STORY_RE.search(href):
            link = _clean_href(href)
        if link is not None:                     # the timestamp anchor gave link + tooltip
            break
    return link, age


def _stories(page):
    """Post-story elements from the first selector that returns any (DOM churn)."""
    for sel in _STORY_SELECTORS:
        try:
            els = page.query_selector_all(sel)
        except Exception:
            continue
        if els:
            return els
    return []


def _debug_shot(page, url: str, tag: str) -> None:
    """Save a screenshot to diagnose selector breakage vs a real block. Off unless
    config.SCRAPER_DEBUG_SCREENSHOTS."""
    if not getattr(config, "SCRAPER_DEBUG_SCREENSHOTS", False):
        return
    gid = url.rstrip("/").split("/")[-1].split("?")[0]
    try:
        page.screenshot(path=str(config.DATA_DIR / f"{tag}_{gid}.png"))
    except Exception:
        pass


def scrape_group(page: Page, url: str, already_seen=None):
    """Open one group and return (posts, stats) — its FRESH visible posts, newest-first.

    Each post: {"text", "permalink", "images", "comments", "age_hours"}. Deduplicated
    by text (falling back to permalink) WITHIN this group. Reads incrementally across
    scrolls because FB virtualizes the feed.

    `already_seen(text, url) -> bool` (passed on a live run): a post already processed
    in an earlier run is skipped here, so a group whose recent posts are all-seen bails
    fast. None (dry run) surfaces everything.

    `stats` = {"read", "age_skipped", "seen_skipped"} — distinct posts parsed, dropped
    as >24h old, and dropped as already-seen — for the per-run funnel.
    """
    if _SORT_CHRONOLOGICAL and "sorting_setting" not in url:
        url = url + ("&" if "?" in url else "?") + _SORT_PARAM

    page.goto(url, wait_until="domcontentloaded")
    # Bail immediately if FB bounced us to a checkpoint/login wall — never retry.
    blocked = _blocked_reason(page)
    if blocked:
        _debug_shot(page, url, "checkpoint")
        raise FacebookBlock(blocked)
    try:
        page.wait_for_selector(_FEED_SELECTOR, timeout=15000)
    except PWTimeout:
        # A wall can also appear as "no feed" — check once more before giving up.
        blocked = _blocked_reason(page)
        if blocked:
            _debug_shot(page, url, "checkpoint")
            raise FacebookBlock(blocked)
        print(f"[scraper] no feed appeared for {url} "
              "(login expired? not a member? group layout changed?)")
        _debug_shot(page, url, "debug")
        return []
    time.sleep(random.uniform(*config.SCRAPER_SCROLL_DELAY))  # let the feed hydrate

    collected: dict[str, dict] = {}
    read_keys: set = set()            # every distinct post parsed (for the funnel)
    age_skipped: set = set()          # dropped as >= the age cutoff
    seen_skipped: set = set()         # dropped as already processed in an earlier run
    # read, then scroll. Do at least MAX_SCROLLS passes, keep going (up to the hard
    # cap) until MIN_POSTS_PER_GROUP — but stop EARLY once scrolling stops turning up
    # new fresh posts (the feed is newest-first, so below that is all old/seen).
    passes = 0
    stale = 0                         # consecutive passes that added no new fresh post
    prev_fresh = 0
    while True:
        for story in _stories(page):
            try:
                raw = story.inner_text() or ""
            except Exception:
                continue
            text = _clean_story(raw)
            if len(text) < _MIN_POST_CHARS or not _HEBREW_RE.search(text):
                # Normally too-short / non-Hebrew text = not a post. BUT a post that
                # is a PHOTO of the ad text has only a tiny caption — keep it if it
                # has an image so the LLM can OCR it (bounded downstream by
                # SCRAPER_MAX_OCR_PER_RUN). Everything else is still skipped.
                if not (config.SCRAPER_OCR_IMAGE_ONLY and _images(story)):
                    continue
            # Key on the text (stable across scroll passes), not the permalink —
            # FB often renders a post's body before its timestamp/permalink
            # anchor. Backfill the permalink when a later pass exposes it.
            key = text[:80]
            read_keys.add(key)
            # Read the permalink AND age from the post's timestamp anchor in one
            # pass — that link IS the canonical permalink. Either can be None on an
            # early pass and get backfilled on a later one.
            link, age = _permalink_and_age(story, url)
            # age filter: skip posts we can READ as >= the cutoff. Because the
            # timestamp may render late, also drop one we'd already added if a
            # later read reveals it's old. Unknown age is kept, so a recent
            # listing is never lost to a missed timestamp.
            if (config.SCRAPER_MAX_POST_AGE_HOURS is not None
                    and age is not None and age >= config.SCRAPER_MAX_POST_AGE_HOURS):
                collected.pop(key, None)
                age_skipped.add(key)
                seen_skipped.discard(key)
                continue  # "1d"+ => 24h or older => outside the last 24h
            # already processed in an earlier run (live only): it would just be
            # re-dropped by the pipeline's dedup — skip it, and don't let it count
            # toward "fresh" growth, so an all-seen group stops scrolling fast.
            if already_seen is not None and already_seen(text, link):
                collected.pop(key, None)
                seen_skipped.add(key)
                age_skipped.discard(key)
                continue
            age_skipped.discard(key)          # it's fresh after all (late-render)
            seen_skipped.discard(key)
            imgs = _images(story)
            cmts = _comments(story)
            entry = collected.get(key)
            if entry is None:
                collected[key] = {"text": text, "permalink": link,
                                  "images": imgs, "comments": cmts, "age_hours": age}
            else:  # backfill fields that render / expand on a later pass
                if entry["permalink"] is None and link:
                    entry["permalink"] = link
                if len(imgs) > len(entry.get("images") or []):
                    entry["images"] = imgs        # keep the richest photo set seen
                if len(cmts) > len(entry.get("comments") or ""):
                    entry["comments"] = cmts
                if len(text) > len(entry["text"]):   # See-more expanded it later
                    entry["text"] = text
                if entry.get("age_hours") is None and age is not None:
                    entry["age_hours"] = age         # backfill a late-rendered time
        passes += 1
        fresh = len(collected)
        if fresh > prev_fresh:                 # this pass turned up new fresh posts
            stale, prev_fresh = 0, fresh
        else:
            stale += 1
        enough = fresh >= config.SCRAPER_MIN_POSTS_PER_GROUP
        stalled = (passes >= config.SCRAPER_MIN_SCROLLS_BEFORE_STOP
                   and stale >= config.SCRAPER_STOP_AFTER_STALE_PASSES)
        if (passes >= config.SCRAPER_SCROLL_CAP
                or (passes > config.SCRAPER_MAX_SCROLLS and enough)
                or stalled):
            break
        # Expand truncated posts AFTER reading, so the permalink/image are read
        # from the stable DOM first (clicking disrupts it) and the fuller text is
        # picked up on the next pass.
        if config.SCRAPER_EXPAND_SEE_MORE:
            _expand_see_more(page)
        page.mouse.wheel(0, _SCROLL_PX)
        time.sleep(random.uniform(*config.SCRAPER_SCROLL_DELAY))

    if not read_keys:                 # feed loaded but NOTHING parsed — likely a
        _debug_shot(page, url, "debug")   # selector break; screenshot to diagnose
    stats = {"read": len(read_keys), "age_skipped": len(age_skipped),
             "seen_skipped": len(seen_skipped)}
    return list(collected.values()), stats
