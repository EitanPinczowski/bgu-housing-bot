---
description: Full dependency health check with remediation
---

```bash
python doctor.py
```

Every dependency, each FAIL carrying its own fix. Report what is broken and what it costs
— not just the row names.

Rows worth knowing how to read:

- **osrm** — a WARN, not a FAIL, by design: the bot still classifies via the straight-line
  estimate, but walk-time scores are degraded. It still blocks `replay --apply`.
- **scraper progress** — only meaningful while a run is live. The heartbeat file is never
  cleared on exit, so on an idle machine a stale heartbeat means nothing. This row
  consults `run_in_progress()` for exactly that reason.
- **llm budget** — FAILs when `LLM_DAILY_BUDGET` exceeds the limit Google last stated. The
  limit comes from a refusal (`limit: 500`), never from the usage dashboard, which shows
  where you have been and not where the cap is.
- **dashboard** — FAILs when `serve_dashboard.py`'s process start predates the files it
  serves. A long-lived server pins the code at start; one served a whole day of stale work
  while looking healthy. **Restart it after any change.**
- **wake timers** — the `BGU *` tasks ship with "wake the computer" OFF, which is the real
  cause of "why didn't it run". Fixed once by `setup_always_on.cmd` as Administrator.

If something is wedged or a run went missing, use the `health-triage` skill — a lost run
has four distinct causes and they need different fixes.
