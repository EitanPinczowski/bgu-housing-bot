"""
Optional Google Sheets sink — a browsable, organized DB of the listings the bot
finds, with its own row-level dedup so nothing is added twice.

This is ADDITIVE: SQLite (storage.py) stays the fast local dedup/cache. The
Sheet is a durable, shareable record you can sort/filter by hand and that
survives a local reset.

Auth is a **service account** (no OAuth browser flow):
  1. Create a service account in Google Cloud, enable the Sheets API, download
     its JSON key to  auth/google_service_account.json  (auth/ is git-ignored).
  2. Create a Google Sheet and SHARE it (Editor) with the service account's
     email (the client_email in that JSON).
  3. Put the sheet id (the long id in its URL) in .env as GOOGLE_SHEET_ID.
Disabled (silently no-op) until both the id and the credentials file exist, so
the bot runs fine with or without it.
"""
from __future__ import annotations
import os
import time
from datetime import datetime

import config
from models import PipelineResult

# HTTP statuses worth retrying — transient Google backend / rate-limit errors.
# A blip here must NOT disable the sheet for the rest of the run (that bug lost
# whole runs of listings): we retry with backoff instead.
_RETRY_STATUS = {429, 500, 502, 503}


def _is_transient(exc) -> bool:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) in _RETRY_STATUS


def _retry(fn, tries: int = 4, base: float = 1.5):
    """Call fn(), retrying transient API errors with exponential backoff. Non-
    transient errors (bad creds, not-shared, bad range) raise immediately."""
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:
            if not _is_transient(exc) or i == tries - 1:
                raise
            time.sleep(base * (2 ** i))

HEADERS = ["first_seen", "status", "tier", "price_per_room", "rooms_free",
           "roommates", "address", "walk_min", "gate", "lease_start", "contact",
           "summary", "source_url", "group", "dedup_key", "mark", "score"]

_DEDUP_COL = HEADERS.index("dedup_key") + 1   # 1-based column of dedup_key
_MARK_COL = HEADERS.index("mark") + 1         # user triage: saved / dismissed

_ws = None            # cached worksheet handle
_seen_keys = None     # cached set of dedup_keys already in the sheet
_disabled = False     # set True after a config/auth failure so we stop retrying


def _cred_path() -> str:
    return os.environ.get("GOOGLE_SHEET_CREDENTIALS",
                          str(config.AUTH_DIR / "google_service_account.json"))


def _worksheet():
    """The target worksheet, or None if Sheets isn't configured/available."""
    global _ws, _disabled
    if _ws is not None or _disabled:
        return _ws
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    cred = _cred_path()
    if not sheet_id or not os.path.exists(cred):
        _disabled = True      # not set up — that's fine, stay a no-op
        return None
    try:
        import gspread
        gc = gspread.service_account(filename=cred)
        ws = _retry(lambda: gc.open_by_key(sheet_id).sheet1)
        # Pin the grid to exactly our column count. A wider grid (a stray far cell
        # once pushed this sheet to 609 columns) makes gspread's auto-append place
        # rows in the wrong place; shrinking it here means that can never persist.
        if ws.col_count != len(HEADERS):
            _retry(lambda: ws.resize(cols=len(HEADERS)))
        if _retry(lambda: ws.row_values(1)) != HEADERS:   # (re)write header if outdated
            _retry(lambda: ws.update([HEADERS], "A1"))
        _ws = ws
        return ws
    except Exception as exc:
        # Transient (503/429/network) -> DON'T latch; retry on the next call so a
        # single blip doesn't lose a whole run. Permanent (bad creds / not shared)
        # -> latch _disabled so we stop hammering a sheet we can never reach.
        if _is_transient(exc):
            print(f"[sheets] temporary open failure (will retry next call): {exc}")
        else:
            print(f"[sheets] disabled — could not open the sheet: {exc}")
            _disabled = True
        return None


def _seen() -> set:
    global _seen_keys
    if _seen_keys is None:
        ws = _worksheet()
        try:
            _seen_keys = set(ws.col_values(_DEDUP_COL)) if ws else set()
        except Exception:
            _seen_keys = set()
    return _seen_keys


def _next_row(ws) -> int:
    """1-based row just past the last non-empty dedup_key cell — the authoritative
    data extent. Using the dedup_key column (which every real row fills) instead of
    gspread's auto table-range detection means a stray far cell can't misplace a
    write, so no blank gaps form."""
    return len(_retry(lambda: ws.col_values(_DEDUP_COL))) + 1


def _write_rows(ws, rows: list) -> None:
    """Write rows at an explicitly computed next-row, never via append_row."""
    if not rows:
        return
    start = _next_row(ws)
    end = start + len(rows) - 1
    rng = f"A{start}:{_col_letter(len(HEADERS))}{end}"
    _retry(lambda: ws.update(rows, rng, value_input_option="USER_ENTERED"))


def save_listing(res: PipelineResult) -> None:
    """Append one listing as a row, unless its dedup_key is already in the sheet.
    Best-effort: any failure is logged and swallowed so it never breaks a run."""
    ws = _worksheet()
    if ws is None:
        return
    key = res.dedup_key or ""
    if key and key in _seen():
        return
    e = res.extract
    row = [
        datetime.now().isoformat(timespec="seconds"), res.status.value,
        res.location_tier, e.price_per_room_ils, e.available_rooms_count,
        e.total_roommates_in_apt, e.street_address_or_neighborhood,
        None if res.walk_minutes is None else round(res.walk_minutes),
        res.walk_gate, e.lease_start_date, e.contact_phone_or_link,
        e.summary_hebrew, res.source_url, res.group, key, "", res.score,   # "" = mark
    ]
    try:
        _write_rows(ws, [row])
        _seen().add(key)
    except Exception as exc:
        print(f"[sheets] append failed: {exc}")


def _row_from_db(r) -> list:
    """Map a DB listings row (see the SELECT in sync_from_db) to a full 17-column
    sheet row in HEADERS order. gate isn't stored in the DB (blank); the caller
    fills dedup_key. Length MUST equal len(HEADERS) or columns misalign."""
    (first_seen, status, tier, price, avail, total, addr, walk, lease,
     contact, summary, url, group, score) = r
    return [first_seen, status, tier, price, avail, total, addr,
            None if walk is None else round(walk), "",   # gate
            lease, contact, summary, url, group, "", "", score]   # dedup_key, mark, score


def sync_from_db() -> int:
    """Reconcile the sheet with the local DB: append every stored listing whose
    dedup_key isn't in the sheet yet, in ONE batch. Self-healing — recovers rows
    that a per-post append dropped to a rate-limit/transient error, and avoids
    the burst of per-row calls that caused those errors. Returns rows added."""
    import sqlite3
    ws = _worksheet()
    if ws is None:
        return 0
    # Self-heal: if the grid has drifted — wrong header, or blank rows interspersed
    # among the data (data rows outnumber non-empty dedup_key cells) — a plain append
    # would land in the wrong place. Rebuild the whole sheet from the DB instead.
    try:
        vals = _retry(lambda: ws.get_all_values())
    except Exception as exc:
        print(f"[sheets] sync: could not read sheet: {exc}")
        return 0
    col = _DEDUP_COL - 1
    header = vals[0] if vals else []
    have = set(r[col] for r in vals[1:] if len(r) > col and r[col].strip())
    if header != HEADERS or (len(vals) - 1) > len(have):
        n = rebuild_from_db()
        sort_by_score()
        print(f"[sheets] sync: grid had drifted — rebuilt {n} rows")
        return n
    with sqlite3.connect(config.DB_PATH) as c:
        rows = c.execute(
            """SELECT first_seen, status, location_tier, price_per_room, available_rooms,
                      total_roommates, address, walk_minutes, lease_start, contact,
                      summary, source_url, "group", score, dedup_key
               FROM listings ORDER BY first_seen""").fetchall()
    batch = []
    for r in rows:
        key = r[-1]
        if key and key not in have:
            row = _row_from_db(r[:-1])
            row[HEADERS.index("dedup_key")] = key
            batch.append(row)
            have.add(key)
    if not batch:
        return 0
    try:
        _write_rows(ws, batch)
        _seen().update(r[HEADERS.index("dedup_key")] for r in batch)
        return len(batch)
    except Exception as exc:
        print(f"[sheets] sync append failed: {exc}")
        return 0


def set_mark(dedup_key: str, mark: str, score=None) -> None:
    """Record the group's net vote in the sheet's `mark` column, and (optionally)
    update the `score` column to the vote-adjusted effective score."""
    ws = _worksheet()
    if ws is None or not dedup_key:
        return
    try:
        cell = _retry(lambda: ws.find(dedup_key))    # dedup_key is unique
        if cell:
            _retry(lambda: ws.update_cell(cell.row, _MARK_COL, mark))
            if score is not None:
                _retry(lambda: ws.update_cell(cell.row, HEADERS.index("score") + 1, score))
                sort_by_score()   # a vote changed the rating — keep the sheet ordered
    except Exception as exc:
        print(f"[sheets] set_mark failed: {exc}")


def rebuild_from_db() -> int:
    """Clear the sheet and rewrite EVERY listings row from the DB — for replay
    --apply, where scores/tiers changed and some rows were dropped or added.
    Preserves votes: `score` = vote-adjusted effective score, `mark` = net vote."""
    import sqlite3
    import storage
    ws = _worksheet()
    if ws is None:
        return 0
    with sqlite3.connect(config.DB_PATH) as c:
        rows = c.execute(
            """SELECT first_seen, status, location_tier, price_per_room, available_rooms,
                      total_roommates, address, walk_minutes, lease_start, contact,
                      summary, source_url, "group", score, dedup_key
               FROM listings ORDER BY first_seen""").fetchall()
    body = [list(HEADERS)]
    for r in rows:
        key = r[-1]
        row = _row_from_db(r[:-1])
        row[HEADERS.index("dedup_key")] = key
        adj = storage.mark_adjustment(key)
        row[HEADERS.index("mark")] = (f"+{adj}" if adj > 0 else str(adj)) if adj else ""
        row[HEADERS.index("score")] = storage.effective_score(key, r[13] or 0)
        body.append(row)
    try:
        ws.clear()
        ws.resize(rows=max(300, len(body) + 50), cols=len(HEADERS))
        _retry(lambda: ws.update(body, "A1", value_input_option="USER_ENTERED"))
        global _seen_keys
        _seen_keys = None
        return len(body) - 1
    except Exception as exc:
        print(f"[sheets] rebuild failed: {exc}")
        return 0


def _col_letter(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def sort_by_score() -> None:
    """Sort the sheet's data rows by the `score` (rating) column, highest first,
    leaving the header row in place. Best-effort — logged and swallowed."""
    ws = _worksheet()
    if ws is None:
        return
    try:
        n = len(_retry(lambda: ws.col_values(1)))      # rows incl. header
        if n <= 2:
            return                                     # nothing to sort
        score_col = HEADERS.index("score") + 1
        rng = f"A2:{_col_letter(len(HEADERS))}{n}"     # data rows only, all columns
        _retry(lambda: ws.sort((score_col, "des"), range=rng))
    except Exception as exc:
        print(f"[sheets] sort failed: {exc}")
