"""Yad2 alert-email ingestion, tested entirely offline.

No IMAP, no network, no credentials — `fetch()` is the only part that touches a mailbox and
it is exercised through its failure path. Everything else (decode, strip, split) is pure
text handling, which is where the bugs live.

The block splitting is the one part that depends on Yad2's template, so these fixtures are
a GUESS at that template until someone runs `--dump` against real mail. They pin the
behaviour that must hold whatever the markup turns out to be: boilerplate never becomes a
flat, one email yields one block per flat, and nothing reaches the pipeline in a dry run.
"""
import email

import pytest

import yad2_mail


def _msg(parts):
    """A multipart message from [(content_type, body)]."""
    raw = ['MIME-Version: 1.0', 'Subject: =?UTF-8?B?15PXmdeo15XXqiDXkdeR15DXqA==?=',
           'Content-Type: multipart/alternative; boundary="B"', '', '--B']
    for ctype, body in parts:
        raw += [f'Content-Type: {ctype}; charset="utf-8"',
                'Content-Transfer-Encoding: 8bit', '', body, '--B']
    raw[-1] = '--B--'
    # FROM BYTES, exactly as `fetch()` does. Built from a str instead, the email package
    # re-encodes an 8bit payload with raw-unicode-escape, so `get_payload(decode=True)`
    # returns a literal "\\u05d3…" and every Hebrew assertion fails against a fixture that
    # does not resemble production. The same class of mistake as a test that builds its
    # timestamp on the local clock while the code reads UTC.
    return email.message_from_bytes("\n".join(raw).encode("utf-8"))


def test_plain_text_is_preferred_over_html():
    """Both parts carry the same flat; the plain one needs no tag-stripping, so it is the
    better source and must win."""
    msg = _msg([("text/plain", "דירת 3 חדרים ברגר 90, 2500 שקל"),
                ("text/html", "<div>דירת 3 חדרים ברגר 90</div>")])
    assert yad2_mail.message_text(msg) == "דירת 3 חדרים ברגר 90, 2500 שקל"


def test_html_is_used_when_there_is_no_plain_part():
    """Yad2 mail is likely HTML-only. Block tags must become newlines, or the splitter has
    no seams and the whole email collapses into one block."""
    msg = _msg([("text/html",
                 "<style>.x{color:red}</style><div>דירה ברגר 90</div>"
                 "<div>2,500 ש\"ח</div><script>t()</script>")])
    text = yad2_mail.message_text(msg)
    assert "רגר 90" in text and "2,500" in text
    assert "color:red" not in text and "t()" not in text, "style/script leaked into the text"
    assert "<" not in text and ">" not in text, "markup survived"


def test_entities_are_decoded():
    msg = _msg([("text/html", "<p>3&nbsp;חדרים &amp; מרפסת</p>")])
    text = yad2_mail.message_text(msg)
    assert "&nbsp;" not in text and "&amp;" not in text and "&" in text


def test_one_email_splits_into_one_block_per_flat():
    """An alert lists several flats. Each must reach the pipeline separately, or the LLM
    is handed two flats as one post — the exact failure `_clean_story` exists for on the
    Facebook side."""
    text = ("דירה 3 חדרים בבאר שבע\nרגר 90\n2,500 ש\"ח\nhttps://www.yad2.co.il/item/aaa\n\n"
            "דירה 4 חדרים בבאר שבע\nמצדה 12\n3,100 ש\"ח\nhttps://www.yad2.co.il/item/bbb")
    blocks = yad2_mail.listing_blocks(text)
    assert len(blocks) >= 2, blocks
    assert any("רגר 90" in b for b in blocks)
    assert any("מצדה 12" in b for b in blocks)
    assert not any("רגר 90" in b and "מצדה 12" in b for b in blocks), "two flats in one block"


def test_boilerplate_never_becomes_a_flat():
    """An unsubscribe footer is not an apartment. Letting one through spends Gemini quota
    to be told so, and the daily budget is 480."""
    text = ("דירה 3 חדרים בבאר שבע ברחוב רגר 90 במחיר 2500 שקל לחודש\n\n"
            "להסרה מרשימת התפוצה לחצו כאן\nכל הזכויות שמורות ליד2\nמדיניות הפרטיות")
    blocks = yad2_mail.listing_blocks(text)
    assert len(blocks) == 1, blocks
    assert "רגר 90" in blocks[0]
    assert "הסרה" not in blocks[0] and "הזכויות שמורות" not in blocks[0]


def test_short_fragments_are_dropped():
    """A nav link or a stray heading is not a flat."""
    assert yad2_mail.listing_blocks("דירות\n\nחיפוש\n\nהתחברות") == []
    assert yad2_mail.listing_blocks("") == []
    assert yad2_mail.listing_blocks(None) == []


def test_a_missing_mailbox_says_what_to_do(monkeypatch):
    """This is the first thing anyone runs. It must not surface a bare imaplib error, and
    it must never invent a credential."""
    for var in ("YAD2_IMAP_HOST", "YAD2_IMAP_USER", "YAD2_IMAP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError) as exc:
        yad2_mail.fetch()
    msg = str(exc.value)
    assert "APP PASSWORD" in msg and "YAD2_IMAP_HOST" in msg


def test_unconfigured_main_exits_cleanly_without_touching_the_pipeline(monkeypatch, capsys):
    """A missing mailbox is a configuration state, not a crash — and nothing may reach the
    DB on the way out."""
    for var in ("YAD2_IMAP_HOST", "YAD2_IMAP_USER", "YAD2_IMAP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(yad2_mail.pipeline, "process_post",
                        lambda *a, **k: pytest.fail("the pipeline must not be called"))
    assert yad2_mail.main([]) == 2
    assert "not configured" in capsys.readouterr().out


def test_dry_run_is_the_default_and_writes_nothing(monkeypatch, capsys):
    """Same rule as `main.py --live`: reading is safe, writing is opt-in. A source that
    saved on first run would put unreviewed Yad2 text straight into the listings table."""
    monkeypatch.setattr(yad2_mail, "fetch",
                        lambda days=2: [("alert", "דירה 3 חדרים ברגר 90 במחיר 2500 שקל לחודש")])
    monkeypatch.setattr(yad2_mail.pipeline, "process_post",
                        lambda *a, **k: pytest.fail("dry run must not process"))
    assert yad2_mail.main([]) == 0
    assert "dry run" in capsys.readouterr().out


def test_live_sends_each_block_through_the_normal_pipeline(monkeypatch, capsys):
    """A Yad2 flat is judged by the SAME rules as a Facebook flat — filters, geocoding,
    zone, score — because it goes through the same `process_post`. `group="yad2"` is what
    makes the source auditable afterwards."""
    seen = []

    class _Res:
        class status:
            value = "MATCH"

    monkeypatch.setattr(yad2_mail, "fetch", lambda days=2: [
        ("alert", "דירה 3 חדרים בבאר שבע ברגר 90 במחיר 2500 שקל לחודש\n\n"
                  "דירה 4 חדרים בבאר שבע במצדה 12 במחיר 3100 שקל לחודש")])
    monkeypatch.setattr(yad2_mail.pipeline, "process_post",
                        lambda text, **kw: seen.append((text, kw)) or _Res())
    assert yad2_mail.main(["--live"]) == 0
    assert len(seen) == 2, seen
    assert all(kw.get("group") == "yad2" for _, kw in seen)
    assert "MATCH: 2" in capsys.readouterr().out
