"""backup_db: a consistent DB copy is written, and old backups are pruned to KEEP."""
import sqlite3

import backup_db
import config


def test_backup_creates_valid_copy(monkeypatch, tmp_path):
    db = tmp_path / "listings.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()
    con.close()
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path / "backups")

    dest = backup_db.backup()
    assert dest is not None and dest.exists()
    con = sqlite3.connect(dest)
    assert con.execute("SELECT x FROM t").fetchone()[0] == 42     # a real, readable copy
    con.close()


def test_backup_no_db_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "missing.sqlite")
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path / "backups")
    assert backup_db.backup() is None


def test_prune_keeps_newest(monkeypatch, tmp_path):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    monkeypatch.setattr(backup_db, "BACKUP_DIR", bdir)
    names = [f"listings-2026010{i}-000000.sqlite" for i in range(1, 6)]   # 5 dated copies
    for n in names:
        (bdir / n).write_text("x")
    removed = backup_db._prune(keep=3)
    assert removed == 2
    kept = sorted(f.name for f in bdir.glob("listings-*.sqlite"))
    assert kept == names[2:]        # the 3 newest (lexically-latest) remain
