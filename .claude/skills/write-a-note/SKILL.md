---
name: write-a-note
description: >
  Record a finding in CLAUDE.md or a skill so it survives and actually prevents the
  mistake again. Use when writing up what was just learned, adding a "do not do X" rule,
  documenting a measurement or a dead end, updating the project notes, or deciding where
  a new note belongs.
---

# Writing a note that holds

This project's notes work because they follow a shape. Match it — a note in the wrong
shape reads like an opinion and gets relitigated on a hunch six weeks later.

## The shape

1. **The rule, in bold, as an imperative or a flat statement of fact.**
   `**A DEAD dedup_key IS NOT A DEAD FLAT**`, not "we should be careful about pruning".
2. **The measurement, with its date.** `21 rows, 11 of them real`; `measured 2026-07-29,
   42 numbers advertise more than one numbered address (one posts 32)`. A number without a
   date cannot be re-checked or expired.
3. **The failure mode** — what actually went wrong, concretely, including the cost.
   *"every flat after a landlord's first was dropped as 'already seen' and never alerted"*.
4. **Why the obvious alternative is wrong.** This is the part that stops the note being
   undone. `_names_a_street` has two conditions and the note says **both are load-bearing,
   each learned by breaking the other**.
5. **The counter-example, kept.** `הבלוק` is why `_AREA_KEYS` cannot be trusted;
   `מגדלי דוד, סורוקה` is why `names_only_a_landmark` must stay inside the `has_location`
   branch. A rule without its counter-example gets "simplified".

## Say what was NOT done, and why

Half the value here is negative results. Give a dead end the same rigour as a change:

> **A CHEAP PRE-LLM TEXT GATE IS A MEASURED DEAD END — do not retry** (2026-08-06, over
> all 6,939 archived posts). … | text < 40 chars | 192 (2.8%) | **35** |

Include the table. "We tried it and it didn't work" is not reusable; the numbers are.

Record a **deliberate no-change** too. `MIN_ALERT_SCORE` was audited and left alone, and
the note says why *and* what evidence would justify moving it — that is what stops the
question being reopened on the score shape alone.

## Where a note belongs

| the note is about | put it in |
|---|---|
| a standing rule that constrains every session | `CLAUDE.md` — `Key decisions` or `SAFETY CONSTRAINTS` |
| what is open right now | `CLAUDE.md` — `OPEN RIGHT NOW`, decisions only |
| how one module works | the matching `*-notes` skill |
| a procedure with steps | the matching workflow skill |
| something tried and rejected | `dead-ends` |
| how a claim was measured | `evidence-rules` |

**State, not decisions, belongs in neither** — listing counts, quota used, whether OSRM is
up are printed by `.claude/hooks/session_start.py`. A hand-dated status block drifts:
`OPEN RIGHT NOW` said 2026-08-06 while commits had landed on 08-07 and 08-08.

## Ask what PROCEDURE changed, not only what fact you learned

The table above places a note by TOPIC, which is easy for a fact and easy to forget for a
procedure — so reference notes stay current while the workflow skills rot. Measured
2026-08-13: eleven commits in one session put findings in `CLAUDE.md` (3) and
`geocoding-notes` (1) and **nothing anywhere else**, leaving `apply-replay` with no mention
of `--frozen` or `full_replay.py`, `health-triage` unaware that OSRM now self-heals, and
`telegram-notes` unaware that a failed alert is retried.

**A stale workflow skill is worse than a missing one**: someone loading `apply-replay`
would have followed it, run a non-reproducible apply, and never learned the safe command
exists. A missing note leaves you to think; a wrong one stops you thinking.

So after recording a finding, ask: **would anyone following the old procedure now do the
wrong thing?** If yes, that skill is part of the change, not follow-up work.

## The rules for moving a note

- **Do not reword while moving.** `.claude/tools/split_check.py` matches the frozen
  baseline verbatim and will fail. Move in one commit, reword in the next, so the reword
  is reviewable on its own.
- **Every skill needs a pointer line in `CLAUDE.md`** under `## Where the rest lives`.
  `tests/test_docs_integrity.py` enforces both directions — an unpointed skill is
  invisible, a dangling pointer is a lie.
- **Never delete a note to resolve a contradiction.** Two notes that disagree mean
  something changed; say which superseded which and when. `MAX_RUN_MINUTES` explicitly
  records that it *reverses* the decision above it, and names the two mechanisms that made
  the reversal safe — so if either is weakened, the reader knows to raise the ceiling
  again.

## After writing

    python .claude/tools/split_check.py
    python -m pytest tests/test_docs_integrity.py -q
