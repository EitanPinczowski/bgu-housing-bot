---
name: data-recovery
description: >
  Restore data/listings.sqlite from a backup, or recover from a corrupted / half-written
  database. Use for "the DB is corrupt", "restore yesterday's backup", "database disk
  image is malformed", "two scrapers wrote at once", "I lost the votes", "roll the
  database back", or when doctor's db row FAILs and tells you to restore from
  data/backups/.
---

# Restoring the database

**`doctor` has been telling you to do this for months and nothing said how.** `_check_db`'s
remediation is *"restore from data/backups/"*, and `_check_backups`' own docstring calls
that advice *"worth exactly as much as the newest file in there"*. This is the procedure.

**The ordering below is the whole point.** Restoring while something is still writing
recreates the corruption you are repairing, and the tempting first move — deleting the
broken file — throws away the only copy of everything newer than the snapshot.

## What is actually at stake

Facebook can give the listings back. Nothing can give these back:

| table | rows (2026-08-14) | re-derivable? |
|---|---|---|
| `marks` | 5 | **NO** — the group's ⭐/🗑 votes. `MIN_ALERT_SCORE` is waiting on ≥20 of these and has been for weeks; losing 5 is losing months of waiting. |
| `manual_locations` | 1 | **NO** — hand-placed 📍 pins, the remedy for the 122 listings no free geocoder can place. |
| `posts` | 11,018 | **NO** — the archive `replay.py` and `stats.py` run on. Re-scraping cannot reach a deleted or edited post. |
| `unknown_locations` | 189 | partly — rebuilt only as new posts arrive. |
| `listings` | 589 | mostly — `replay.py --apply` rebuilds verdicts, but `first_seen` is lost and with it the staleness clock (`storage-notes`). |
| `seen`, `post_fingerprints`, `callback_tokens` | 23,302 / 497 / 503 | yes — dedup state rebuilds, at the cost of re-alerting old flats. |

## 1. Stop every writer, and VERIFY it stopped

Four things write this DB. Missing one is how a restore gets corrupted a second time.

```bash
python -c "import scraper; print('scrape running:', scraper.run_in_progress())"
```

- **The scraper.** `False` is not enough on its own — a run starts on the hour all day, so
  disable the tasks or one will start mid-restore. `apply-replay` records what works:
  **disable `BGU Housing Scraper` and `BGU Housing Scraper Hot`, restore, re-enable.** The
  Hot task needs an **elevated** shell ("Access is denied" otherwise).
- **`bot_listener`.** It writes `marks` and `callback_tokens` every time somebody taps
  ⭐/🗑. `doctor`'s `listener` row says whether it is up. Stop it, and remember it **does
  not reload code** — it must be restarted afterwards either way.
- **`serve_dashboard`.** Writes `manual_locations` when a pin is dropped.
- **Any `replay.py --apply` / `warm_cache` / `resolve_unknowns`.** `guard.py` already
  refuses these during a scrape, but not during a restore.

## 2. Move the damaged file ASIDE — never delete it

```bash
mv data/listings.sqlite "data/listings.CORRUPT-$(date +%Y%m%d-%H%M%S).sqlite"
```

Same reversible move the `osrm-docker` skill uses for orphaned sockets, and for the same
reason: **a half-written database is not an empty one.** It still holds every vote, pin and
archived post written since the snapshot, and step 4 is going to read them out of it. Also
move the `-wal` and `-shm` siblings if they exist — a WAL left beside a restored file will
be replayed into it.

## 3. Check the snapshot BEFORE swapping it in

`backup_db.py` writes through SQLite's online backup API, so a snapshot is internally
consistent even if it was taken mid-write. That is worth trusting, not assuming:

```bash
python -c "import sqlite3,sys; p=sys.argv[1]; c=sqlite3.connect(p); print(c.execute('PRAGMA integrity_check').fetchone()[0]); [print(f'  {t:20}', c.execute(f'SELECT COUNT(*) FROM \"{t}\"').fetchone()[0]) for (t,) in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\")]" data/backups/listings-YYYYMMDD-HHMMSS.sqlite
```

Expect `ok` and plausible counts. Run the **same command against the damaged file** and
compare: the difference is exactly what the restore costs, and you should see it before
you accept it, not after.

**Pick by content, not by filename.** The newest backup is usually right, but if the
corruption is a bad migration rather than a crash, the newest snapshot may already contain
it — walk backwards until `integrity_check` returns `ok` and the counts look sane.

## 4. Restore, then salvage what the snapshot is missing

```bash
cp data/backups/listings-YYYYMMDD-HHMMSS.sqlite data/listings.sqlite
```

Backups are roughly daily (`BGU Backup`, and it is missed whenever the machine sleeps
through its slot — see `health-triage`), so up to a day of votes and pins is now sitting
only in the file you set aside. If the damaged file still opens, take them back:

```sql
ATTACH 'data/listings.CORRUPT-….sqlite' AS old;
INSERT OR IGNORE INTO marks             SELECT * FROM old.marks;
INSERT OR IGNORE INTO manual_locations  SELECT * FROM old.manual_locations;
INSERT OR IGNORE INTO posts             SELECT * FROM old.posts;
```

`INSERT OR IGNORE`, never `INSERT OR REPLACE` — the snapshot's row is the trustworthy one
where they collide. Skip `listings` entirely; step 5 rebuilds it properly.

> **Rehearsed 2026-08-14 against copies, not written from memory.** Restoring the
> `20260813-213002` snapshot over a live-state copy and running the three statements
> above took `posts` from **10,946 → 11,018** — exactly the 72 rows the snapshot was
> missing, matching live — with `marks` and `manual_locations` already whole and
> `PRAGMA integrity_check` still `ok` afterwards. `listings` stayed at 588 by design;
> step 5 rebuilds it. **Rehearse on a copy, never on `data/listings.sqlite`** — make the
> "live" copy with SQLite's backup API as `backup_db.py` does, not `cp`, because a raw
> copy of a database something is writing is not a database.

## 5. Rebuild what IS derivable, then check

```bash
python replay.py --frozen --apply
```

Recomputes every verdict, tier, score and walk time from the restored archive and rebuilds
the Sheet. **Two preconditions, both enforced by `guard.py`:** OSRM must be up (the AMBER
boundary *is* a walk time) and no scrape may be running. `--frozen` because an un-frozen
apply bakes one roll of the network dice into the DB. Full details in `apply-replay`.

Then re-enable the scheduled tasks, restart `bot_listener` and `serve_dashboard`, and:

```bash
python doctor.py
```

The `db` and `backups` rows are the ones this procedure was about. **Take a fresh backup
immediately** — you have just spent one, and `_check_backups` only alarms at 48h.

## Prevention, in one line

`guard.py` refuses `replay --apply`, the LLM harnesses and the DB-writer scripts while a
scrape is running, because two writers on one SQLite leave it half-rewritten. That guard
is the reason this runbook has never yet been needed in anger. Do not route around it with
`BGU_SKIP_GUARD=1` to save a few minutes.
