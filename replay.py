"""
Replay the classifier over every archived post — re-test filter / zone / threshold
/ scoring changes against your whole history, WITHOUT re-scraping Facebook.

    python replay.py            # reuse the stored LLM parse, re-run classify+score
                                #   (fast, no LLM, no quota) — for zone/threshold/score edits
    python replay.py --llm      # re-run the LLM extraction too — for prompt.py/llm.py edits
                                #   (uses Gemini quota)
    python replay.py --changed  # only list posts whose verdict/score changed

Read-only: never writes to the DB/Sheet and never sends Telegram. It reports what
the CURRENT code + config would decide for each stored post, and what changed —
so after editing the green zone, MAX_WALK_MINUTES, fit.py, etc. you can see
exactly which past listings flip, with no browser and (by default) no LLM cost.
"""
from __future__ import annotations
import json
import os
import sys
from collections import Counter

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pipeline
import storage
from models import ListingExtract

_USE_LLM = "--llm" in sys.argv
_CHANGED_ONLY = "--changed" in sys.argv


def _reclassify(post):
    """Current-code verdict for one archived post, or None if unusable."""
    imgs = json.loads(post["images"]) if post["images"] else []
    if _USE_LLM:
        if not post["raw_text"]:
            return None
        return pipeline.process_post(post["raw_text"], source_url=post["source_url"],
                                     group=post["group"], images=imgs,
                                     comments=post["comments"], commit=False)
    if not post["parsed_json"]:
        return None
    e = ListingExtract.model_validate_json(post["parsed_json"])
    return pipeline._classify(e, post["raw_text"] or "", post["source_url"],
                              post["group"], imgs, None, commit=False)


def main() -> None:
    posts = storage.all_posts()
    now = Counter()
    changes = []
    skipped = 0
    for p in posts:
        res = _reclassify(p)
        if res is None:
            skipped += 1
            continue
        nv, ns = res.status.value, res.score
        now[nv] += 1
        if nv != p["verdict"] or ns != p["score"]:
            changes.append((p, nv, ns, res))

    mode = "LLM re-parse" if _USE_LLM else "stored parse"
    print(f"replayed {len(posts) - skipped} posts ({mode}); skipped {skipped}")
    print(f"now: {dict(now)}")
    print(f"changed: {len(changes)}")
    for p, nv, ns, res in changes[:50]:
        addr = ((res.extract.street_address_or_neighborhood or "") if res.extract else "")[:22]
        print(f"  {str(p['verdict']):10}/{str(p['score']):>4}  ->  {nv:10}/{str(ns):>4}   "
              f"{(res.location_tier or ''):6} {addr}")
    if not _CHANGED_ONLY and not changes:
        print("(nothing changed — current code agrees with the stored verdicts)")


if __name__ == "__main__":
    main()
