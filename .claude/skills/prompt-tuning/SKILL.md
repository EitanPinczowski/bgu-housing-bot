---
name: prompt-tuning
description: >
  Change the Hebrew extraction prompt, or compare Gemini models, with a measured gate
  instead of a hunch. Use for "the LLM is missing prices", "fix the price division rule",
  "should we switch models", "reorder the ladder", "run model_ab", "run batch_ab", or any
  edit to `_SYSTEM_HE` / the extraction schema in llm.py.
---

# Tuning the prompt or the model

## The open problem

**The PRICE DIVISION RULE is the real open problem.** The n=100 A/B (2026-08-07,
`data/model_ab/`) showed **neither model reliably divides a total rent by the number of
residents** — which is what `_SYSTEM_HE` states. 3.5 usually answers null; 3.1 sometimes
answers the whole flat's rent as the per-room price. Both lose flats; 3.1's more quietly,
because an inflated price is then dropped silently by the ≤2000 filter while a null lands
in NEEDS_DATA where a person still sees it.

**Tightening that one prompt line is worth more than any model swap.**

## Before you run anything

**Never run the harness while a scrape is running.** Pacing is per PROCESS, the RPM limit
is per PROJECT, so two writers issue ~27/min against a limit of 15. `guard.py` blocks it.

    python -c "import scraper; print(scraper.run_in_progress())"

Check the budget too — the quota resets at **10:00 Israel time**, not midnight, so the
08:00 run always spends the previous day's leftovers.

## Running the A/B

The quota is **per project per model**, and the two windows are rarely both full at once,
so ask each model on its own schedule. The posts and prompt are pinned by the sample file,
so the phases still compare the same thing:

    python model_ab.py sample  [n_per_bucket]
    python model_ab.py ask     gemini-3.1-flash-lite
    python model_ab.py ask     gemini-3.5-flash-lite
    python model_ab.py report  gemini-3.5-flash-lite gemini-3.1-flash-lite

Each `ask` runs the model **twice** — once for its answer, once for its own noise floor.
A disagreement only means something measured against how much a model disagrees with
itself.

**The gate:** no post may flip `is_apartment_ad`, and no MATCH-eligible post may lose its
price, rooms, or address beyond what the control loses against itself.

## Rules that were learned the hard way

- **A 48-post A/B is not enough to reorder the models.** At n=48, 3.1 won every one of 5
  price disagreements. At n=100 they disagree 15 times (13 with 3.5 self-consistent, so
  not noise) and **3.1 twice returned the WHOLE FLAT'S rent as the per-room price** —
  2,800 where 3.5 said 1,400 for 2 roommates. n=48 had simply caught 3.1's good cases.
  The promotion was reverted.
- **Prefer the visible failure.** 3.1 inflates and the flat vanishes; 3.5 returns null and
  the flat reaches NEEDS_DATA where a person sees it.
- **The control is a LIVE call in the same session, not the archive.** `posts.parsed_json`
  was written over days by two different models and older prompts. Measured that way the
  address field "disagrees" 80% of the time and says nothing.
- **Run the harness at `--batch N`, never at `config.LLM_BATCH_SIZE`.** `batch_ab.py` used
  to chunk by the config knob, which is 1 while batching is disabled — so it compared a
  single call against a single call and printed PASS on both gates without batching
  anything. **A test that reads the switch it is gating can only agree with itself.**
- **The address column means little.** 79% self vs 65% cross is mostly `רחוב X` vs `X` and
  `שכונה ב` vs `שכונה ב׳` — same place. 3.5 is only 79% self-consistent on address and 91%
  on rooms; that noise floor is what retired an early scare from a 4-post smoke test.
- Some disagreements are **polluted input**, not model quality: the 346 un-reparsed MERGED
  posts carry two stories' text (see `fb-selectors`).

## What is already settled — do not re-open without new evidence

- **Batching stays OFF** (`LLM_BATCH_SIZE = 1`). It fails its accuracy gate: price 80%,
  rooms 70%, address 70% against a 100/100/85% single-call floor. Price and rooms agree
  *perfectly* call-to-call, so the drop is batching, not variance — and they are the
  fields the hard filters run on.
- **The model ladder order is 3.5 then 3.1.** Only the ORDER was reverted; 3.1 stays the
  reserve rung, which is where the doubled daily capacity comes from.
- **Pin the model, never `-latest`.** The alias moves, and a silent swap changes what is
  extracted from thousands of posts.

See the `dead-ends` and `evidence-rules` skills before proposing a new experiment.
