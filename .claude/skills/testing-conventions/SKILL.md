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
pipe.) 565 test functions at last check, plus the docs-integrity ones.

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
- Offline always. No network, no live Gemini, no real browser. `conftest.py` adds the
  project root to `sys.path` so the flat modules import directly.

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
