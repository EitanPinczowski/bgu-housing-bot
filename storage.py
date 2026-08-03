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
CREATE TABLE IF NOT EXISTS notes (
    dedup_key TEXT PRIMARY KEY,
    text TEXT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
-- Coordinates set by hand from the dashboard, when the geocoder put a flat in the
-- wrong place (there are no apartments inside the university). Keyed by dedup_key so
-- it corrects ONE listing; the "same address everywhere" case goes to user_pins.json
-- via geocode.add_pin instead. pipeline._classify prefers this over the geocoder, so
-- a correction survives replay --apply and every later re-read.
CREATE TABLE IF NOT EXISTS manual_locations (
    dedup_key TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    note TEXT
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
-- Short stand-ins for a dedup_key inside a Telegram button.
--
-- Telegram caps callback_data at 64 BYTES and rejects the WHOLE MESSAGE with
-- BUTTON_DATA_INVALID if any button is over — it does not drop just that button. A
-- dedup_key is `phone|address`, and Hebrew costs 2 bytes a character, so a descriptive
-- address blows the cap: measured 2026-08-02, 16 of 417 keys were too long, the longest
-- 93 bytes. Because alerts are BATCHED, one such listing took down the whole batch —
-- that run delivered 4 of 16 alerts and the rest were lost silently.
CREATE TABLE IF NOT EXISTS callback_tokens (
    token TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_callback_tokens_key ON callback_tokens(dedup_key);
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
    posted_at TEXT,
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
    if "amenities" not in cols:
        # One JSON blob rather than a column per amenity: the list is display-only
        # config (config.AMENITY_TARGETS) and will grow, and a schema migration per
        # bus stop would be absurd.
        c.execute("ALTER TABLE listings ADD COLUMN amenities TEXT")
    pcols = {r[1] for r in c.execute("PRAGMA table_info(posts)").fetchall()}
    if pcols and "posted_at" not in pcols:
        c.execute("ALTER TABLE posts ADD COLUMN posted_at TEXT")
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


def _norm_addr_raw(address: Optional[str]) -> Optional[str]:
    """The pre-2026-08-02 normalisation: whitespace collapsed, quote marks dropped.

    Kept because it is the form every key already in `seen` and in `listings` was
    written with. `dedup_keys` still emits it so an existing flat is recognised and
    does not re-alert under its new key."""
    if not address or not any(ch.isdigit() for ch in address):
        return None
    norm = re.sub(r"\s+", " ", address.translate(_ADDR_STRIP)).strip().lower()
    return norm or None


def _norm_addr(address: Optional[str]) -> Optional[str]:
    """Identity of a NUMBERED street address: `canonical street|number`, else None.

    THE ADDRESS PART OF A DEDUP KEY MUST BE THE STREET AND THE NUMBER, NOT THE POST'S
    WORDING. Scrubbing whitespace is not enough — landlords describe one flat many
    ways, and each phrasing minted its own key, so the same flat was stored twice.
    Measured 2026-08-02 over 399 listings: 11 duplicate pairs, every one of them a
    phrasing difference over an identical (phone, street, number):

        רגר 93, הבלוק          vs  רגר 93, גבול בין שכונה ב' ל-שכונה ד', הבלוק
        ברגר 155               vs  רגר 155                (a ב proclitic)
        רחוב סוסו הכהן 6        vs  סוסו הכהן 6            (a road-type word)
        ו' הישנה, בן מתיתיהו 13 vs  בן מתיתיהו 13, ו' הישנה  (component order)
        רח' וינגייט 64          vs  רחוב וינגייט 64         (an abbreviation)

    Every one of those is already solved by `streets.canonical`, which is what the
    geocoder resolves addresses with — so dedup now agrees with the map about what
    counts as the same place.

    This does NOT relax the 2026-07-29 rule that identity is phone + NUMBERED address
    rather than the phone alone: a bare street or neighbourhood still returns None, so
    it still collapses on the phone and two different flats never merge. It only stops
    one flat's two descriptions from counting as two.

    When the street cannot be resolved we fall back to the old scrubbed text, so an
    address the index does not know behaves exactly as it did before."""
    raw = _norm_addr_raw(address)
    if not raw:
        return None
    m = re.search(r"\b(\d{1,4})\b", address)
    if not m:
        return raw
    # lazy: keeps storage's import graph free of the geocoding stack, and only the
    # dedup path pays for loading the street index
    try:
        import geocode
        import streets
        cands = list(geocode._candidate_tokens(address)[:2])
        # THE STREET IS THE WORDS JUST BEFORE THE NUMBER. `_candidate_tokens` splits on
        # punctuation, so it hands back whole phrases: `רגר 93 פינתי עם שלמה המלך`
        # resolves to nothing and the key fell back to raw text — which is how the very
        # duplicate that started this (רגר 93, twice) survived the first fix. Take the
        # text up to the house number and its last comma-segment, which is the street in
        # both `רגר 93 פינתי…` and `ו' הישנה, בן מתיתיהו 13`.
        head = address[:m.start()]
        tail = re.split(r"[,/|]", head)[-1].strip()
        if tail:
            cands.append(tail)
        for cand in cands:
            real, _how = streets.canonical(cand)
            if real:
                return f"{real}|{m.group(1)}".lower()
    except Exception:
        pass
    return raw


def _addr_key(e: ListingExtract) -> Optional[str]:
    norm = _norm_addr(e.street_address_or_neighborhood)
    return "addr:" + norm if norm else None


def _digits_key(contact: Optional[str]) -> Optional[str]:
    """The last 9 digits of a contact string, or None. Same rule as _phone_key, but
    from a stored `contact` column rather than an extract."""
    digits = re.sub(r"\D", "", contact or "")
    return digits[-9:] if len(digits) >= 7 else None


def _contact_numbers(contact: Optional[str]) -> set:
    """EVERY phone number in a contact string, not just the last one.

    A post often lists two ("054-3376992, 052-3252255"), and taking the last 9 digits
    of the whole string invents a number that belongs to neither: three וינגייט 64 rows
    from one landlord looked like two different people and refused to merge."""
    out = set()
    for run in re.findall(r"\d[\d\-\s]{6,}\d", contact or ""):
        d = re.sub(r"\D", "", run)
        # a run can be two numbers with only a space between them
        for i in range(0, len(d) - 8, 9) if len(d) >= 18 else [0]:
            part = d[i:i + 9] if len(d) >= 18 else d[-9:]
            if len(part) >= 9:
                out.add(part)
    return out


def _phone_key(e: ListingExtract) -> Optional[str]:
    if e.contact_phone_or_link:
        digits = re.sub(r"\D", "", e.contact_phone_or_link)
        if len(digits) >= 7:
            return "phone:" + digits[-9:]
    return None


def make_dedup_key(e: ListingExtract) -> str:
    """The single primary key written to the listings row.

    The phone survives cross-posting, so it is the backbone — but a phone alone
    is NOT a flat. Measured on the archive: 42 numbers advertise more than one
    numbered address (one posts 32), and keying on the bare phone silently
    collapsed 101 genuinely different flats into a single row, dropping every one
    after the first as "already seen". So when we know BOTH the phone and a
    numbered address, the identity is the pair; the flats separate while reposts of
    the same flat still collapse. Without a house number we can't tell two flats
    apart, so a bare address keeps the old conservative phone-only behaviour."""
    phone = _phone_key(e)
    addr = _norm_addr(e.street_address_or_neighborhood)
    if phone and addr:
        return f"{phone}|{addr}"
    return phone or _content_hash_key(e)


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
    # THE LEGACY FORMS, or every flat already stored would re-alert. `seen` and the
    # listings table are full of keys built from the post's raw wording; once the
    # address part became `street|number` those stopped matching, and a repost of a
    # known flat would have looked brand new. Same shape as the callback-token
    # fallback: recognise the old key, mint the new one.
    phone = _phone_key(e)
    raw = _norm_addr_raw(e.street_address_or_neighborhood)
    if raw:
        for legacy in (f"{phone}|{raw}" if phone else None, "addr:" + raw):
            if legacy and legacy not in keys:
                keys.append(legacy)
    return keys


def is_duplicate(e: ListingExtract) -> bool:
    """Have we already handled THIS flat? The dedup entry point — use it instead of
    is_seen_any(dedup_keys(e)) so the phone/address rules live in one place.

    Two checks:
      1. any of the listing's stable keys is already seen (primary + address), and
      2. a listing with NO house number, from a phone we already have under some
         address, is treated as the same flat. We genuinely cannot tell whether
         "רגר, אצל דני" is a new flat or a vaguer re-read of the one we stored, and
         a wrong duplicate alert is worse than a missed vague one."""
    if is_seen_any(dedup_keys(e)):
        return True
    phone = _phone_key(e)
    if phone and not _norm_addr(e.street_address_or_neighborhood):
        with _conn() as c:
            return c.execute(
                "SELECT 1 FROM seen WHERE dedup_key = ? OR dedup_key LIKE ? LIMIT 1",
                (phone, phone + "|%")).fetchone() is not None
    return False


_broker_counts: Optional[dict] = None


def _build_broker_counts() -> dict:
    """{phone_key: distinct numbered addresses advertised} from the POST ARCHIVE.

    The archive, not the listings table: listings only holds what survived the zone
    and price gates, so an agency with 32 flats city-wide and two near campus would
    look like a private landlord. Built once per process (a few thousand rows) and
    cached — process_post calls this per post."""
    counts: dict = {}
    with _conn() as c:
        rows = c.execute("SELECT parsed_json FROM posts "
                         "WHERE parsed_json IS NOT NULL").fetchall()
    seen_pairs = set()
    for (pj,) in rows:
        try:
            d = json.loads(pj)
        except Exception:
            continue
        phone, addr = d.get("contact_phone_or_link"), d.get("street_address_or_neighborhood")
        if not phone or not addr:
            continue
        digits = re.sub(r"\D", "", phone)
        norm = _norm_addr(addr)
        if len(digits) < 7 or not norm:
            continue
        pair = ("phone:" + digits[-9:], norm)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        counts[pair[0]] = counts.get(pair[0], 0) + 1
    return counts


def phone_listing_count(phone: Optional[str]) -> int:
    """How many DISTINCT numbered flats this contact has advertised. A private
    landlord has one or two; an agency has many, which is a far more reliable
    signal than matching "תיווך" in the text (plenty of brokers never say it, and
    plenty of posts mention it about someone else)."""
    global _broker_counts
    if not phone:
        return 0
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return 0
    if _broker_counts is None:
        _broker_counts = _build_broker_counts()
    return _broker_counts.get("phone:" + digits[-9:], 0)


def invalidate_broker_counts() -> None:
    """Drop the cached agency tallies — call after archiving new posts so a long-lived
    process (the listener) doesn't keep a stale picture."""
    global _broker_counts
    _broker_counts = None


_broker_pairs: set = set()


def _note_broker_pair(extract) -> None:
    """Fold one freshly archived post into the cached tallies, so a run that discovers
    a landlord's 4th flat flags them immediately. Updating in place beats invalidating:
    rebuilding scans the whole archive, and record_post runs once per post."""
    if _broker_counts is None or extract is None:
        return
    phone = getattr(extract, "contact_phone_or_link", None)
    norm = _norm_addr(getattr(extract, "street_address_or_neighborhood", None))
    if not phone or not norm:
        return
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return
    pair = ("phone:" + digits[-9:], norm)
    if pair in _broker_pairs:
        return
    _broker_pairs.add(pair)
    _broker_counts[pair[0]] = _broker_counts.get(pair[0], 0) + 1


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


def stale_keys(days: Optional[int] = None) -> set:
    """dedup_keys of listings first seen more than `days` ago — almost certainly gone, so
    /top and the scheduled top-N skip them (they stay in the DB, Sheet and /search)."""
    days = days if days is not None else getattr(config, "LISTING_STALE_DAYS", 21)
    cutoff = (datetime.now() - timedelta(days=days)).strftime(_NOW)
    with _conn() as c:
        return {r[0] for r in c.execute(
            "SELECT dedup_key FROM listings WHERE first_seen < ?", (cutoff,))}


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
                extract, res: PipelineResult, age_hours=None) -> None:
    """Archive one post. `age_hours` (how old the post was when scraped) is stored as an
    absolute `posted_at`, so we can later see WHEN good listings actually get posted and
    weight the scrape schedule toward those hours. Harmless when unknown (None)."""
    posted_at = None
    if age_hours is not None:
        try:
            posted_at = (datetime.now() - timedelta(hours=float(age_hours))).strftime(_NOW)
        except (TypeError, ValueError):
            posted_at = None
    with _conn() as c:
        c.execute(
            """INSERT INTO posts
               (sig, raw_text, comments, images, "group", source_url, parsed_json,
                verdict, reason, tier, score, posted_at, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(sig) DO UPDATE SET
                 raw_text=excluded.raw_text, comments=excluded.comments, images=excluded.images,
                 "group"=excluded."group", source_url=excluded.source_url,
                 parsed_json=excluded.parsed_json, verdict=excluded.verdict,
                 reason=excluded.reason, tier=excluded.tier, score=excluded.score,
                 posted_at=COALESCE(excluded.posted_at, posts.posted_at)""",
            (sig, raw_text, comments or "", json.dumps(images or []), group, source_url,
             extract.model_dump_json() if extract else None,
             res.status.value, res.reason, res.location_tier, res.score, posted_at))
    _note_broker_pair(extract)     # keep the agency tallies current within this run


def all_posts() -> list:
    """Every archived post as a dict, newest first — for replay.py."""
    with _conn() as c:
        cur = c.execute("""SELECT sig, raw_text, comments, images, "group", source_url,
                                  parsed_json, verdict, reason, tier, score, first_seen,
                                  posted_at
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


def get_all_images() -> list:
    """Every image URL stored on any listing. The dashboard's image proxy uses this as
    its ALLOW-LIST — a URL that isn't here is never fetched, so the proxy can't be turned
    into a relay for arbitrary addresses."""
    urls = []
    with _conn() as c:
        for (blob,) in c.execute("SELECT images FROM listings WHERE images IS NOT NULL"):
            try:
                urls.extend(json.loads(blob) or [])
            except Exception:
                continue
    return urls


def dashboard_version() -> dict:
    """A cheap fingerprint of the listings table, so an open dashboard can poll this and
    only refetch the full payload when something actually changed."""
    with _conn() as c:
        n, newest = c.execute(
            "SELECT COUNT(*), COALESCE(MAX(first_seen),'') FROM listings").fetchone()
        marks = c.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
    return {"count": n, "newest": newest, "marks": marks}


def set_note(dedup_key: str, text: Optional[str]) -> None:
    """Save (or clear) a free-text note against a listing — "called Tue, no answer",
    "viewing Thu 17:00". Empty text deletes the row rather than storing a blank."""
    if not dedup_key:
        return
    with _conn() as c:
        if not (text or "").strip():
            c.execute("DELETE FROM notes WHERE dedup_key=?", (dedup_key,))
            return
        c.execute("INSERT INTO notes(dedup_key, text, ts) VALUES (?,?,CURRENT_TIMESTAMP) "
                  "ON CONFLICT(dedup_key) DO UPDATE SET text=excluded.text, "
                  "ts=CURRENT_TIMESTAMP", (dedup_key, text.strip()))


def get_note(dedup_key: str) -> str:
    with _conn() as c:
        row = c.execute("SELECT text FROM notes WHERE dedup_key=?", (dedup_key,)).fetchone()
    return (row[0] if row else "") or ""


def all_notes() -> dict:
    """{dedup_key: text} — one query, so the dashboard doesn't do N of them."""
    with _conn() as c:
        return {k: t for k, t in c.execute("SELECT dedup_key, text FROM notes") if t}


# A dedup_key is `phone|address`; Telegram allows 64 BYTES of callback_data and rejects
# the whole message if any button exceeds it (see the callback_tokens DDL). 12 hex chars
# keeps `dismiss|<token>` at 20 bytes with room to spare, and 48 bits makes a collision
# across a few thousand listings vanishingly unlikely — and the loop below handles one
# anyway rather than silently pointing two flats at the same button.
_TOKEN_CHARS = 12


def callback_token(dedup_key: str) -> str:
    """A short, stable stand-in for `dedup_key`, safe inside a Telegram button.

    Deterministic, so re-alerting the same flat reuses its token instead of growing a
    row every run, and so a button posted last week still resolves today."""
    import hashlib
    if not dedup_key:
        return ""
    digest = hashlib.blake2s(dedup_key.encode("utf-8")).hexdigest()
    with _conn() as c:
        for size in range(_TOKEN_CHARS, len(digest) + 1):
            token = digest[:size]
            row = c.execute("SELECT dedup_key FROM callback_tokens WHERE token=?",
                            (token,)).fetchone()
            if row is None:
                c.execute("INSERT INTO callback_tokens(token, dedup_key) VALUES (?,?)",
                          (token, dedup_key))
                return token
            if row[0] == dedup_key:
                return token                      # already minted, reuse it
        return digest                             # full digest collided: impossible-ish


def key_for_token(token: str) -> Optional[str]:
    """The dedup_key a button's token stands for, or None if we've never seen it."""
    if not token:
        return None
    with _conn() as c:
        row = c.execute("SELECT dedup_key FROM callback_tokens WHERE token=?",
                        (token,)).fetchone()
    return row[0] if row else None


def set_manual_location(dedup_key: str, lat: float, lon: float,
                        note: Optional[str] = None) -> None:
    """Pin ONE listing's coordinates by hand, overriding the geocoder for good.

    `pipeline._classify` prefers this, so the correction survives `replay --apply`
    and every later re-read — which is the whole point. To fix an ADDRESS rather than
    a listing (`אוניברסיטת בן גוריון` resolving to the campus, `שכונה ד` to a
    centroid), use `geocode.add_pin` instead: that fixes every listing there, now and
    in the future."""
    if not dedup_key:
        return
    with _conn() as c:
        c.execute("INSERT INTO manual_locations(dedup_key, lat, lon, ts, note) "
                  "VALUES (?,?,?,CURRENT_TIMESTAMP,?) "
                  "ON CONFLICT(dedup_key) DO UPDATE SET lat=excluded.lat, "
                  "lon=excluded.lon, ts=CURRENT_TIMESTAMP, note=excluded.note",
                  (dedup_key, float(lat), float(lon), note))


def manual_location(dedup_key: str):
    """(lat, lon) if this listing has been placed by hand, else None."""
    if not dedup_key:
        return None
    with _conn() as c:
        row = c.execute("SELECT lat, lon FROM manual_locations WHERE dedup_key=?",
                        (dedup_key,)).fetchone()
    return (row[0], row[1]) if row else None


def clear_manual_location(dedup_key: str) -> bool:
    """Drop the override so the geocoder decides again. Returns whether one existed."""
    with _conn() as c:
        cur = c.execute("DELETE FROM manual_locations WHERE dedup_key=?", (dedup_key,))
        return cur.rowcount > 0


def all_manual_locations() -> dict:
    """{dedup_key: (lat, lon)} — one query, so the dashboard doesn't do N of them."""
    with _conn() as c:
        return {k: (la, lo)
                for k, la, lo in c.execute("SELECT dedup_key, lat, lon "
                                           "FROM manual_locations")}


def post_text_for(keys) -> dict:
    """{dedup_key: original post text} for the given listings.

    The extracted fields lose detail — amenities, quirks, "ללא תיווך" — so the dashboard
    shows the raw ad too. Matched by recomputing each archived post's dedup keys, the
    same way backfill_first_seen does."""
    wanted = {k for k in keys if k}
    if not wanted:
        return {}
    out: dict = {}
    with _conn() as c:
        rows = c.execute("SELECT parsed_json, raw_text FROM posts "
                         "WHERE parsed_json IS NOT NULL AND raw_text != ''").fetchall()
    for pj, raw in rows:
        try:
            e = ListingExtract.model_validate_json(pj)
        except Exception:
            continue
        for key in dedup_keys(e):
            if key in wanted and key not in out:
                out[key] = raw
    return out


def post_for(dedup_key: str):
    """The archived post behind one listing, as a dict, or None.

    Same key-recomputation as post_text_for. Used to re-run the classifier on a
    single listing (after its location is corrected by hand) instead of replaying the
    whole archive — the verdict then comes from the real pipeline rather than a
    parallel reimplementation that could drift from it."""
    if not dedup_key:
        return None
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM posts WHERE parsed_json IS NOT NULL").fetchall()
    for row in rows:
        try:
            e = ListingExtract.model_validate_json(row["parsed_json"])
        except Exception:
            continue
        if dedup_key in dedup_keys(e):
            return dict(row)
    return None


def search_post_text(term: str, limit: int = 400) -> set:
    """dedup_keys of listings whose ORIGINAL post text contains `term`.

    This is the point of keeping the archive: "ללא תיווך" (236 posts), "מרוהט" (756),
    "מזגן" (148) are nowhere in the extracted schema, so they're otherwise unsearchable.
    Quote marks are folded so הכ״ג and הכ"ג match each other."""
    term = _norm_search(term)
    if len(term) < 2:
        return set()
    hits: set = set()
    with _conn() as c:
        rows = c.execute("SELECT parsed_json, raw_text FROM posts "
                         "WHERE parsed_json IS NOT NULL AND raw_text != ''").fetchall()
    for pj, raw in rows:
        if term not in _norm_search(raw):
            continue
        try:
            e = ListingExtract.model_validate_json(pj)
        except Exception:
            continue
        hits.update(k for k in dedup_keys(e) if k)
        if len(hits) >= limit:
            break
    return hits


def _norm_search(s: Optional[str]) -> str:
    return (s or "").translate(_ADDR_STRIP).lower()


def backfill_first_seen() -> int:
    """Restore each listing's real discovery date from the post archive.

    `replay --apply` used to `INSERT OR REPLACE`, which reset `first_seen` to now on
    every run — so the whole table reads as "found today" and LISTING_STALE_DAYS, the
    /top time windows and the freshness score factor all mean nothing. The upsert no
    longer does that, but the existing rows are already wrong. `posts.first_seen` was
    never clobbered (record_post preserves it on conflict), so the archive can repair it.

    Moves a date BACKWARDS only. That's what makes a re-run a no-op, and it means the
    repair can never invent a listing that looks newer than it is. Returns rows moved.
    """
    earliest: dict = {}
    with _conn() as c:
        rows = c.execute("SELECT parsed_json, posted_at, first_seen FROM posts "
                         "WHERE parsed_json IS NOT NULL").fetchall()
    for pj, posted_at, first_seen in rows:
        stamp = posted_at or first_seen
        if not stamp:
            continue
        try:
            e = ListingExtract.model_validate_json(pj)
        except Exception:
            continue
        for key in dedup_keys(e):
            if key and (key not in earliest or stamp < earliest[key]):
                earliest[key] = stamp

    moved = 0
    with _conn() as c:
        for key, stamp in earliest.items():
            moved += c.execute(
                "UPDATE listings SET first_seen=? WHERE dedup_key=? AND first_seen>?",
                (stamp, key, stamp)).rowcount
    return moved


def rekey_phone_listings() -> int:
    """One-time migration for the phone|address dedup key (see make_dedup_key).

    Rows written before that change are keyed on the bare phone. Left alone they'd be
    orphaned — a re-read now computes "phone:X|רגר 5" and would add a SECOND row, and
    replay's delete-by-key would miss the old one. So rename each such row to the key
    it would get today, carrying its ⭐/🗑 votes and fingerprint across. Idempotent:
    rows already scoped, or without a numbered address, are skipped. Returns rows moved.
    """
    moved = 0
    with _conn() as c:
        rows = c.execute("SELECT dedup_key, address FROM listings "
                         "WHERE dedup_key LIKE 'phone:%'").fetchall()
        for old, address in rows:
            if "|" in old:
                continue                              # already scoped
            norm = _norm_addr(address)
            if not norm:
                continue                              # no house number -> phone-only key
            new = f"{old}|{norm}"
            if c.execute("SELECT 1 FROM listings WHERE dedup_key=?", (new,)).fetchone():
                continue                              # target exists; leave the merge to
                                                      # merge_duplicate_listings
            c.execute("UPDATE listings SET dedup_key=? WHERE dedup_key=?", (new, old))
            c.execute("UPDATE OR IGNORE marks SET dedup_key=? WHERE dedup_key=?", (new, old))
            c.execute("DELETE FROM marks WHERE dedup_key=?", (old,))
            c.execute("UPDATE OR IGNORE post_fingerprints SET dedup_key=? WHERE dedup_key=?",
                      (new, old))
            c.execute("INSERT OR IGNORE INTO seen(dedup_key) VALUES (?)", (new,))
            moved += 1
    return moved


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
        for grp in list(groups.values()):
            if len(grp) < 2:
                continue
            for sub in _by_landlord(grp):
                if len(sub) < 2:
                    continue
                removed += _collapse(c, sub, richness)
        removed += _absorb_vague(c, rows)
        return removed


def _absorb_vague(c, rows: list) -> int:
    """Fold a listing whose address has NO house number into the numbered listing that
    is plainly the same flat: same landlord, same price, same number of free rooms.

    One flat often arrives twice, once described and once addressed —
    `מול שער האוניברסיטה` and `רחוב רגר 153` from 054-3972962, both 1300₪ for 3 rooms.
    `is_duplicate` collapses the vague read at INGEST when the phone is already known,
    but only in that direction: if the vague post came FIRST, the numbered one is
    genuinely new and both rows are kept.

    DELIBERATELY STRICTER THAN THE ADDRESS MERGE. There the address is corroborating
    evidence; here there is none, so a phone alone is not enough — a landlord with two
    flats who describes one vaguely would lose it. Requiring price AND room count to
    match as well is what makes this safe, and an ambiguous case (several numbered rows
    fit) is skipped rather than guessed at.

    The NUMBERED row always survives, whatever `richness` would say: it is the one that
    knows where the flat is."""
    numbered, vague = [], []
    for r in rows:
        (numbered if _norm_addr(r[1]) else vague).append(r)
    removed = 0
    for v in vague:
        nums = _contact_numbers(v[5])
        if not nums or v[2] is None or v[3] is None:
            continue                       # no phone, or nothing to corroborate with
        fits = [n for n in numbered
                if _contact_numbers(n[5]) & nums and n[2] == v[2] and n[3] == v[3]]
        if len(fits) != 1:
            continue                       # none, or ambiguous — leave it alone
        keep, dead = fits[0][0], v[0]
        if keep == dead:
            continue
        c.execute("UPDATE OR IGNORE marks SET dedup_key=? WHERE dedup_key=?", (keep, dead))
        c.execute("DELETE FROM marks WHERE dedup_key=?", (dead,))
        c.execute("DELETE FROM post_fingerprints WHERE dedup_key=?", (dead,))
        c.execute("DELETE FROM listings WHERE dedup_key=?", (dead,))
        removed += 1
    return removed


def _by_landlord(grp: list) -> list:
    """Split one address's rows into per-landlord clusters.

    ONE ADDRESS IS NOT ONE FLAT. Grouping by address alone was survivable while the key
    held the post's raw wording and collisions were rare; once it became `canonical
    street|number`, far more rows collide — and in a student building several landlords
    advertise different flats at the same number. Measured 2026-08-02: of 40 colliding
    groups, 11 held more than one contact and merging them would have DELETED 17 real
    listings — וינגייט 64 alone has three separate landlords.

    Refusing the whole group was too blunt, though: אברהם אבינו 3 holds one landlord's
    flat twice AND a different landlord's, so the real duplicate survived. Rows are
    therefore clustered by SHARED PHONE NUMBER — two rows join when their number sets
    intersect, which is what links "054-3376992, 052-3252255" to "054-3376992".

    A row with no contact at all joins the single cluster if there is exactly one;
    with two rival landlords present there is no way to say whose flat it is, so it
    stays on its own rather than being guessed at."""
    clusters: list = []                      # [ (numbers, rows) ]
    orphans = []
    for r in grp:
        nums = _contact_numbers(r[5])
        if not nums:
            orphans.append(r)
            continue
        hit = [cl for cl in clusters if cl[0] & nums]
        if not hit:
            clusters.append([set(nums), [r]])
            continue
        merged_nums, merged_rows = set(nums), [r]
        for cl in hit:
            merged_nums |= cl[0]
            merged_rows += cl[1]
            clusters.remove(cl)
        clusters.append([merged_nums, merged_rows])
    if len(clusters) == 1:
        clusters[0][1] += orphans
    else:
        clusters += [[set(), [o]] for o in orphans]
    return [rows for _nums, rows in clusters]


def _collapse(c, grp: list, richness) -> int:
    """Keep the richest row of `grp`, migrate its votes, delete the rest."""
    removed = 0
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
    """Write (or ENRICH) a listing row.

    Enrichment, not replacement: a later, thinner read of the same flat — the LLM
    missed the price this time, or a second source knows only the address — must
    never blank out a field we already had. Every nullable column is written as
    COALESCE(new, old), so a row only ever gains detail. Non-nullable status fields
    (status/tier/score/walk) DO overwrite: those are recomputed every time and the
    fresh verdict is the right one."""
    e = res.extract
    imgs = json.dumps(res.images or [])
    am = json.dumps(res.amenities or {}, ensure_ascii=False)
    with _conn() as c:
        c.execute(
            """INSERT INTO listings
               (dedup_key,status,location_tier,price_per_room,available_rooms,total_roommates,
                address,walk_minutes,lease_start,contact,summary,source_url,"group",
                price_from_comment,score,images,floor,furnished,balcony,elevator,geocode_source,
                amenities)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dedup_key) DO UPDATE SET
                 status=excluded.status,
                 location_tier=excluded.location_tier,
                 score=excluded.score,
                 walk_minutes=excluded.walk_minutes,
                 geocode_source=excluded.geocode_source,
                 price_from_comment=excluded.price_from_comment,
                 -- everything below only ever GAINS detail (see the docstring)
                 price_per_room=COALESCE(excluded.price_per_room, price_per_room),
                 available_rooms=COALESCE(excluded.available_rooms, available_rooms),
                 total_roommates=COALESCE(excluded.total_roommates, total_roommates),
                 address=COALESCE(excluded.address, address),
                 lease_start=COALESCE(excluded.lease_start, lease_start),
                 contact=COALESCE(excluded.contact, contact),
                 summary=COALESCE(excluded.summary, summary),
                 source_url=COALESCE(excluded.source_url, source_url),
                 "group"=COALESCE(excluded."group", "group"),
                 floor=COALESCE(excluded.floor, floor),
                 furnished=COALESCE(excluded.furnished, furnished),
                 balcony=COALESCE(excluded.balcony, balcony),
                 elevator=COALESCE(excluded.elevator, elevator),
                 -- a JSON blob is "empty" as '[]'/'{}', which COALESCE can't see
                 images=CASE WHEN excluded.images IN ('[]','') THEN images
                             ELSE excluded.images END,
                 amenities=CASE WHEN excluded.amenities IN ('{}','') THEN amenities
                                ELSE excluded.amenities END""",
            (res.dedup_key, res.status.value, res.location_tier,
             e.price_per_room_ils, e.available_rooms_count, e.total_roommates_in_apt,
             e.street_address_or_neighborhood, res.walk_minutes, e.lease_start_date,
             e.contact_phone_or_link, e.summary_hebrew, res.source_url, res.group,
             1 if e.price_from_comment else 0, res.score, imgs,
             e.floor, _tri(e.furnished), e.balcony_or_garden, _tri(e.has_elevator),
             res.geo_source, am),
        )


def listing_amenities(dedup_key: str) -> dict:
    """The stored amenity walk-times for a listing — {} for a row saved before the
    column existed, or for one where nothing resolved."""
    with _conn() as c:
        row = c.execute("SELECT amenities FROM listings WHERE dedup_key=?",
                        (dedup_key,)).fetchone()
    try:
        return json.loads(row[0]) if row and row[0] else {}
    except Exception:
        return {}
