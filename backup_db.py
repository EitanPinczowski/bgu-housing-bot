"""
Timestamped backup of the source-of-truth SQLite DB (data/listings.sqlite).

The DB holds everything the bot can't re-derive from Facebook — dedup state, the
group's ⭐/🗑 votes, and the post archive that powers replay/stats. A corruption or
a bad migration could wipe it, so keep a rolling set of dated copies.

    python backup_db.py

Schedule it weekly in Task Scheduler. Backups live in data/backups/ (git-ignored),
newest kept, oldest pruned beyond KEEP.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime

import config

BACKUP_DIR = config.DATA_DIR / "backups"
KEEP = 14                      # how many dated copies to retain


def _prune(keep: int = KEEP) -> int:
    """Delete all but the newest `keep` backups. Returns how many were removed."""
    files = sorted(BACKUP_DIR.glob("listings-*.sqlite"))
    old = files[:-keep] if keep > 0 else files
    for f in old:
        try:
            f.unlink()
        except Exception as exc:
            print(f"[backup] could not remove {f.name}: {exc}")
    return len(old)


def backup():
    """Write a consistent copy of the DB via SQLite's online backup API (safe even if
    something is mid-write), then prune old ones. Returns the new path, or None."""
    if not config.DB_PATH.exists():
        print("[backup] no DB yet — nothing to back up")
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    dest = BACKUP_DIR / f"listings-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)          # consistent snapshot, not a raw file copy
    finally:
        dst.close()
        src.close()
    pruned = _prune()
    print(f"[backup] wrote {dest.name} (kept {KEEP}, pruned {pruned})")
    return dest


if __name__ == "__main__":
    backup()
