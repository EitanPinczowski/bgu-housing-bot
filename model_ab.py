"""Does a DIFFERENT Gemini model change what we extract?

The free daily quota is per project per MODEL, so a second model carries its own
allowance and spilling over to it when the first is exhausted roughly doubles capacity
(measured 2026-08-06: `gemini-3.5-flash-lite` RPD 500, `gemini-3.1-flash-lite` RPD 500).
That is only worth having if the second model reads Hebrew housing posts as well as the
first — a model that loses prices costs more than a model that runs out early, which is
exactly how the batching question was decided.

GATE: no post may flip is_apartment_ad, and no MATCH-eligible post may lose its price,
rooms, or address.

Usage:  python model_ab.py [n_per_bucket] [--candidate MODEL]

Two lessons carried over from batch_ab.py, both learned the hard way:

  * THE CONTROL IS A LIVE CALL, NOT THE ARCHIVE. `posts.parsed_json` was written over
    days by two different models (186 posts on 2026-08-03 came from the Ollama fallback)
    and by older prompts, so comparing against it measures drift, not the model. Measured
    that way the address field "disagreed" 80% of the time and said nothing.
  * MEASURE THE NOISE FLOOR. The same model asked twice does not always answer
    identically, so a candidate's disagreement is only meaningful against how much the
    control disagrees with ITSELF. In the batching run price and rooms agreed 100%
    call-to-call, which is what made their drop attributable to batching rather than to
    variance.

Costs quota: 2 calls per post on the control model (answer + noise floor) and 1 on the
candidate.

DO NOT RUN THIS WHILE A SCRAPE IS RUNNING. `GEMINI_MIN_INTERVAL_SEC` paces requests
per PROCESS, not per project, and the RPM limit is per project — so two processes each
pacing at 4.5 s issue ~27/min against a limit of 15 and manufacture the very 429s this
is meant to avoid. Same applies to `replay.py --llm`.
"""
import collections
import sqlite3
import sys

sys.path.insert(0, r"C:\Users\eitan\OneDrive\Desktop\bgu_housing_bot")
from dotenv import load_dotenv  # noqa: E402

# .env is loaded PER ENTRY POINT in this project; without it the GEMINI_API_KEY lookup
# raises and the harness silently measures Ollama instead.
load_dotenv(r"C:\Users\eitan\OneDrive\Desktop\bgu_housing_bot\.env")

import config  # noqa: E402
import llm  # noqa: E402

PER_BUCKET = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5
CANDIDATE = (sys.argv[sys.argv.index("--candidate") + 1]
             if "--candidate" in sys.argv else "gemini-3.1-flash-lite")
CONTROL = config.GEMINI_MODEL
BUCKETS = ("MATCH", "NEEDS_DATA", "DROP", "NOT_AD")
FIELDS = ("is_apartment_ad", "price_per_room_ils", "available_rooms_count",
          "total_roommates_in_apt", "street_address_or_neighborhood",
          "contact_phone_or_link", "floor", "furnished", "balcony_or_garden",
          "has_elevator", "price_from_comment")
# The four the hard filters actually run on — a wrong answer here changes a verdict.
CRITICAL = ("is_apartment_ad", "price_per_room_ils", "available_rooms_count",
            "street_address_or_neighborhood")


def _ask(model: str, text: str):
    """One single-post call against a named model."""
    real = config.GEMINI_MODEL
    config.GEMINI_MODEL = model
    try:
        return llm._extract_gemini(text)
    finally:
        config.GEMINI_MODEL = real


con = sqlite3.connect(config.DB_PATH)
con.row_factory = sqlite3.Row
sample = []
for b in BUCKETS:
    sample += con.execute(
        "SELECT raw_text, comments, verdict FROM posts "
        "WHERE verdict=? AND parsed_json IS NOT NULL AND length(raw_text) > 60 "
        "ORDER BY RANDOM() LIMIT ?", (b, PER_BUCKET)).fetchall()

posts = [(r["raw_text"], r["comments"] or "") for r in sample]
texts = [llm.with_comments(t, c) for t, c in posts]

print(f"CONTROL   {CONTROL}   (live call, NOT the archived parsed_json)")
print(f"CANDIDATE {CANDIDATE}")
print(f"sample: {len(sample)} posts "
      f"({collections.Counter(r['verdict'] for r in sample)})")
print(f"cost: {2 * len(texts)} calls on the control + {len(texts)} on the candidate\n")

control = [_ask(CONTROL, t) for t in texts]
repeat = [_ask(CONTROL, t) for t in texts]          # the noise floor
cand = [_ask(CANDIDATE, t) for t in texts]


def _agreement(a_list, b_list):
    agree, total = collections.Counter(), collections.Counter()
    for a, b in zip(a_list, b_list):
        for f in FIELDS:
            total[f] += 1
            agree[f] += (getattr(a, f, None) == getattr(b, f, None))
    return agree, total


noise_a, noise_t = _agreement(control, repeat)
cand_a, cand_t = _agreement(control, cand)

print(f"{'field':32} {'noise floor':>12} {'candidate':>12}")
for f in FIELDS:
    nf = 100 * noise_a[f] / noise_t[f] if noise_t[f] else 0
    cf = 100 * cand_a[f] / cand_t[f] if cand_t[f] else 0
    # Only flag where the candidate is WORSE than the control's own variance — that is
    # the difference attributable to the model rather than to run-to-run noise.
    flag = "  <-- " if cf < nf - 5 and f in CRITICAL else ""
    print(f"  {f:30} {nf:11.0f}% {cf:11.0f}%{flag}")

flips, losses = [], []
for r, o, n in zip(sample, control, cand):
    if o.is_apartment_ad != n.is_apartment_ad:
        flips.append((r["verdict"], o.is_apartment_ad, n.is_apartment_ad,
                      (r["raw_text"] or "")[:60]))
    if r["verdict"] in ("MATCH", "NEEDS_DATA"):
        for f in ("price_per_room_ils", "available_rooms_count",
                  "street_address_or_neighborhood"):
            if getattr(o, f) is not None and getattr(n, f) is None:
                losses.append((f, getattr(o, f), (r["raw_text"] or "")[:60]))

print(f"\nGATE 1 — is_apartment_ad flips : {len(flips)}  "
      f"{'PASS' if not flips else 'FAIL'}")
for v, a, b, t in flips[:6]:
    print(f"    [{v}] {a} -> {b}   {t}")
print(f"GATE 2 — MATCH-eligible losses : {len(losses)}  "
      f"{'PASS' if not losses else 'FAIL'}")
for f, was, t in losses[:6]:
    print(f"    lost {f}={was}   {t}")
print(f"\nn={len(sample)} — small. Read the losses, not just the percentages.")
