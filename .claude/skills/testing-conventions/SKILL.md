---
name: testing-conventions
description: >
  Write or fix a test in this repo without corrupting real operational data. Use when
  adding a test, a fixture, or a conftest entry; when a test touches storage, the LLM
  budget, DATA_DIR, or the scraper log; or for "add a test for this", "the suite is
  failing", "run the tests".
---

# Writing a test here

    python -m pytest -q

**Do not pipe it to `tail` or `head`** — that discards pytest's exit code, so a failing
suite reads as a passing one. Read the count, or drop the pipe. (`guard.py` blocks the
pipe.) 672 passing at last check, plus the docs-integrity ones.

## ⛔ The rule that exists because it was broken twice

**A test must not read or write real operational files.** The damage is quiet and it
outlives the test run.

### `data/llm_budget.json` — handled for you

`tests/conftest.py` has an **autouse** fixture repointing `llm._BUDGET_PATH` at `tmp_path`.
It is autouse and in conftest rather than in one module because *the module that needs it
is not always the module that writes it*. A test recording a fake refusal once left
`refused_at` and a synthetic error string in the real file, so `doctor` then advised a
budget change based on a value no provider ever sent — invisible while `LLM_DAILY_BUDGET`
was 900, and it only surfaced when the budget was lowered and **14 unrelated tests began
failing**.

The same fixture resets `llm._model_rung`, which is process state: a test that exhausts
the first model would otherwise leave every later test running on the second one.

### `data/geocode_cache.json` and the network — handled for you

Three more autouse fixtures in `tests/conftest.py`, added 2026-08-12 after two tests in
`test_geocode.py` were found geocoding against **live Overpass and Nominatim**. Each is
exercised against the thing it catches in `tests/test_offline_guards.py` — disable any one
and a test there fails.

- **`_no_test_may_reach_the_network`** blocks every `requests` verb. "Offline always" was
  already the rule; nothing enforced it. **A networked test here is not slow, it is
  WRONG**: the mirrors disagree, so the assertion tracks whichever replied. Measured over
  `pytest-randomly` seeds 1-7: **3 of 7 failed**, and one test flipped between runs at a
  fixed seed. After the fixtures, 8 of 8 seeds pass in the same 31.7 s — a constant runtime
  is itself the evidence that nothing is dialling out.
  A blocked call raises, and `geocode`'s tiers already swallow exceptions and fall through
  to local data, so the effect is exactly the `--local-only` mode CLAUDE.md names as the
  only trustworthy way to measure here. A test that needs a response still stubs `requests`
  itself and wins, because monkeypatch undoes in reverse order.
- **`_no_test_may_touch_the_real_geocode_cache`** repoints `geocode._CACHE_PATH` at
  `tmp_path` and resets `_cache` (module-level, loaded once per process — repointing the
  path alone leaves the first test's copy in memory for all the rest). `geocode_detailed`
  caches every answer it gets and `_save_cache` only refuses to SHRINK a cache, so
  **additions land silently in the production file** the live pipeline and every map dot
  read from.
- **`_no_test_may_leave_a_mirror_marked_dead`** clears `geocode._dead_mirrors`, a
  per-process circuit breaker with no invalidation. Correct in production, where a process
  is one scrape; wrong across tests, which pretend to be many. One mirror left dead turned
  an exact call-count assertion from 8 into 7.

**THE CACHE IS WHAT TURNS A FLAKE INTO A WALL.** The intermittent failure became permanent
the moment the suite wrote `אברהם אבינו, שכונה ד` (overpass) and `אברהם אבינו` (nominatim)
into the cache — **711 m apart, against a test asserting they agree within 300 m**. From
then on it failed every run and deleting the file was the only way back to green. If a
geocoding test starts failing on every run, look at the cache before you look at the code.

**A test that needs the network is usually testing the wrong thing.**
`test_the_proximity_word_must_govern_the_landmark` used `רגר 5`, a number we hold no local
record of, so it only ever passed because Overpass answered — `static` is the *correct*
offline verdict for a number we cannot place. Switching to `רגר 153`, a number in our own
data, tests the ranking rule instead of the mirror.

### `config.DATA_DIR` — you must patch this yourself

**Patch it FIRST, before the code under test can touch it.** `scraper._abort` appends an
`ABORT` line to `search_log.txt`; a test that does not patch `DATA_DIR` appends fake aborts
to the **real operational log**, which `stats.py` then counts. That happened — see the
comment at `tests/test_scraper_lock.py:322`.

```python
monkeypatch.setattr(config, "DATA_DIR", tmp_path)
```

### The database — use the fixture

```python
def test_something(temp_db):
    ...
```

`temp_db` points `config.DB_PATH` at a fresh SQLite file. Patching the attribute is enough
because **storage reads `config.DB_PATH` on every call**.

## House style

- One behaviour per test, named for the behaviour, not the function
  (`test_validate_catches_bad_price`, not `test_validate_2`).
- The docstring says **why the test exists** — the failure it prevents — not what the code
  does. Most of this suite's value is in the notes attached to a rule.
- Assert on the *reason*, not just the exception type:
  `except SystemExit as e: assert "TARGET_PRICE" in str(e)`.
- Offline always. No network, no live Gemini, no real browser — and this is now ENFORCED,
  not merely asked for: see the network fixture above. `conftest.py` adds the project root
  to `sys.path` so the flat modules import directly.

## The trap that makes a test agree with itself

**Never let a test read the switch it is gating.** `batch_ab.py` chunked by
`config.LLM_BATCH_SIZE`, which is 1 while batching is disabled — so it compared a single
call against a single call and printed PASS on both gates without batching anything. Pass
the value in explicitly (`--batch N`) instead.

The same shape shows up in guards: a check that can only ever say PASS is not a check.
When adding one, **prove it fails** on a deliberately broken input before trusting it.

## Before committing

    python -m pytest -q
    python -m ruff check .

`.pre-commit-config.yaml` also runs ruff, gitleaks, `detect-private-key`, and a 2 MB
added-file cap. Never bypass hooks with `--no-verify`.
