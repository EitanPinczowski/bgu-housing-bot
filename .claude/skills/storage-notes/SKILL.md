---
name: storage-notes
description: >
  Reference notes on SQLite persistence and the Google Sheets mirror: dedup identity,
  save_listing's COALESCE enrichment, orphan pruning, the post archive, and the grid-
  growth rule for the Sheet. Load before editing storage.py or sheets.py, or changing
  how listings are keyed or deduplicated.
---

# Storage Notes

What is stored, how a flat is identified, and the ways that has gone wrong.

> Moved verbatim from `CLAUDE.md`. Do not reword in place — see the
> `write-a-note` skill.

- **Dedup identity = phone + NUMBERED ADDRESS**, not the phone alone
  (`storage.make_dedup_key` / `is_duplicate`). The phone survives reposts, but a phone
  is not a flat: measured 2026-07-29, **42 numbers advertise more than one numbered
  address (one posts 32)**, and the old phone-only key collapsed **101 distinct flats**
  into single rows — every flat after a landlord's first was dropped as "already seen"
  and never alerted. Fixing it took 288→309 listings and 79→91 MATCH. Do not "simplify"
  this back to the phone. The asymmetry is deliberate: a read with **no** house number
  still collapses on the phone alone, because a vague re-read can't be told from a new
  flat and a wrong duplicate alert is the worse failure. No phone → content hash.

- `storage.py` — SQLite: dedup, listings, votes/marks, unknown-locations, fingerprints, post archive.
  - **A DEAD `dedup_key` IS NOT A DEAD FLAT** (`prune_orphan_listings`, 2026-08-06). It
    would have deleted **21 rows, 11 of them real** — `אלכסנדר ינאי 30` at score 98,
    `אברהם אבינו 11` at 93. Two faults:
    - It built the live-key set from `make_dedup_key` when a listing legitimately holds
      ANY key `dedup_keys` yields — 2,942 keys instead of 5,238, inventing 11 false
      orphans on its own.
    - The premise. "No parse reproduces this key" does not mean the flat is gone: the key
      FORMAT changed (`phone:X|street|number` → `phone:X|address`), retention nulls
      `parsed_json` on old posts, and a re-parse moves the key while the flat stays real.
      `ברוטנברג 13` — MATCH, score 83, with a phone — survives all three with no
      duplicate anywhere.
    - A row must now be **orphaned AND redundant** (another row represents the same flat,
      under the grouping `merge_duplicate_listings` uses). 21 → 2 on the live DB, and the
      per-flat count decrements as rows go so the LAST row of a flat is never taken.
  - **`replay.py` deletes by the NEW extract's keys**, so a post that flips to `NOT_AD`
    leaves the listing its old parse created — that is how `phone:522629429` survived its
    own re-parse and had to be removed by hand.
- `sheets.py` — optional Google Sheets sink (append, batch reconcile, sort, rebuild).
  - **`_write_rows` GROWS THE GRID FIRST.** Writing past the worksheet's row count fails
    with `Range (Sheet1!A393:T393) exceeds grid limits. Max rows: 392` — 4 times before
    this was fixed. `_retry` cannot help (not transient) and `save_listing` swallows
    failures by design, so listings silently stopped reaching the mirror while SQLite was
    fine. **Only ever grow:** `resize(rows=N)` below the current count DELETES rows, and
    those rows are listings.
