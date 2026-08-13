---
name: evidence-rules
description: >
  How to measure something in this project so the answer is trustworthy. Load before
  running an A/B, adding a benchmark or harness, quoting an accuracy number, choosing a
  sample size, reading a rate limit or usage dashboard, or concluding that a change made
  something better. Also when a measurement contradicts an earlier one.
---

# Measuring things here

Almost every expensive mistake in this project's history was a measurement that agreed
with itself. These rules are what the corpus has learned to demand.

## 1. Hold out, or the test grades itself against its own answer key

`geo_accuracy.py` hides each address's own anchor before asking the geocoder. **Without
the hold-out it grades itself against its own answer key** — the number would look
excellent and mean nothing.

## 2. The control must be a LIVE call in the same session

Not the archive. `posts.parsed_json` was written over days by **two different models**
(186 posts on 2026-08-03 came from the Ollama fallback) and by older prompts. Measured
against it, the address field "disagrees" 80% of the time and says nothing at all.

## 3. Never let a harness read the switch it is gating

`batch_ab.py` chunked by `config.LLM_BATCH_SIZE`, which is **1** while batching is
disabled — so it compared a single call against a single call and printed **PASS on both
gates without batching anything**.

> A test that reads the switch it is gating can only agree with itself.

Pass the value in explicitly (`--batch N`). The same shape appears in guards: a check that
can only say PASS is not a check. **Prove it fails** on a deliberately broken input before
trusting it.

## 4. Know the noise floor before reading a difference

Every A/B here runs each model **twice** — once for its answer, once against itself. 3.1
is ~100% self-consistent; 3.5 is 91% on rooms and **79% on address against itself**. That
floor is what retired an early scare from a 4-post smoke test where 3.5 appeared to "lose"
room counts.

A cross-model disagreement smaller than the self-disagreement is not a finding.

## 5. n=48 was not enough

**The clearest lesson of the whole exercise.** At n=48, 3.1 won every one of 5 price
disagreements and was promoted. At n=100 they disagree 15 times and **3.1 twice returned
the WHOLE FLAT'S rent as the per-room price**. n=48 had simply caught 3.1's good cases.
The promotion was reverted.

Before acting on a small sample, ask what the sample would look like if the opposite were
true.

## 6. Measure a cap; do not infer it from how often it complains

`GEMINI_MIN_INTERVAL_SEC` was left at 4.0 — exactly 15/min, zero headroom — on the
reasoning that 429s were under 1% of requests so the cap "is not being saturated". That
was wrong: the error rate was low only because the **daily** ceiling bit first. The Rate
Limit page showed a measured peak of **17**.

## 7. A usage dashboard shows where you have BEEN, never where the cap IS

`LLM_DAILY_BUDGET` was set to 900 "under the ~1,000/day observed ceiling", read off the
usage chart. The real limit was **500**, so the budget sat above it and could never bind.
**Follow what a refusal states** (`limit: 500`), not what a chart displays.

And **the ceiling is not stable**: on 08-04 the same model served ~687 requests under a
later 500 cap. Never hard-code a limit.

## 8. Distinguish a blip from a terminal condition — by retrying

Google often names no quota metric (both 08-05 refusals came back `unknown`), so parsing
PerDay/PerMinute cannot be relied on. **A retry that succeeds proves the refusal was
transient.** The string test is used only to SKIP retries when the error explicitly says
per-day, where waiting cannot help.

Earlier refusals that "kept climbing past" (252 → 259, 389 → 393) were per-minute blips —
reading them as the daily ceiling would have argued for cutting the budget to ~250.

## 9. Count the right denominator

`stats.py`'s reliability row counted `END|SKIP` together and so **read healthiest exactly
when runs were being lost** — 119% of target on a day when 5 of 11 were lock-held. A SKIP
is a run that did not happen. And a run that STARTS and never ENDS is a third loss that
neither END nor SKIP can see.

Likewise: broker detection counts from the **post archive**, not the listings table. An
agency whose flats are mostly out of zone otherwise looks like a private landlord — that
one choice moved it from 1 contact flagged to 7 of 136.

## 10. Check what fraction of the data can answer the question at all

**Two thirds of the archive cannot answer the latency question.** `posted_at` was
rewritten on every sighting while `first_seen` was not, so 1,968 of 3,027 rows were
dropped as impossible — silently, until someone looked. The surviving sample is *posts
published once*, and `stats.py` now says so.

Always report the n, and what was excluded.

## 11. If the measurement calls the network, you measured the network

Two `replay.py` passes minutes apart over the same 10,565 posts disagreed on **1,144
rows** — 736 `street_geom -> overpass` — while the code change under test accounted for
**116**. Reported as-is, that is a 10x overstatement of your own change.

The same shape has now bitten three times: the geo-accuracy harness (a 715 m "regression"
that was a different mirror), the test suite (3 of 7 seeds failing), and this. **The tell
is always that a re-run disagrees with itself.**

- Freeze the external tiers and compare like with like: `replay.py --frozen`,
  `geo_dump --local-only`. Both are verified byte-for-byte reproducible.
- When you cannot freeze, run the SAME comparison at both commits and diff the diffs —
  the shared noise cancels and what remains is your change.
- **Check the source column, not just the counts.** `('static','static_street'): 116`
  beside `('street_geom','overpass'): 736` is what separated signal from noise here; the
  totals alone looked like a catastrophe.

## 12. A number that matches your theory can still be a coincidence

The archive's impossible `posted_at` rows had a median overshoot of exactly **120 min**,
which is exactly the scrape interval — so the diagnosis "ages lost to the hover budget"
fit and felt confirmed. It was wrong. The **p90 of 170 min** is not a scrape interval, and
`3h - 0.2h` is: the real cause was a UTC/local clock mismatch, and the prediction
`overshoot = 3h - age` fits BOTH points.

One statistic agreeing is not evidence; a model that predicts the whole distribution is.
Before accepting a cause, ask what ELSE it predicts, and go and look at that.

## 13. Prefer the visible failure mode

When two options are both wrong sometimes, prefer the one that fails where a person will
see it. 3.1 inflates a price and the ≤2000 filter drops the flat **silently**; 3.5 returns
null and the flat lands in NEEDS_DATA where a human still sees it.

## 12. A measurement's opposite is also a finding

Record deliberate **no-changes** with the same rigour — `MIN_ALERT_SCORE` was audited and
left alone, and the note says what evidence *would* justify moving it. That is what stops
the question being reopened on the same data.

See `dead-ends` for the results these rules produced, and `write-a-note` for how to record
a new one.
