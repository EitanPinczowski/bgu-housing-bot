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

### THE CAUSE WAS A STALE TOOLTIP NODE (2026-08-13, third diagnosis and the right one)

Age capture fell **90% → 37% → 10%** across three full runs in one day. Two causes were
proposed and each was measured and refuted — the hover budget (233 of 800 spent) and local
contention (the 16:00 run had none and scored worst). The actual bug:

```js
document.querySelector('[role="tooltip"]')     // the FIRST tooltip in DOCUMENT ORDER
```

That is not the tooltip this hover popped. **FB leaves tooltips in the DOM as they fade**,
and a profile-name tooltip parses to `None` — so one stale non-date node answered every
subsequent read, for the rest of the post. Hovering harder could not help, which is why
the 16:00 run shows the paradoxical signature **2.8 hovers per post and 10% capture**:
maximum spend, near-zero yield.

The fix reads `querySelectorAll` and takes the first tooltip that parses to a date, and
also re-reads the anchor's own `aria-label`/`title` AFTER the hover — FB renders those
lazily too, exactly like the href, and they were only ever read before the hover, when FB
has not filled them in.

**SPEND IS NOT YIELD, AND ONLY SPEND WAS INSTRUMENTED.** `hovers=N/CAP` looked healthy
throughout. `scraper.age_sources()` now tallies where each age came from — page, hover, or
nowhere — and the run summary prints `post age: N/M captured (page · hover · none)` with a
`← HOVERING IS YIELDING NOTHING` marker. Any one of the three runs would have been
diagnosed immediately by that line.

**A TEST DOUBLE CANNOT CATCH A SELECTOR BUG, AND WILL LOOK LIKE IT DID.** `_TipsAnchor`
returns whatever list it was built with regardless of the script it is handed, so
reverting `querySelectorAll` to `querySelector` left the whole file green — verified by
reverting it. The Python half (try every tooltip, take the first that parses) IS pinned,
proved by reverting that instead. For the JS half the strongest available check without a
real browser is asserting the script text itself, which
`test_the_page_is_asked_for_every_tooltip_not_just_the_first` does. Worth knowing which
half of a browser fix your tests actually hold.

### DO NOT MEASURE A SCRAPE YOU ARE COMPETING WITH (2026-08-13)

**The run used to judge the raise was contaminated by my own commands, and the retraction
below is more useful than anything the run appeared to show.**

The 14:00 full run spent **305 of 800** and captured an age on only **37 of 101 posts
(37%)**, which read as "the cap was never the constraint, so the raise bought nothing".
That conclusion was wrong twice over.

**Capture is normally 68-93%, so 37% is an incident, not a baseline.** Per run, by local
hour: 08-11 19:00 → 54%, 08-12 11:00 → 93%, 08-12 14:00 → 68%, 08-12 18:00 → 93%,
08-13 10:00 → **90%**. The 35% that looked like a flat line came from comparing the 14:00
run against a 10:00 figure I had wrongly deflated — I confused *has* a `posted_at` with
*has a usable one*, subtracting the 63 rows the UTC/local clock bug had made impossible.
Those 63 were present-but-wrong, not missing. **Presence and correctness are different
questions and one measurement cannot answer both.**

**The shape tells you which fault it is. Group capture is bimodal — ~100% or ~0% — so
order the groups by visit time and look at WHERE the zeros sit:**

- **Zeros at the END, never recovering** = the budget ran out. 08-12 collapses from 14:48
  onward, 08-11's last group scores 5%. Both ran under the old 300 cap, so the note above
  was right and the raise did fix this — the 14:00 run's final groups scored 54-100% where
  300 would have left them dead.
- **Zeros in the MIDDLE, recovering afterwards** = contention, not budget. 14:00's dead
  block runs 14:13:47 → 14:31:44 and heals at 14:34:40, with 62% of the budget unspent.
  The window brackets my own work exactly: `geocode_cache.json` written 14:14:54, the
  dashboard published 14:30:07. The plausible mechanism is `SCRAPER_HOVER_WAIT_SEC` — a
  **fixed 0.6 s** wait for the tooltip to render, which heavy local CPU/IO can outrun. A
  post whose tooltip has not appeared yet yields no age however many hovers remain.

CLAUDE.md already forbids running the A/B harnesses during a scrape, for the LLM's
per-project RPM limit. **This is the same rule for a different shared resource — wall-clock
responsiveness — and it applies to anything heavy, including a dashboard publish or a
geocode warm.** `guard.py` enforces the LLM case; nothing enforces this one, so check
`python -c "import scraper; print(scraper.run_in_progress())"` before a long local job, and
discard any age-capture number measured across one.

The exit-condition fix that came out of this stands on its own logic rather than on that
measurement: `_hover_reveal` broke on the **link** alone, on a comment asserting the
timestamp anchor yields link and tooltip together. It does not — `_age_from_aria` returns
None for a profile-name tooltip or one that has not rendered — so a post whose first anchor
revealed a permalink was abandoned with `age` still None. Now `if link is not None and age
is not None`, still bounded by `SCRAPER_HOVER_MAX_PER_POST` (3), worst case ~3 hovers per
post against the ~2.2 measured. It should help most where the tooltip is slow, which is
exactly the contention case. Pinned both ways in `tests/test_scraper.py`
(`test_hovering_continues_until_the_age_is_found_not_just_the_link`,
`test_hovering_stops_as_soon_as_both_are_known`); the first fails with the fix reverted.
**Its value is unmeasured** — judge it on a run nothing else is competing with.
