---
name: fb-selectors
description: >
  Repair the Facebook scraper when it stops reading posts — empty results, zero posts per
  group, a changed FB DOM, broken selectors, a checkpoint or login wall, or post text that
  contains two different listings merged together. Use for "the scraper returns nothing",
  "FB changed their layout", "fix the selectors", "posts are coming through garbled".
---

# Repairing the FB reader

Everything fragile lives in one place: the **FRAGILE block** at `scraper.py:365`. Expect
periodic tuning — FB's DOM is unstable by nature, and this is designed around that rather
than against it.

## First, tell the two failure modes apart

**Zero posts, no error** → selectors. **A `FacebookBlock` exception** → a checkpoint or
login wall, which is a different thing entirely and must not be worked around.

    grep -c "FacebookBlock" data/scraper_runs.log

## Selector repair

The chain is deliberately multi-selector so one change does not take everything down:

```
_FEED_SELECTOR   = '[role="feed"]'
_STORY_SELECTORS = ('[role="feed"] > div', '[role="article"]', 'div[aria-posinset]')
```

Add a new candidate to `_STORY_SELECTORS` rather than replacing the list — the old ones
keep working for cached layouts and A/B buckets, and you cannot tell which bucket the
account is in from here.

Work against a **real logged-in browser**, non-headless, with the persistent profile. Do
not switch to headless cookie injection to make debugging easier; that is one of the
standing safety constraints.

## ⛔ A block wall is not a bug to route around

`FacebookBlock` (`scraper.py:462`) is raised when FB shows a checkpoint / login /
verification wall instead of the feed. `main.py` stops the run and warns you.

> **Do NOT retry into it.**

Detection is `_BLOCK_DOM_SELECTOR` — a password field where the feed should be. Do not add
CAPTCHA-solving or detection evasion beyond human-like pacing. The account is the user's
**only** Facebook account.

## One scraped block is not always one post

`_clean_story` — measured 2026-08-05: **404 of 6,502 archived posts (6%)** carry an
embedded author+age header, meaning the block ran on into the NEXT story, which has its
own price, address and phone. 32 live MATCHes, 20 NEEDS_DATA.

Mostly harmless — the tail is usually a comment and the right flat was still extracted —
but the reported case was a couple's *wanted* ad followed by a stranger's offer: the LLM
extracted the OFFER, so the listing showed the wanted-ad's text under the wanted-ad's
permalink.

- The boundary is the **author line + a bare relative age** (`3h`, `13h`) that FB renders
  above every story after the first. The post's own header does not survive cleaning in
  that shape — its timestamp is the CSS-scrambled single characters dropped just above —
  so a surviving pair marks the next story.
- **Index 0/1 is excluded**, or the cut eats the post itself and leaves an empty body.
- The `_TAIL_MARKERS` cut did not fire on the reported block because it had no "View more
  comments".
- **This fixes future scrapes only.** The 404 already archived keep their merged text;
  re-parsing them needs `replay.py --llm`, which spends Gemini quota. Un-reparsed merged
  posts are polluted input — do not read a model disagreement on one as model quality
  (see `prompt-tuning`).

## Verify a fix without a live run

    python manual.py           # paste a real post, type END — the risk-free entry point
    python main.py             # DRY RUN by default; --live is what commits and notifies

`main.py` without `--live` prints what it would process and touches nothing.

## Afterwards

    python stats.py            # did posts-read recover?
    python group_report.py     # is one group still returning nothing?

If posts are being read but nothing classifies, the fault is downstream — use
`health-triage` or `prompt-tuning`, not this skill.
