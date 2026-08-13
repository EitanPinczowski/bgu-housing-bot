---
name: scraper-volume
description: >
  Change how much or how often the Facebook scraper reads — cadence, schedule, group
  list, scroll depth, the hot pass, MIN_POSTS_PER_GROUP, MAX_SCROLLS. Use for "scrape
  more often", "find listings faster", "reduce detection lag", "add a group", "run it at
  night", "increase the depth", or editing update_schedule.cmd / FB_GROUPS.
---

# Changing scraper volume or cadence

**Read this before changing any of it.** Volume is not a free parameter here: the user
has **only their personal Facebook account**, no burner, and automated reading of FB
groups violates FB's ToS and risks suspension.

> Do not raise volume/cadence further without an explicit, informed request.

That line is a standing constraint in `CLAUDE.md`, not advice. Current settings were
already chosen after a clear high-risk assessment.

## Where it stands now

`SCRAPER_SCAN_ALL_GROUPS=True` (all 14 groups every run, after 3 zero-match groups were
pruned), `MIN_POSTS_PER_GROUP=20`, `MAX_SCROLLS=15` / `SCROLL_CAP=25`, **6 full runs**
(08/10/14/16/18/20) + **4 hot runs** (12/15/17/19) = **1,626 reads/day**.

What keeps each run's footprint down is the **early-stop**: the feed is newest-first, so a
group stops scrolling once it turns up no more *fresh* posts — fresh meaning within
`SCRAPER_MAX_POST_AGE_HOURS` (24h) AND not already processed. So the 2nd–7th runs of a day
are shallow, which is what makes this comparable in total work to the old 4×/day deep
scans.

## The invariant: a hot pass is PAID FOR out of full runs

`update_schedule.cmd` dropped the dead noon slot to fund the hot passes — **1757 → 1626
reads/day, −7.5%**. If you add a hot run, take the budget from somewhere.

Re-check with:

    python group_report.py     # per-group yield
    python stats.py            # funnel, runs/day, time-to-detect — all with their n

## ⛔ Before you touch the schedule: the lag is LOST RUNS, not cadence

Measured before changing anything, 7 days to 2026-08-05:

- **20 of 42 scheduled full runs completed (48%)**, with **17 slots LOST to a held lock**
  and 7 more that started and never ended.
- A slot that never runs cannot be fixed by moving the slots around.
- The three lock repairs (bounded teardown, the self-watchdog, `start_keep_awake`) all
  landed 08-04/08-05 — **after almost all of that data**.

**Re-measure over clean days before touching cadence.** The "trim productive groups to pay
for a hot pass" trade-off is not needed if the scheduled runs simply happen. Use the
`health-triage` skill first.

## What is structural and cannot be fixed by scheduling

- **The overnight gap.** Posts published 19:00–23:00 (27% of the usable sample) have a
  500–680-minute median because **night runs are forbidden**. Afternoon posts sit at
  45–139 min. Nothing to fix here without breaking the daytime-only rule.
- **Two thirds of the archive cannot answer latency questions at all.** `posted_at` was
  rewritten on every sighting while `first_seen` is not, so a landlord reposting the same
  text pushed `posted_at` past `first_seen` — 1,968 of 3,027 rows dropped as impossible.
  Fixed at the source (`record_post` keeps the EARLIEST), but **forward-only**: rows
  already overwritten have genuinely lost the original publication time.
- **Don't drop a group on one bad number.** Group `138595033004411`'s 1,066-min median was
  a single wedged run that slept until 09:15 and read a backlog, not its posting pattern.

## The rules that do not move

- Non-headless, **persistent real browser profile**; log in once manually. Not headless
  cookie injection.
- Long randomized delays, +up to 25 min jitter per scheduled run so it isn't clockwork.
- **Daytime only. No night runs.**
- **Dry-run by default** — only commit/notify with `--live`.
- Read-only: never posts, comments, messages, or interacts. Only scrolls and reads.
- **No CAPTCHA-solving and no detection evasion** beyond human-like pacing.
- `FacebookBlock` aborts a run on a checkpoint/login wall and **never retries into it**.
- Facebook is the **only** source. Yad2 was evaluated and rejected — every endpoint sits
  behind Radware Bot Manager, so the only ways in are forbidden, and it would risk the
  **home IP the FB scraper depends on**.

## Hovering is run DURATION, not scrape volume — and it is what captures post age

`SCRAPER_MAX_HOVERS_PER_RUN` bounds how many timestamp anchors a run may hover. **A hover
is the only way to read a post's age under he-IL**: the on-page timestamp is CSS-scrambled
and the date lives in a tooltip.

It is NOT covered by the volume rules above. A hover is mouse movement over already-loaded
content — it loads no page and reads no extra post. What it costs is ~0.7 s each, i.e. run
length.

**RAISED 300 -> 800 on 2026-08-13, at the user's explicit request.** At 300, with up to 3
tries per post, the budget ran out partway through runs reading 262-390 posts and every
later post was archived with **no age at all** — 10-35% a day, which is much of why
`stats._detection_lag` runs on a fraction of the archive.

**Size it from a run, not from a guess.** The cap was raised on an estimate because
`_hover_used` was private and nothing reported it. Both the run summary and the search log
now carry `hovers=N/CAP`, and the summary says so when the cap is hit:

    hovers: 800/800   ← BUDGET EXHAUSTED: posts after this point have no age

Read that line before changing the number again.
