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
