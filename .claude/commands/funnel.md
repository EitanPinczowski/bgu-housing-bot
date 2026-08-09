---
description: Show the pipeline funnel, run reliability, and time-to-detect
---

```bash
python stats.py
```

Summarise what changed and what it means. Read these rows carefully rather than repeating
them:

- **reliability** — a `SKIP` is a run that did NOT happen, and is counted as a loss. So is
  a `START` with no `END`. The designed ~1-in-8 `random human-like skip` is counted apart.
  If completions are well under target, the problem is lost runs, not cadence — use the
  `health-triage` skill, and do **not** propose rebalancing the schedule.
- **time-to-detect** — always read the `n` beside it. Posts published 19:00–23:00 have a
  500–680 min median because night runs are forbidden; that is structural, not a bug.
- **alert gate** — the histogram and where `MIN_ALERT_SCORE` sits in it. This was audited
  and deliberately left alone; it only moves on vote data, not on the score shape.
- **unusable share** — rows whose `posted_at` was overwritten and so cannot answer latency
  questions. Forward-only fix; the share should fall over time.

For per-group yield:

```bash
python group_report.py
```

Do not drop a group on one bad median — the worst cluster on record was a single wedged
run reading a backlog, not a posting pattern.
