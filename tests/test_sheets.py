"""sheets._row_from_db: a DB row maps to exactly len(HEADERS) columns (misalignment
here is what once polluted the sheet), with furnished shown as a Hebrew label."""
import sheets

# a row shaped like the SELECT in sync_from_db/rebuild_from_db, WITHOUT dedup_key
_ROW = ("2026-07-20", "MATCH", "GREEN", 1400, 2, 3, "רגר 1", 7.0, "1.10",
        "3", 1, "050-1234567", "סיכום", "http://x", "grp", 80)


def test_row_from_db_matches_headers_length():
    row = sheets._row_from_db(_ROW)
    assert len(row) == len(sheets.HEADERS)
    assert row[sheets.HEADERS.index("floor")] == "3"
    assert row[sheets.HEADERS.index("furnished")] == "מרוהט"
    assert row[sheets.HEADERS.index("score")] == 80


def test_furnished_labels():
    def furn(v):
        r = list(_ROW)
        r[10] = v
        return sheets._row_from_db(tuple(r))[sheets.HEADERS.index("furnished")]
    assert furn(1) == "מרוהט"
    assert furn(0) == "לא מרוהט"
    assert furn(None) == ""
