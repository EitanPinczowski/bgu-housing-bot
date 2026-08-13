---
name: telegram-notes
description: >
  Reference notes on Telegram output: MarkdownV2 alerts, albums, group-vs-DM routing,
  the 64-byte callback_data cap and vote buttons, and the daily digests. Load before
  editing notifier.py, bot_listener.py, dm_digest.py, digest.py, or top_listings.py.
---

# Telegram Notes

How alerts and digests reach the user, and the caps that silently drop them.

> Moved verbatim from `CLAUDE.md`. Do not reword in place — see the
> `write-a-note` skill.

- `notifier.py` — Telegram MarkdownV2 alerts; group-vs-DM routing; albums; vote buttons.
  - **A VOTE BUTTON CARRIES A TOKEN, NEVER THE dedup_key.** Telegram caps `callback_data`
    at **64 BYTES** and answers BUTTON_DATA_INVALID by rejecting the **whole message**,
    not the offending button. A dedup_key is `phone|address` and Hebrew costs 2 bytes a
    character, so a descriptive address overflows — measured 2026-08-02, 16 of 417 keys,
    the longest 93 bytes. Because alerts are **batched**, one long address took a whole
    batch down: that run delivered **4 of 16** and the rest were lost with one line of log.
    `storage.callback_token` mints a stable 12-hex-char stand-in into `callback_tokens`;
    `bot_listener._resolve` reverses it and **falls back to a raw key**, because buttons
    posted before the change live in the chat forever. `_update_tally` must match through
    `_resolve` too — comparing `save|{key}` silently stops the counts updating.
    `_fit_callbacks` drops anything still over 64 bytes so a regression costs a button,
    not the alert.

- `top_listings.py` / `digest.py` / `dm_digest.py` — morning/evening top-N, recaps, DM digest.
  - **`doctor`'s FAILs RIDE THE DM DIGEST** (`dm_digest._health_section`), because every
    failure of 2026-08-04/05 — the wedged lock that ate 17 scheduled slots, the 22-hour
    stale dashboard, the runs that slept through their trigger — was found only because a
    person happened to run `doctor.py`. Nothing ever pushed. A check nobody reads is not a
    check; now silence means healthy rather than unobserved.
    - **FAIL only, never WARN.** A digest that cries daily is one you stop opening, which
      is the exact failure this is meant to fix rather than reproduce.
    - **A failure ALONE must be enough to send.** `build()` returned None when there was
      nothing unmapped, so on a quiet day a wedged scraper would have been reported by
      nobody — the health rows join that emptiness test.
    - It is wrapped and cannot suppress the rest: a health check that throws reports
      itself as a line and the unmapped-locations digest still goes out.
- `bot_listener.py` / `watchdog.py` — vote-button listener + DM-only `/search`
  (`query.py`) and `/status` commands; dependency health check.

## An alert that fails to send is OWED, not lost

`notifier` retried exactly one failure: a 400 from bad MarkdownV2, resent as plain text. A
**network** failure printed `[notifier] sendMessage … failed (status None)` and dropped the
alert — and `mark_seen_all` had already run, so the flat could never re-alert. Observed
2026-08-12 20:02, in the run where DNS was down: the listing reached SQLite and the Sheet;
the notification, which is the entire point of the bot, never happened.

- `notify()` returns **None** (nothing owed), **True** (sent) or **False** (owed and
  FAILED). It returned None either way, so a failed send was indistinguishable from a good
  one. `send_batch` likewise returns what actually went out — it used to return `k` even
  when every send failed.
- `storage.mark_alerted(key)` records a delivery; `storage.pending_alerts(hours)` lists
  what is still owed, and `main._retry_pending_alerts()` re-sends at the START of the next
  run — so a listing found by a previous run gets its alert even if this run finds nothing.
- **NO 'OWED' FLAG EXISTS**, deliberately. The gate is `status in (MATCH, NEEDS_DATA)` and
  `score >= MIN_ALERT_SCORE`, both already stored, so owed is recomputable as alert-worthy
  AND `alerted_at IS NULL`. One nullable column, no second source of truth to drift.
- **BOUNDED BY AGE** (`config.ALERT_RETRY_MAX_AGE_HOURS`, 24): a flat found two days ago has
  probably gone, and a late alert is worse than none — it trains you to ignore the channel.
- `sent 1 of 2` in the run log is usually NOT a failure: `send_batch` reports how many
  cleared the notifier's own gate, which is narrower than main's `alertable` list. Check
  `pending_alerts` before treating it as a lost alert.
