"""
Local SQLite store: deduplication + saved listings.

Dedup key prefers the contact phone (survives reposts and cross-posting to
several groups). Falls back to a hash of address+price+rooms. We write
incrementally so a crash mid-run never loses or reprocesses state.
"""
from __future__ import annotations
import hashlib
import re
import sqlite3
from typing import Optional

import config
from models import ListingExtract, PipelineResult

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
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.DB_PATH)
    c.executescript(_SCHEMA)
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


def save_listing(res: PipelineResult) -> None:
    e = res.extract
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO listings
               (dedup_key,status,location_tier,price_per_room,available_rooms,total_roommates,
                address,walk_minutes,lease_start,contact,summary,source_url,"group")
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (res.dedup_key, res.status.value, res.location_tier,
             e.price_per_room_ils, e.available_rooms_count, e.total_roommates_in_apt,
             e.street_address_or_neighborhood, res.walk_minutes, e.lease_start_date,
             e.contact_phone_or_link, e.summary_hebrew, res.source_url, res.group),
        )
