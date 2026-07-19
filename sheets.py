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
from datetime import datetime

import config
from models import PipelineResult

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
        ws = gc.open_by_key(sheet_id).sheet1
        if ws.row_values(1) != HEADERS:      # (re)write header if missing/outdated
            ws.update([HEADERS], "A1")
        _ws = ws
        return ws
    except Exception as exc:
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
        ws.append_row(row, value_input_option="USER_ENTERED")
        _seen().add(key)
    except Exception as exc:
        print(f"[sheets] append failed: {exc}")


def set_mark(dedup_key: str, mark: str, score=None) -> None:
    """Record the group's net vote in the sheet's `mark` column, and (optionally)
    update the `score` column to the vote-adjusted effective score."""
    ws = _worksheet()
    if ws is None or not dedup_key:
        return
    try:
        cell = ws.find(dedup_key)          # dedup_key is unique
        if cell:
            ws.update_cell(cell.row, _MARK_COL, mark)
            if score is not None:
                ws.update_cell(cell.row, HEADERS.index("score") + 1, score)
    except Exception as exc:
        print(f"[sheets] set_mark failed: {exc}")
