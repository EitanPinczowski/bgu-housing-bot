"""
Local SQLite store: deduplication + saved listings.

Dedup key prefers the contact phone (survives reposts and cross-posting to
several groups). Falls back to a hash of address+price+rooms. We write
incrementally so a crash mid-run never loses or reprocesses state.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import config
from models import ListingExtract, PipelineResult

_NOW = "%Y-%m-%d %H:%M:%S"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    dedup_key TEXT PRIMARY KEY,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS listings (
    dedup_key TEXT PRIMARY KEY,
    status TEXT,
    location_tier TEXT,
    price_per_room INTEGER,
    available_rooms INTEGER,
    total_roommates INTEGER,
    address TEXT,
    walk_minutes REAL,
    lease_start TEXT,
    contact TEXT,
    summary TEXT,
    source_url TEXT,
    "group" TEXT,
    price_from_comment INTEGER DEFAULT 0,
    score INTEGER,
    images TEXT,
    file_ids TEXT,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS marks (
    dedup_key TEXT,
    user_id TEXT,
    mark TEXT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dedup_key, user_id)
);
CREATE TABLE IF NOT EXISTS unknown_locations (
    location TEXT PRIMARY KEY,
    count INTEGER DEFAULT 0,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS post_fingerprints (
    dedup_key TEXT PRIMARY KEY,
    tokens TEXT,
    first_seen TEXT
);
CREATE TABLE IF NOT EXISTS posts (
    sig TEXT PRIMARY KEY,
    raw_text TEXT,
    comments TEXT,
    images TEXT,
    "group" TEXT,
    source_url TEXT,
    parsed_json TEXT,
    verdict TEXT,
    reason TEXT,
    tier TEXT,
    score INTEGER,
    first_seen TEXT
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.DB_PATH)
    c.executescript(_SCHEMA)
    # migration: add columns introduced after an older DB was created
    cols = {r[1] for r in c.execute("PRAGMA table_info(listings)").fetchall()}
    if "price_from_comment" not in cols:
        c.execute("ALTER TABLE listings ADD COLUMN price_from_comment INTEGER DEFAULT 0")
    if "score" not in cols:
        c.execute("ALTER TABLE listings ADD COLUMN score INTEGER")
    if "images" not in cols:
        c.execute("ALTER TABLE listings ADD COLUMN images TEXT")
    if "file_ids" not in cols:
        c.execute("ALTER TABLE listings ADD COLUMN file_ids TEXT")
    # marks became per-user (dedup_key,user_id); recreate the old single-mark table
    mcols = {r[1] for r in c.execute("PRAGMA table_info(marks)").fetchall()}
    if "user_id" not in mcols:
        c.execute("DROP TABLE IF EXISTS marks")
        c.execute("CREATE TABLE marks (dedup_key TEXT, user_id TEXT, mark TEXT, "
                  "ts TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (dedup_key, user_id))")
    return c


def make_dedup_key(e: ListingExtract) -> str:
    if e.contact_phone_or_link:
        digits = re.sub(r"\D", "", e.contact_phone_or_link)
        if len(digits) >= 7:
            return "phone:" + digits[-9:]
    basis = f"{e.street_address_or_neighborhood}|{e.price_per_room_ils}|{e.available_rooms_count}|{e.total_roommates_in_apt}"
    return "hash:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def is_seen(dedup_key: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM seen WHERE dedup_key=?", (dedup_key,)).fetchone() is not None


def mark_seen(dedup_key: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO seen(dedup_key) VALUES (?)", (dedup_key,))


# URL-level dedup, checked BEFORE the LLM runs so a post we already processed in
# an earlier run doesn't cost another API call. Reuses the `seen` table with a
# "url:" prefix (a permalink is unique enough; no hashing needed). This only
# catches the same permalink again — cross-posted reposts with a different URL
# are still caught later by the phone/content dedup_key.
def is_url_seen(source_url: str) -> bool:
    return is_seen("url:" + source_url)


def mark_url_seen(source_url: str) -> None:
    mark_seen("url:" + source_url)


# Per-user triage from the alert buttons: 'saved' (interested) / 'dismissed'.
def get_user_mark(dedup_key: str, user_id) -> Optional[str]:
    """This user's existing vote on this apartment, or None if they haven't voted."""
    with _conn() as c:
        row = c.execute("SELECT mark FROM marks WHERE dedup_key=? AND user_id=?",
                        (dedup_key, str(user_id))).fetchone()
    return row[0] if row else None


def set_mark(dedup_key: str, user_id, mark: str) -> bool:
    """Record a vote ONCE per user per apartment. Returns True if it was newly
    recorded, False if this user had already voted (their vote is left unchanged
    — votes are final, no flipping or re-pressing). INSERT OR IGNORE against the
    (dedup_key, user_id) primary key makes the check atomic."""
    with _conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO marks(dedup_key, user_id, mark, ts) "
                        "VALUES (?,?,?,CURRENT_TIMESTAMP)",
                        (dedup_key, str(user_id), mark))
        return cur.rowcount > 0


def mark_counts(dedup_key: str) -> dict:
    """How many people saved vs dismissed this apartment: {'saved': n, 'dismissed': m}."""
    with _conn() as c:
        d = dict(c.execute("SELECT mark, COUNT(*) FROM marks WHERE dedup_key=? GROUP BY mark",
                           (dedup_key,)).fetchall())
    return {"saved": d.get("saved", 0), "dismissed": d.get("dismissed", 0)}


def mark_adjustment(dedup_key: str) -> int:
    """Net score delta from the group's votes: +MARK_SCORE_DELTA per person who
    saved, -MARK_SCORE_DELTA per person who dismissed."""
    d = mark_counts(dedup_key)
    return config.MARK_SCORE_DELTA * (d["saved"] - d["dismissed"])


def base_score(dedup_key: str) -> int:
    with _conn() as c:
        row = c.execute("SELECT score FROM listings WHERE dedup_key=?", (dedup_key,)).fetchone()
    return row[0] if row and row[0] is not None else 0


def effective_score(dedup_key: str, base: Optional[int] = None) -> int:
    """Base fit score plus the group's vote adjustment."""
    if base is None:
        base = base_score(dedup_key)
    return base + mark_adjustment(dedup_key)


def get_images(dedup_key: str) -> list:
    with _conn() as c:
        row = c.execute("SELECT images FROM listings WHERE dedup_key=?", (dedup_key,)).fetchone()
    try:
        return json.loads(row[0]) if row and row[0] else []
    except Exception:
        return []


# Telegram photo file_ids captured the FIRST time a listing was alerted. Unlike
# Facebook CDN URLs (which expire), a file_id is reusable by the bot forever, so
# re-posting a listing in the morning/evening top-N always keeps its album.
def set_file_ids(dedup_key: str, file_ids: list) -> None:
    if not dedup_key or not file_ids:
        return
    with _conn() as c:
        c.execute("UPDATE listings SET file_ids=? WHERE dedup_key=?",
                  (json.dumps(file_ids), dedup_key))


def get_file_ids(dedup_key: str) -> list:
    with _conn() as c:
        row = c.execute("SELECT file_ids FROM listings WHERE dedup_key=?", (dedup_key,)).fetchone()
    try:
        return json.loads(row[0]) if row and row[0] else []
    except Exception:
        return []


# --- unknown locations: names the LLM extracted but geocoding couldn't map, so
# you can pin the common ones to the static table (see the daily DM digest). ----
def record_unknown_location(name: Optional[str]) -> None:
    if not name or not name.strip():
        return
    with _conn() as c:
        c.execute("INSERT INTO unknown_locations(location, count, last_seen) VALUES (?,1,?) "
                  "ON CONFLICT(location) DO UPDATE SET count=count+1, last_seen=excluded.last_seen",
                  (name.strip(), datetime.now().strftime(_NOW)))


def unknown_locations(days: int = 7) -> list:
    """[(location, count, last_seen)] seen in the last `days`, most frequent first."""
    since = (datetime.now() - timedelta(days=days)).strftime(_NOW)
    with _conn() as c:
        return c.execute("SELECT location, count, last_seen FROM unknown_locations "
                         "WHERE last_seen >= ? ORDER BY count DESC, last_seen DESC",
                         (since,)).fetchall()


# --- fuzzy cross-post dedup: a fingerprint (set of Hebrew word tokens) of each
# saved listing's text, so a near-identical repost (same flat, phone shown in one
# copy only) is caught even when the exact text-signature and dedup_key differ. --
def record_fingerprint(dedup_key: str, tokens) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO post_fingerprints(dedup_key, tokens, first_seen) "
                  "VALUES (?,?,?)",
                  (dedup_key, json.dumps(sorted(set(tokens))), datetime.now().strftime(_NOW)))


def find_similar(tokens, days: int = 4, threshold: float = 0.72,
                 min_tokens: int = 8) -> Optional[str]:
    """dedup_key of a recently-saved listing whose token set is ≥ threshold
    Jaccard-similar to `tokens`, else None. Skips very short posts (unreliable)."""
    ts = set(tokens)
    if len(ts) < min_tokens:
        return None
    since = (datetime.now() - timedelta(days=days)).strftime(_NOW)
    best, best_sim = None, 0.0
    with _conn() as c:
        rows = c.execute("SELECT dedup_key, tokens FROM post_fingerprints WHERE first_seen >= ?",
                         (since,)).fetchall()
    for key, tj in rows:
        try:
            other = set(json.loads(tj))
        except Exception:
            continue
        if len(other) < min_tokens:
            continue
        union = len(ts | other)
        sim = (len(ts & other) / union) if union else 0.0
        if sim >= threshold and sim > best_sim:
            best, best_sim = key, sim
    return best


# --- raw-post archive: every post that reached the LLM, with its parsed fields
# and final verdict. Lets us re-run classification/scoring against history WITHOUT
# re-scraping Facebook (replay.py), and powers the --stats funnel (stats.py). ---
def record_post(sig: str, raw_text: str, comments, images, group, source_url,
                extract, res: PipelineResult) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO posts
               (sig, raw_text, comments, images, "group", source_url, parsed_json,
                verdict, reason, tier, score, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(sig) DO UPDATE SET
                 raw_text=excluded.raw_text, comments=excluded.comments, images=excluded.images,
                 "group"=excluded."group", source_url=excluded.source_url,
                 parsed_json=excluded.parsed_json, verdict=excluded.verdict,
                 reason=excluded.reason, tier=excluded.tier, score=excluded.score""",
            (sig, raw_text, comments or "", json.dumps(images or []), group, source_url,
             extract.model_dump_json() if extract else None,
             res.status.value, res.reason, res.location_tier, res.score))


def all_posts() -> list:
    """Every archived post as a dict, newest first — for replay.py."""
    with _conn() as c:
        cur = c.execute("""SELECT sig, raw_text, comments, images, "group", source_url,
                                  parsed_json, verdict, reason, tier, score, first_seen
                           FROM posts ORDER BY first_seen DESC""")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def verdict_counts() -> dict:
    """Counts of archived posts per verdict (status) — for stats.py."""
    with _conn() as c:
        return dict(c.execute("SELECT verdict, COUNT(*) FROM posts GROUP BY verdict").fetchall())


def drop_reason_counts() -> list:
    """(reason, count) for DROP verdicts, most common first — the funnel detail."""
    with _conn() as c:
        return c.execute("SELECT reason, COUNT(*) c FROM posts WHERE verdict='DROP' "
                         "GROUP BY reason ORDER BY c DESC").fetchall()


def save_listing(res: PipelineResult) -> None:
    e = res.extract
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO listings
               (dedup_key,status,location_tier,price_per_room,available_rooms,total_roommates,
                address,walk_minutes,lease_start,contact,summary,source_url,"group",price_from_comment,score,images)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (res.dedup_key, res.status.value, res.location_tier,
             e.price_per_room_ils, e.available_rooms_count, e.total_roommates_in_apt,
             e.street_address_or_neighborhood, res.walk_minutes, e.lease_start_date,
             e.contact_phone_or_link, e.summary_hebrew, res.source_url, res.group,
             1 if e.price_from_comment else 0, res.score, json.dumps(res.images or [])),
        )
