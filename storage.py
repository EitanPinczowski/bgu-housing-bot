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
    floor TEXT,
    furnished INTEGER,
    balcony INTEGER,
    elevator INTEGER,
    geocode_source TEXT,
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
    if "floor" not in cols:
        c.execute("ALTER TABLE listings ADD COLUMN floor TEXT")
    if "furnished" not in cols:
        c.execute("ALTER TABLE listings ADD COLUMN furnished INTEGER")
    if "balcony" not in cols:
        c.execute("ALTER TABLE listings ADD COLUMN balcony INTEGER")
    if "elevator" not in cols:
        c.execute("ALTER TABLE listings ADD COLUMN elevator INTEGER")
    if "geocode_source" not in cols:
        c.execute("ALTER TABLE listings ADD COLUMN geocode_source TEXT")
    # marks became per-user (dedup_key,user_id); recreate the old single-mark table
    mcols = {r[1] for r in c.execute("PRAGMA table_info(marks)").fetchall()}
    if "user_id" not in mcols:
        c.execute("DROP TABLE IF EXISTS marks")
        c.execute("CREATE TABLE marks (dedup_key TEXT, user_id TEXT, mark TEXT, "
                  "ts TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (dedup_key, user_id))")
    return c


def _content_hash_key(e: ListingExtract) -> str:
    """Fallback key from the listing's content — address + price + rooms + mates."""
    basis = f"{e.street_address_or_neighborhood}|{e.price_per_room_ils}|{e.available_rooms_count}|{e.total_roommates_in_apt}"
    return "hash:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# geresh / gershayim / straight+curly quotes — stripped so "רד״ק"/"רד'ק" normalize
_ADDR_STRIP = str.maketrans("", "", "״׳'`\"‘’“”")


def _norm_addr(address: Optional[str]) -> Optional[str]:
    """Normalized form of a NUMBERED street address (one that carries a house
    number), or None. Collapsing whitespace and dropping quote marks makes the same
    flat's address stable across reads. Bare streets/neighborhoods (no house number,
    e.g. 'רחוב קדש', 'שכונה ב') return None on purpose — different flats share those,
    so they must NOT collapse together."""
    if not address or not any(ch.isdigit() for ch in address):
        return None
    norm = re.sub(r"\s+", " ", address.translate(_ADDR_STRIP)).strip().lower()
    return norm or None


def _addr_key(e: ListingExtract) -> Optional[str]:
    norm = _norm_addr(e.street_address_or_neighborhood)
    return "addr:" + norm if norm else None


def make_dedup_key(e: ListingExtract) -> str:
    """The single primary key written to the listings row: the phone when present
    (survives cross-posting), else the content hash."""
    if e.contact_phone_or_link:
        digits = re.sub(r"\D", "", e.contact_phone_or_link)
        if len(digits) >= 7:
            return "phone:" + digits[-9:]
    return _content_hash_key(e)


def dedup_keys(e: ListingExtract) -> list:
    """The stable keys a listing should be marked/checked 'seen' under, so the same
    flat collapses across reads even when the LLM extracted the phone (or the price)
    on only one read. De-duplicated, order-stable:
      - the primary key (phone else content-hash), and
      - the numbered-address key (the רינגלבלום 1 / רגר 164 case — same numbered
        flat under a phone key on one read and a content hash on another).
    Deliberately NOT the content-hash on its own: with a null/bare address it
    collides across genuinely different flats that share a price+rooms, which would
    drop a real second listing. The content hash is only trusted when it IS the
    primary key (i.e. there's no phone), where make_dedup_key already returns it."""
    keys = [make_dedup_key(e)]
    ak = _addr_key(e)
    if ak and ak not in keys:
        keys.append(ak)
    return keys


def is_seen(dedup_key: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM seen WHERE dedup_key=?", (dedup_key,)).fetchone() is not None


def is_seen_any(keys) -> bool:
    """True if ANY of these keys is already seen — the multi-key dedup check."""
    keys = [k for k in keys if k]
    if not keys:
        return False
    with _conn() as c:
        q = "SELECT 1 FROM seen WHERE dedup_key IN (%s) LIMIT 1" % ",".join("?" * len(keys))
        return c.execute(q, keys).fetchone() is not None


def mark_seen(dedup_key: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO seen(dedup_key) VALUES (?)", (dedup_key,))


def mark_seen_all(keys) -> None:
    """Mark every one of these keys seen (idempotent) — pair with is_seen_any."""
    keys = [(k,) for k in keys if k]
    if not keys:
        return
    with _conn() as c:
        c.executemany("INSERT OR IGNORE INTO seen(dedup_key) VALUES (?)", keys)


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


# "contacted" — a flat you've already messaged, so it stops resurfacing in top-N. Stored
# as a mark under a reserved user id so it never counts as a saved/dismissed vote.
_CONTACTED_UID = "_contacted"


def set_contacted(dedup_key: str) -> None:
    if not dedup_key:
        return
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO marks(dedup_key, user_id, mark, ts) "
                  "VALUES (?,?,?,CURRENT_TIMESTAMP)", (dedup_key, _CONTACTED_UID, "contacted"))


def contacted_keys() -> set:
    with _conn() as c:
        return {r[0] for r in c.execute("SELECT dedup_key FROM marks WHERE mark='contacted'")}


def saved_listings(limit: int = 15) -> list:
    """Listings anyone ⭐-saved (excluding ones marked contacted), newest first — for
    the Telegram /saved command. Returns dict rows."""
    with _conn() as c:
        cur = c.execute(
            """SELECT DISTINCT l.dedup_key, l.address, l.price_per_room, l.available_rooms,
                      l.walk_minutes, l.score, l.source_url, l.location_tier
               FROM listings l JOIN marks m ON m.dedup_key = l.dedup_key
               WHERE m.mark='saved'
                 AND l.dedup_key NOT IN (SELECT dedup_key FROM marks WHERE mark='contacted')
               ORDER BY l.first_seen DESC LIMIT ?""", (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


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


def low_confidence_geocodes(limit: int = 15) -> list:
    """[(address, tier, geocode_source)] for kept listings resolved by a FUZZY geocoder
    (overpass/nominatim) rather than the trusted static table — worth a human glance
    (and pinning to STATIC_TABLE if the point is off). Newest first."""
    with _conn() as c:
        return c.execute(
            "SELECT address, location_tier, geocode_source FROM listings "
            "WHERE geocode_source IN ('overpass','nominatim') "
            "ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()


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


def prune_old_posts(max_age_days: int) -> int:
    """Retention: null raw_text/parsed_json for archived posts older than
    max_age_days, KEEPING sig+verdict (so dedup and stats survive and a pruned
    post is never rescanned). VACUUMs only when rows changed. Returns rows pruned."""
    cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime(_NOW)
    with _conn() as c:
        n = c.execute("UPDATE posts SET raw_text='', parsed_json=NULL "
                      "WHERE first_seen < ? AND (raw_text != '' OR parsed_json IS NOT NULL)",
                      (cutoff,)).rowcount
    if n:
        v = sqlite3.connect(config.DB_PATH)
        v.isolation_level = None            # VACUUM can't run inside a transaction
        v.execute("VACUUM")
        v.close()
    return n


def verdict_counts() -> dict:
    """Counts of archived posts per verdict (status) — for stats.py."""
    with _conn() as c:
        return dict(c.execute("SELECT verdict, COUNT(*) FROM posts GROUP BY verdict").fetchall())


def drop_reason_counts() -> list:
    """(reason, count) for DROP verdicts, most common first — the funnel detail."""
    with _conn() as c:
        return c.execute("SELECT reason, COUNT(*) c FROM posts WHERE verdict='DROP' "
                         "GROUP BY reason ORDER BY c DESC").fetchall()


def group_yield() -> list:
    """Per-FB-group archive yield: (group, total, match, needs, drop, not_ad),
    most matches first — to spot dead groups worth dropping from FB_GROUPS."""
    with _conn() as c:
        rows = c.execute(
            """SELECT "group", COUNT(*),
                      SUM(verdict='MATCH'), SUM(verdict='NEEDS_DATA'),
                      SUM(verdict='DROP'), SUM(verdict='NOT_AD')
               FROM posts WHERE "group" IS NOT NULL AND "group" != ''
               GROUP BY "group" ORDER BY 3 DESC, 2 DESC""").fetchall()
    return [(g, tot, m or 0, n or 0, d or 0, na or 0) for g, tot, m, n, d, na in rows]


def delete_listing(dedup_key: str) -> None:
    """Remove a listing (e.g. replay --apply found it now classifies RED/NOT_AD)."""
    with _conn() as c:
        c.execute("DELETE FROM listings WHERE dedup_key=?", (dedup_key,))


def prune_orphan_listings() -> int:
    """Delete listing rows whose dedup_key can't be reproduced from ANY current
    archived post's parse — i.e. the post that created them was later re-parsed to a
    different key, leaving the old row orphaned (e.g. today's Ollama re-parse). Safe:
    a live listing's key is always derivable from its archived parse, so real rows are
    never removed; no-ops if the live-key set is empty (nothing to compare against).
    Returns rows removed."""
    with _conn() as c:
        live = set()
        for (pj,) in c.execute("SELECT parsed_json FROM posts WHERE parsed_json IS NOT NULL AND parsed_json != ''"):
            try:
                live.add(make_dedup_key(ListingExtract.model_validate_json(pj)))
            except Exception:
                continue
        if not live:
            return 0                       # archive gives us nothing — don't wipe listings
        removed = 0
        for (k,) in c.execute("SELECT dedup_key FROM listings").fetchall():
            if k not in live:
                c.execute("DELETE FROM listings WHERE dedup_key=?", (k,))
                c.execute("DELETE FROM marks WHERE dedup_key=?", (k,))
                c.execute("DELETE FROM post_fingerprints WHERE dedup_key=?", (k,))
                removed += 1
        return removed


def _group_key(dedup_key, address) -> str:
    """The identity a listings ROW is grouped under for de-duplication: its NUMBERED
    address (collapses a phone/hash/field flip of the same flat), else the row's own
    dedup_key so it groups only with itself. Deliberately NOT a content hash: a
    null/bare address + shared price+rooms collides across genuinely different flats
    (different phones), which must never merge."""
    norm = _norm_addr(address)
    return "addr:" + norm if norm else str(dedup_key)


def merge_duplicate_listings() -> int:
    """One-time cleanup: the SAME numbered flat stored under several keys (phone vs
    hash vs a field-flip) — e.g. רינגלבלום 1 as two hashes, רגר 164 as phone+hash.
    Group the listings rows by numbered address, keep the RICHEST row in each group
    (most non-null core fields; tie -> the phone-keyed row, then higher score),
    migrate that group's votes to the kept key, and delete the rest. Returns rows
    removed. Bare/null-address rows never merge (grouped by their own key)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT dedup_key, address, price_per_room, available_rooms, "
            "total_roommates, contact, score FROM listings").fetchall()
        groups: dict = {}
        for r in rows:
            groups.setdefault(_group_key(r[0], r[1]), []).append(r)

        def richness(r):
            core = (r[2], r[3], r[4], r[5])            # price, avail, mates, contact
            return (sum(x is not None for x in core),
                    r[0].startswith("phone:"), r[6] or 0)

        removed = 0
        for grp in groups.values():
            if len(grp) < 2:
                continue
            keep = max(grp, key=richness)[0]
            for r in grp:
                dead = r[0]
                if dead == keep:
                    continue
                c.execute("UPDATE OR IGNORE marks SET dedup_key=? WHERE dedup_key=?", (keep, dead))
                c.execute("DELETE FROM marks WHERE dedup_key=?", (dead,))
                c.execute("DELETE FROM post_fingerprints WHERE dedup_key=?", (dead,))
                c.execute("DELETE FROM listings WHERE dedup_key=?", (dead,))
                removed += 1
        return removed


def set_source_url(dedup_key: str, url: str) -> None:
    """Backfill a listing's post link (e.g. from the live link_backfill)."""
    if not dedup_key or not url:
        return
    with _conn() as c:
        c.execute("UPDATE listings SET source_url=? WHERE dedup_key=?", (url, dedup_key))


def set_post_source_url(sig: str, url: str) -> None:
    """Backfill an archived post's link too, so a later replay keeps it."""
    if not sig or not url:
        return
    with _conn() as c:
        c.execute("UPDATE posts SET source_url=? WHERE sig=?", (url, sig))


def _tri(v):
    """True/False/None -> 1/0/None for a nullable boolean column."""
    return None if v is None else (1 if v else 0)


def save_listing(res: PipelineResult) -> None:
    e = res.extract
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO listings
               (dedup_key,status,location_tier,price_per_room,available_rooms,total_roommates,
                address,walk_minutes,lease_start,contact,summary,source_url,"group",
                price_from_comment,score,images,floor,furnished,balcony,elevator,geocode_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (res.dedup_key, res.status.value, res.location_tier,
             e.price_per_room_ils, e.available_rooms_count, e.total_roommates_in_apt,
             e.street_address_or_neighborhood, res.walk_minutes, e.lease_start_date,
             e.contact_phone_or_link, e.summary_hebrew, res.source_url, res.group,
             1 if e.price_from_comment else 0, res.score, json.dumps(res.images or []),
             e.floor, _tri(e.furnished), e.balcony_or_garden, _tri(e.has_elevator),
             res.geo_source),
        )
