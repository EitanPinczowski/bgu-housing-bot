---
name: apply-replay
description: >
  Re-classify stored listings offline and write the results back with `replay.py --apply`.
  Use after changing the green zone, MAX_WALK_MINUTES, fit.py, a config threshold, the
  geocoder, or the LLM prompt — or when the user says "apply the replay", "re-classify
  the listings", "rebuild the verdicts", "push the change through to the listings".
---

# Applying a replay

`--apply` rewrites verdicts, tiers, scores and walk times for **every** stored listing
and rebuilds the Sheet. It is destructive, and it has two preconditions that do not
announce themselves.

`.claude/hooks/guard.py` blocks the command when either fails. Do not reach for
`BGU_SKIP_GUARD=1` to get past it — the block is the mechanism working.

## 1. Verify the base

    python doctor.py

Answers both preconditions in one command.

**OSRM must be PASS.** It is a Docker container, and a replay without it *silently*
substitutes the straight-line walk estimate. The AMBER boundary IS a walk time, so
applying while it is down bakes the approximation into every tier and score. If it is
down, load the `osrm-docker` skill.

## 2. Find a gap — this is the hard part

A run starts on the hour all day. **Waiting politely for a free lock failed twice.**

    python -c "import scraper; print(scraper.run_in_progress())"

If that says `True`, **disable the tasks, apply, re-enable**:

    schtasks /Change /TN "BGU Housing Scraper" /DISABLE
    schtasks /Change /TN "BGU Housing Scraper Hot" /DISABLE

`BGU Housing Scraper Hot` needs an **elevated shell** — from a normal one it returns
"Access is denied", and it will look like it worked.

Why it matters: both processes write the same SQLite, and a collision leaves the DB
half-rewritten. On 2026-08-05 an 18:00 full run held the lock for 90+ minutes at ~2 min
per post because it had fallen through to the local model.

## 3. Preview, and actually read the diff

    python replay.py

**Always read this before `--apply`.** Only a replay diff caught the "near the
university" regression: dropping the university from `_LANDMARKS` was half a fix, the
phrase then fell through to Overpass, which answered with a point *outside* the campus
polygon — so the no-housing mask missed it too — and two listings came back as AMBER
MATCHes.

## 4. Apply, then re-enable

    python replay.py --apply
    schtasks /Change /TN "BGU Housing Scraper" /ENABLE
    schtasks /Change /TN "BGU Housing Scraper Hot" /ENABLE

Re-enabling is not optional. A forgotten disable costs every scheduled run, silently,
until somebody notices the listings have stopped arriving.

## Useful flags

- `--only-merged`, `--only-imprecise`, `--only-bare`, `--min-score N` narrow the set.
- `--llm` re-parses through Gemini. It **spends quota** and is blocked while a scrape
  runs (pacing is per PROCESS, the RPM limit is per PROJECT — two writers issue ~27/min
  against a limit of 15).

## Known sharp edges

- **`replay.py` deletes by the NEW extract's keys.** A post that flips to `NOT_AD`
  leaves behind the listing its old parse created. That is how `phone:522629429`
  survived its own re-parse and had to be removed by hand.
- **`save_listing` ENRICHES, never replaces.** Every nullable column is written as
  `COALESCE(new, old)`, so a thinner later read (the LLM missed the price this time) can
  only add detail. The recomputed verdict/tier/score/walk *does* overwrite — that part
  is meant to be fresh. It also no longer resets `first_seen`.
- A `⭐` cannot rescue a placeless flat: `_classify` uses the **raw** score, not the
  voted one, so a replay gives the same answer whatever the group has clicked.

## Afterwards

    python stats.py

Check the funnel moved the way the change predicted. If it did not, the change did
something other than what you thought — read the diff again rather than re-applying.
