---
description: Is it safe to write the DB right now? (scrape, OSRM, quota)
---

Report whether a destructive write (`replay.py --apply`, `warm_cache.py`, an A/B harness)
can run right now. Run this and interpret the result — do not just paste the output.

```bash
python doctor.py
```

Then the three things that gate a write specifically:

```bash
python -c "import scraper, osrm, dates, llm, config; print('scrape running:', scraper.run_in_progress()); print('osrm alive:', osrm.alive()); print('window:', dates.quota_window()); [print(' ', m, llm.budget_state(m)) for m in config.GEMINI_MODELS]"
```

Read it as:

- **scrape running `True`** → do not write. Disable the tasks first (`apply-replay` skill).
- **scrape running `None`** → the OS could not be asked. Treat as unsafe for a write.
- **osrm alive `False`** → `--apply` would bake the straight-line walk estimate into every
  tier and score. Fix it first (`osrm-docker` skill).
- **budget at or near `LLM_DAILY_BUDGET`** → anything with `--llm` will fall through to
  Ollama and grind. The window resets at 10:00 Israel time, not midnight.

Say plainly whether it is safe, and if not, which condition blocks it and what fixes it.
