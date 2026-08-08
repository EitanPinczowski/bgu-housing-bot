"""sheets._row_from_db: a DB row maps to exactly len(HEADERS) columns (misalignment
here is what once polluted the sheet), with furnished shown as a Hebrew label."""
import sheets

# a row shaped like the SELECT in sync_from_db/rebuild_from_db, WITHOUT dedup_key
# (…, floor, furnished, balcony, contact, …) — balcony is now the amenity text
_ROW = ("2026-07-20", "MATCH", "GREEN", 1400, 2, 3, "רגר 1", 7.0, "1.10",
        "3", 1, "מרפסת", "050-1234567", "סיכום", "http://x", "grp", 80)


def test_row_from_db_matches_headers_length():
    row = sheets._row_from_db(_ROW)
    assert len(row) == len(sheets.HEADERS)
    assert row[sheets.HEADERS.index("floor")] == "3"
    assert row[sheets.HEADERS.index("furnished")] == "מרוהט"
    assert row[sheets.HEADERS.index("balcony/garden")] == "מרפסת"   # the specific one
    assert row[sheets.HEADERS.index("score")] == 80


def test_balcony_cell_shows_one_and_legacy_fallback():
    def balc(v):
        r = list(_ROW)
        r[11] = v
        return sheets._row_from_db(tuple(r))[sheets.HEADERS.index("balcony/garden")]
    assert balc("גינה") == "גינה"           # shows the single amenity
    assert balc("מרפסת") == "מרפסת"
    assert balc(1) == "מרפסת/גינה"           # legacy bool row -> combined fallback
    assert balc(None) == ""


def test_save_listing_row_matches_headers(monkeypatch):
    """The per-post live append must emit exactly len(HEADERS) columns in order —
    a mismatch here silently misaligns every column of a live-appended row."""
    from models import ListingExtract, PipelineResult, Status
    captured = {}
    monkeypatch.setattr(sheets, "_worksheet", lambda: object())
    monkeypatch.setattr(sheets, "_seen", lambda: set())
    monkeypatch.setattr(sheets, "_write_rows", lambda ws, rows: captured.update(row=rows[0]))
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 1",
                       floor="3", furnished=True, balcony_or_garden="גינה")
    sheets.save_listing(PipelineResult(status=Status.MATCH, dedup_key="k",
                        location_tier="GREEN", score=90, extract=e))
    row = captured["row"]
    assert len(row) == len(sheets.HEADERS)
    assert row[sheets.HEADERS.index("floor")] == "3"
    assert row[sheets.HEADERS.index("furnished")] == "מרוהט"
    assert row[sheets.HEADERS.index("balcony/garden")] == "גינה"
    assert row[sheets.HEADERS.index("dedup_key")] == "k"
    assert row[sheets.HEADERS.index("score")] == 90


# --- the grid has to be big enough BEFORE we write into it --------------------------

class _FakeWS:
    def __init__(self, row_count, used_rows=1):
        self.row_count = row_count
        self._used = used_rows
        self.calls = []

    def col_values(self, _c):
        return ["x"] * self._used            # _next_row = used + 1

    def resize(self, rows=None, cols=None):
        self.calls.append(("resize", rows))
        self.row_count = rows

    def update(self, rows, rng, value_input_option=None):
        self.calls.append(("update", rng))


def test_the_sheet_is_grown_before_a_write_runs_off_its_grid():
    """`APIError: [400]: Range (Sheet1!A393:T393) exceeds grid limits. Max rows: 392` —
    four times. Not transient, so _retry could not help, and save_listing swallows the
    failure, so listings silently stopped reaching the mirror."""
    ws = _FakeWS(row_count=392, used_rows=392)
    sheets._write_rows(ws, [["a"] * len(sheets.HEADERS)])
    kinds = [c[0] for c in ws.calls]
    assert kinds == ["resize", "update"], ws.calls
    assert ws.calls[0][1] == 393 + 50, "grow with the same headroom as the rebuild"


def test_a_sheet_with_room_is_never_resized():
    """resize() downward DELETES rows, and those rows are listings — so it must be
    guarded by the comparison, never issued unconditionally."""
    ws = _FakeWS(row_count=1000, used_rows=10)
    sheets._write_rows(ws, [["a"] * len(sheets.HEADERS)])
    assert [c[0] for c in ws.calls] == ["update"], ws.calls
    assert ws.row_count == 1000


# --- the mirror should read as a table -----------------------------------------------

class _TableWS:
    """A worksheet carrying a native Sheets Table, like the real mirror."""
    id = 7

    def __init__(self, table_end=200, used=413, tables=True):
        self.calls = []
        self._used = used
        self.spreadsheet = self
        self._tables = ([{"tableId": "T1", "name": "Rental_Housing_Listings",
                          "range": {"sheetId": 7, "startRowIndex": 0,
                                    "endRowIndex": table_end,
                                    "startColumnIndex": 0, "endColumnIndex": 20}}]
                        if tables else [])

    def format(self, rng, fmt):
        self.calls.append(("format", rng, fmt))

    def col_values(self, _c):
        return ["x"] * self._used

    def fetch_sheet_metadata(self):
        return {"sheets": [{"properties": {"sheetId": 7}, "tables": self._tables}]}

    def batch_update(self, body):
        self.calls.append(("batch_update", body))


def test_every_cell_is_centred_by_whole_column():
    """Whole columns, not the rows that exist today — Sheets carries column formatting
    onto rows appended later, so tomorrow's listing comes out centred too."""
    ws = _TableWS()
    sheets._format_as_table(ws)
    rng, fmt = ws.calls[0][1], ws.calls[0][2]
    assert ":" in rng and not any(ch.isdigit() for ch in rng), rng
    assert fmt == {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}


def test_the_table_is_grown_to_the_last_used_row():
    """A Sheets table does NOT grow with appended rows. Measured 2026-08-08: it covered
    rows 1-200 while 413 were in use, so everything past 200 sat outside it."""
    ws = _TableWS(table_end=200, used=413)
    sheets._format_as_table(ws)
    upd = [c for c in ws.calls if c[0] == "batch_update"]
    assert len(upd) == 1, ws.calls
    req = upd[0][1]["requests"][0]["updateTable"]
    assert req["table"]["range"]["endRowIndex"] == 413
    assert req["table"]["tableId"] == "T1"
    # only the RANGE is sent, so the user's colours/name/columns survive untouched
    assert req["fields"] == "range"


def test_a_table_that_already_covers_everything_is_left_alone():
    ws = _TableWS(table_end=413, used=413)
    sheets._format_as_table(ws)
    assert not [c for c in ws.calls if c[0] == "batch_update"]


def test_a_sheet_with_no_table_is_not_given_one():
    """The table is the user's; this only ever extends what is already there."""
    ws = _TableWS(tables=False)
    sheets._format_as_table(ws)
    assert not [c for c in ws.calls if c[0] == "batch_update"]


def test_formatting_never_breaks_the_mirror():
    """save_listing swallows failures by design; cosmetics must not be the one thing
    that loses a listing."""
    class Boom(_TableWS):
        def format(self, rng, fmt):
            raise RuntimeError("no permission")

        def fetch_sheet_metadata(self):
            raise RuntimeError("network")

    sheets._format_as_table(Boom())        # must not raise
