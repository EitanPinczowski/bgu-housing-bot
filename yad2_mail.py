"""Yad2 as a second source — via its own saved-search ALERT EMAILS, read over IMAP.

    python yad2_mail.py                 # dry run: show what would be processed
    python yad2_mail.py --dump          # write each extracted block to data/yad2_dump/
    python yad2_mail.py --live          # process for real (saves + alerts)
    python yad2_mail.py --days 3        # how far back to look (default 2)

WHY EMAIL AND NOT THE SITE. Scraping Yad2 is a closed decision, recorded in the
`dead-ends` skill: every endpoint sits behind Radware Bot Manager, so the only ways in are
CAPTCHA-solving or detection evasion — both forbidden — and it would risk the **home IP the
Facebook scraper depends on**, which is the account this project cannot afford to lose. The
same note names the legitimate route, which is this one: Yad2 will email you your own saved
search, and reading your own mailbox needs nobody's permission.

WHY THERE IS NO YAD2 HTML PARSER HERE. The project already owns a component that turns
free Hebrew apartment text into a structured listing — `llm.py`, behind `pipeline`. Writing
a second, bespoke extractor for Yad2's markup would be a parallel thing to maintain and it
would break the first time they change a template. So this module does the narrow part
email requires — connect, select the right messages, strip to text, split into per-listing
blocks — and hands each block to `pipeline.process_post`, which then applies exactly the
same filters, geocoding, zone rules and scoring as a Facebook post. A Yad2 flat and a
Facebook flat are judged by one set of rules because they go through one pipeline.

CREDENTIALS. Read from the environment, never arguments and never hard-coded:

    YAD2_IMAP_HOST      e.g. imap.gmail.com
    YAD2_IMAP_USER      the mailbox address
    YAD2_IMAP_PASSWORD  an APP PASSWORD, not your account password
    YAD2_IMAP_FOLDER    optional, default INBOX
    YAD2_FROM           optional sender filter, default "yad2"

Use a dedicated address or a Gmail app password, and give it a filter so only Yad2 alerts
land there. The connection is read-only (`readonly=True`), so this can never delete, move
or even mark your mail as read.

DRY-RUN BY DEFAULT, matching `main.py`: without `--live` it prints what it would process
and writes nothing. Run `--dump` first against your real mailbox — the block splitting is
the one part that depends on Yad2's template, and reading a few real dumps is how you find
out whether it is right before anything reaches the DB.

WORTH MEASURING BEFORE TRUSTING. Yad2's inventory is largely whole flats and broker
listings, while this bot targets the שותפים (flatshare) market, so the yield may be poor.
`--dump` and a dry run tell you that for free; a low MATCH rate here is information, not a
fault to fix.
"""
from __future__ import annotations

import argparse
import email
import imaplib
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header

from dotenv import load_dotenv

load_dotenv()

import config  # noqa: E402
import pipeline  # noqa: E402

DUMP_DIR = config.DATA_DIR / "yad2_dump"

# A Yad2 alert lists several flats in one message. These are the seams between them; the
# list is deliberately loose because the template WILL change, and a missed seam merges two
# flats into one block rather than losing either — the same failure the Facebook scraper
# already handles in `_clean_story`, and the reason `--dump` exists.
_BLOCK_SPLIT = re.compile(
    r"(?:\n\s*){2,}|"                       # a blank-line gap
    r"(?=\bדירה\b.{0,40}\bבבאר שבע\b)",      # a new "flat in Beer Sheva" heading
)
# NOT the item URL. Splitting before `yad2.co.il/item/…` looks like the natural seam and is
# exactly backwards: the link FOLLOWS the flat it belongs to, so cutting there strands every
# permalink in a block of its own, each short enough for the length floor to drop — which in
# testing took a two-flat email down to ZERO blocks.

# Boilerplate that is not part of any flat. Dropped before splitting so it cannot become a
# block of its own and reach the LLM as a "post".
_BOILERPLATE = re.compile(
    r"(?:הסר(?:ה)? מרשימת התפוצה|להסרה מרשימת|unsubscribe|כל הזכויות שמורות|"
    r"מדיניות הפרטיות|תנאי שימוש|לצפייה בדוא\"ל|view this email)",
    re.I,
)

_TAG = re.compile(r"<[^>]+>")
_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def _text_from_html(html: str) -> str:
    """HTML email to plain text, without adding a parser dependency.

    Deliberately crude: block tags become newlines so the block splitter has seams to find,
    everything else is dropped. The LLM reads prose, not markup, and anything this loses
    (styling, tracking pixels, layout tables) is not part of a flat."""
    s = _STYLE.sub(" ", html)
    s = re.sub(r"<br\s*/?>|</(p|div|tr|td|li|h\d)>", "\n", s, flags=re.I)
    s = _TAG.sub(" ", s)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(entity, char)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def message_text(msg) -> str:
    """The best available text for one message: prefer text/plain, fall back to HTML."""
    plain, html = [], []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            body = part.get_payload(decode=True)
            if body is None:
                continue
            text = body.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        (plain if ctype == "text/plain" else html).append(text)
    if plain:
        return "\n\n".join(plain).strip()
    return _text_from_html("\n".join(html))


def listing_blocks(text: str) -> list:
    """Split one alert email into one text block per flat.

    Blocks shorter than 40 characters are dropped: a nav link or a stray heading is not a
    flat, and handing it to the LLM would spend quota to be told so."""
    if not text:
        return []
    out = []
    for chunk in _BLOCK_SPLIT.split(text):
        if not chunk:
            continue
        chunk = "\n".join(ln for ln in chunk.splitlines()
                          if not _BOILERPLATE.search(ln)).strip()
        if len(chunk) >= 40:
            out.append(chunk)
    return out


def _subject(msg) -> str:
    try:
        return str(make_header(decode_header(msg.get("Subject") or "")))
    except Exception:
        return msg.get("Subject") or ""


def fetch(days: int = 2) -> list:
    """[(subject, text)] for Yad2 alert mail from the last `days`. READ-ONLY.

    Raises RuntimeError with a readable message when the mailbox is not configured, rather
    than a bare imaplib error — this is the first thing anyone runs and the failure should
    say what to do."""
    host = os.environ.get("YAD2_IMAP_HOST")
    user = os.environ.get("YAD2_IMAP_USER")
    password = os.environ.get("YAD2_IMAP_PASSWORD")
    if not (host and user and password):
        raise RuntimeError(
            "mailbox not configured — set YAD2_IMAP_HOST / YAD2_IMAP_USER / "
            "YAD2_IMAP_PASSWORD in .env (use an APP PASSWORD, never your account "
            "password). See this module's docstring.")
    folder = os.environ.get("YAD2_IMAP_FOLDER", "INBOX")
    sender = os.environ.get("YAD2_FROM", "yad2")
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")

    out = []
    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, password)
        # readonly: this must never delete, move, or even mark mail as read. It is the
        # user's real inbox and the project's rule for Facebook applies here too.
        conn.select(folder, readonly=True)
        typ, data = conn.search(None, "SINCE", since, "FROM", sender)
        if typ != "OK":
            return out
        for num in (data[0].split() if data and data[0] else []):
            typ, raw = conn.fetch(num, "(BODY.PEEK[])")     # PEEK: does not set \\Seen
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            out.append((_subject(msg), message_text(msg)))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="process for real (saves + alerts). Default is a dry run.")
    ap.add_argument("--dump", action="store_true",
                    help="write each extracted block to data/yad2_dump/ and stop")
    ap.add_argument("--days", type=int, default=2, help="how far back to look (default 2)")
    args = ap.parse_args(argv)

    try:
        mails = fetch(args.days)
    except RuntimeError as exc:
        print(f"[yad2] {exc}")
        return 2
    except Exception as exc:
        print(f"[yad2] mailbox unreachable: {type(exc).__name__}: {exc}")
        return 2

    blocks = []
    for subject, text in mails:
        found = listing_blocks(text)
        print(f"[yad2] {subject[:70]!r} -> {len(found)} block(s)")
        blocks.extend(found)

    print(f"[yad2] {len(mails)} mail(s), {len(blocks)} listing block(s), "
          f"mode: {'LIVE' if args.live else 'dry run'}")

    if args.dump:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        for i, b in enumerate(blocks, 1):
            (DUMP_DIR / f"block-{i:03d}.txt").write_text(b, encoding="utf-8")
        print(f"[yad2] wrote {len(blocks)} block(s) to {DUMP_DIR} — read a few before --live")
        return 0

    if not args.live:
        for b in blocks[:3]:
            print("-" * 60)
            print(b[:400])
        if blocks:
            print("-" * 60)
            print("[yad2] dry run — nothing saved. Use --dump to inspect all blocks, "
                  "then --live.")
        return 0

    counts: dict = {}
    for b in blocks:
        try:
            res = pipeline.process_post(b, source_url=None, group="yad2")
            counts[res.status.value] = counts.get(res.status.value, 0) + 1
        except Exception as exc:
            print(f"[yad2] pipeline error on a block: {type(exc).__name__}: {exc}")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
