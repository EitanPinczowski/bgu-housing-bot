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
  - **A TOWER IS NOT A DUPLICATE** (2026-08-12). `אלכסנדר ינאי 17` holds **10 listings
    from 10 DIFFERENT posts across 6 groups** — studios, a 3-room, one on the 12th floor,
    in a building the posts call a מגדל with two lifts. Merging them would destroy 9 real
    ads. `merge_duplicate_listings` now skips any address with
    `>= config.MULTI_UNIT_MIN_POSTS` distinct posts (`storage.is_multi_unit`), and PRINTS
    what it skipped. 16 addresses qualify today.
    - **THE THRESHOLD IS 15, NOT 4 LIKE THE BROKER RULE, and picking 4 by analogy would
      have broken both directions.** Measured over the archive: real duplicates sit at
      **2-6** distinct posts (`השלום 67` 2, `רגר 93` 4, `רגר 162` 6) while towers sit at
      **30-61** (`סמטת קדש 22` 30, `אלכסנדר ינאי 17` 35, `אלכסנדר ינאי 32` 54,
      `אברהם אבינו 10` 61). A threshold of 4 would have blocked every true duplicate the
      function exists to remove — and still not caught `הורקנוס 45` (3 posts), the case
      that actually deletes a real flat. **Post count separates towers; it cannot see the
      hazard below.**
  - **A CONTACTLESS ROW JOINS THE ONE LANDLORD ONLY IF IT DOES NOT CONTRADICT THEM.**
    `_by_landlord` absorbed every orphan into a lone cluster, and at `הורקנוס 45` that
    put one landlord's 3.5-room furnished flat at **1300**/room together with a phone-less
    3-room-with-balcony at **1250** — two flats in one building, five days apart.
    `_collapse` ranks the phone-keyed row richer and would have deleted a MATCH at
    score 88. `_price_conflict` now refuses that.
    - **PRICE ONLY.** It is the one core fact a landlord states outright. Room counts are
      unusable here — the model omits `available_rooms_count` on ~20% of posts, so a
      null-vs-2 gap is an extraction miss, not evidence, and gating on it would refuse
      `השלום 67` and `רגר 162`, which ARE duplicates that differ only in what the thinner
      read managed to extract.
    - With both guards, a blanket `merge_duplicate_listings()` on the live DB predicts
      **one** removal — the `רגר 93` duplicate — instead of silently taking a real flat
      with it. That is what makes the tool safe to run again.
- `sheets.py` — optional Google Sheets sink (append, batch reconcile, sort, rebuild).
  - **`_write_rows` GROWS THE GRID FIRST.** Writing past the worksheet's row count fails
    with `Range (Sheet1!A393:T393) exceeds grid limits. Max rows: 392` — 4 times before
    this was fixed. `_retry` cannot help (not transient) and `save_listing` swallows
    failures by design, so listings silently stopped reaching the mirror while SQLite was
    fine. **Only ever grow:** `resize(rows=N)` below the current count DELETES rows, and
    those rows are listings.
