"""Does batching change what the LLM returns?

The archive holds every post's SINGLE-CALL extract in posts.parsed_json. Re-extract a
stratified sample through llm.extract_many and compare field by field. This is the only
gate that can answer the question: tests/test_golden.py runs on FROZEN extracts, so it
exercises _classify and never the LLM at all.

GATE: no post may flip is_apartment_ad, and no MATCH-eligible post may lose its price,
rooms, or address.

Usage:  python batch_ab.py [n_per_bucket]
"""
import collections
import sqlite3
import sys

sys.path.insert(0, r"C:\Users\eitan\OneDrive\Desktop\bgu_housing_bot")
from dotenv import load_dotenv  # noqa: E402

# .env is loaded PER ENTRY POINT in this project. Without this the GEMINI_API_KEY
# lookup raises, extract_many falls back to per-post Ollama, and the harness measures
# the local model against the archive instead of measuring batching. It took a
# 10-minute timeout to notice.
load_dotenv(r"C:\Users\eitan\OneDrive\Desktop\bgu_housing_bot\.env")

import config  # noqa: E402
import llm  # noqa: E402

PER_BUCKET = int(sys.argv[1]) if len(sys.argv) > 1 else 5
BUCKETS = ("MATCH", "NEEDS_DATA", "DROP", "NOT_AD")
# The fields a wrong answer would actually cost us. summary_hebrew is free prose and
# lease_start_date is rarely present; neither changes a verdict.
FIELDS = ("is_apartment_ad", "price_per_room_ils", "available_rooms_count",
          "total_roommates_in_apt", "street_address_or_neighborhood",
          "contact_phone_or_link", "floor", "furnished", "balcony_or_garden",
          "has_elevator", "price_from_comment")

con = sqlite3.connect(config.DB_PATH)
con.row_factory = sqlite3.Row
sample = []
for b in BUCKETS:
    rows = con.execute(
        "SELECT raw_text, comments, parsed_json, verdict FROM posts "
        "WHERE verdict=? AND parsed_json IS NOT NULL AND length(raw_text) > 60 "
        "ORDER BY RANDOM() LIMIT ?", (b, PER_BUCKET)).fetchall()
    sample += [r for r in rows]

print("CONTROL = a single Gemini call made now, NOT the archived parsed_json\n")
print(f"sample: {len(sample)} posts "
      f"({collections.Counter(r['verdict'] for r in sample)})")
print(f"batch size: {config.LLM_BATCH_SIZE}\n")

posts = [(r["raw_text"], r["comments"] or "") for r in sample]

# THE CONTROL IS A SINGLE GEMINI CALL MADE RIGHT NOW, not the archived parsed_json.
# The archive was written over days by TWO different models (186 posts on 2026-08-03
# alone came from the Ollama fallback) and by older prompt versions, so comparing
# against it measures model drift, not batching. Measured that way the address field
# "disagreed" 80% of the time — which turned out to say nothing about batches.
old = [llm._extract_gemini(llm.with_comments(t, c)) for t, c in posts]

new = []
for i in range(0, len(posts), config.LLM_BATCH_SIZE):
    new += llm.extract_many(posts[i:i + config.LLM_BATCH_SIZE])

agree = collections.Counter()
total = collections.Counter()
flips, losses = [], []
for r, o, n in zip(sample, old, new):
    for f in FIELDS:
        a, b = getattr(o, f, None), getattr(n, f, None)
        total[f] += 1
        agree[f] += (a == b)
    if o.is_apartment_ad != n.is_apartment_ad:
        flips.append((r["verdict"], o.is_apartment_ad, n.is_apartment_ad,
                      (r["raw_text"] or "")[:60]))
    # a MATCH-eligible post must not LOSE a field it had (gaining is fine)
    if r["verdict"] in ("MATCH", "NEEDS_DATA"):
        for f in ("price_per_room_ils", "available_rooms_count",
                  "street_address_or_neighborhood"):
            if getattr(o, f) is not None and getattr(n, f) is None:
                losses.append((f, getattr(o, f), (r["raw_text"] or "")[:60]))

print("per-field agreement (batched vs a single Gemini call, same posts, now):")
for f in FIELDS:
    pct = 100 * agree[f] / total[f] if total[f] else 0
    flag = "  <-- " if pct < 90 else ""
    print(f"  {f:32} {agree[f]:3}/{total[f]:3}  {pct:5.1f}%{flag}")

print(f"\nGATE 1 — is_apartment_ad flips : {len(flips)}  "
      f"{'PASS' if not flips else 'FAIL'}")
for v, a, b, t in flips[:6]:
    print(f"    [{v}] {a} -> {b}   {t}")
print(f"GATE 2 — MATCH-eligible losses : {len(losses)}  "
      f"{'PASS' if not losses else 'FAIL'}")
for f, was, t in losses[:6]:
    print(f"    lost {f}={was}   {t}")
