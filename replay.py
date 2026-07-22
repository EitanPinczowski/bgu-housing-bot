"""
Replay the classifier over every archived post — re-test filter / zone / threshold
/ scoring changes against your whole history, WITHOUT re-scraping Facebook.

    python replay.py            # reuse the stored LLM parse, re-run classify+score
                                #   (fast, no LLM, no quota) — for zone/threshold/score edits
    python replay.py --llm      # re-run the LLM extraction too — for prompt.py/llm.py edits
                                #   (uses Gemini quota)
    python replay.py --changed  # only list posts whose verdict/score changed
    python replay.py --apply    # WRITE the results: update DB scores/tiers, add
                                #   newly-qualifying listings, drop now-RED ones,
                                #   and rebuild the Sheet. No Telegram (bulk change).

Without --apply it's read-only: it reports what the CURRENT code+config would
decide for each stored post, and what changed — so after editing the green zone,
MAX_WALK_MINUTES, fit.py, etc. you can preview which past listings flip with no
browser and (by default) no LLM cost. Then --apply commits that.
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

import geocode
import pipeline
import sheets
import storage
from models import ListingExtract, Status

_USE_LLM = "--llm" in sys.argv
_CHANGED_ONLY = "--changed" in sys.argv
_APPLY = "--apply" in sys.argv
# --min-score N : only replay archived posts whose STORED score is >= N (focus the
# refresh on the alert-worthy top listings; keeps an LLM re-parse cheap).
_MIN_SCORE = (int(sys.argv[sys.argv.index("--min-score") + 1])
              if "--min-score" in sys.argv else None)
_PRUNE_ORPHANS = "--prune-orphans" in sys.argv   # drop rows whose key no longer maps to a parse
# --only-bare-nbhd : replay ONLY posts whose stored location is a bare neighborhood
# (no street). Pair with --llm to cheaply re-extract just those under the improved
# "prefer the street over the neighborhood" prompt, spending Gemini quota only where
# it can help (a street buried under a neighborhood name).
_ONLY_BARE = "--only-bare-nbhd" in sys.argv


def _is_bare_nbhd_post(post) -> bool:
    """True if the post's stored parse resolved to a bare neighborhood (a whole area,
    no specific street) — the population --only-bare-nbhd re-extracts."""
    pj = post.get("parsed_json")
    if not pj:
        return False
    try:
        loc = ListingExtract.model_validate_json(pj).street_address_or_neighborhood
    except Exception:
        return False
    return geocode.is_bare_neighborhood(loc)


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
    e = pipeline._postprocess_extract(e, post["raw_text"] or "", post["comments"] or "")
    return pipeline._classify(e, post["raw_text"] or "", post["source_url"],
                              post["group"], imgs, None, commit=False)


def main() -> None:
    posts = storage.all_posts()
    now = Counter()
    changes = []
    skipped = rescued = demoted = 0
    for p in posts:
        if _MIN_SCORE is not None and (p["score"] or 0) < _MIN_SCORE:
            skipped += 1
            continue
        if _ONLY_BARE and not _is_bare_nbhd_post(p):
            skipped += 1
            continue
        res = _reclassify(p)
        if res is None:
            skipped += 1
            continue
        nv, ns = res.status.value, res.score
        now[nv] += 1
        if nv != p["verdict"] or ns != p["score"]:
            changes.append((p, nv, ns, res))
        if _APPLY and res.dedup_key:
            if res.status in (Status.MATCH, Status.NEEDS_DATA):
                storage.save_listing(res)              # upsert (update or add)
            else:
                storage.delete_listing(res.dedup_key)  # now RED/NOT_AD -> drop
            storage.record_post(p["sig"], p["raw_text"] or "", p["comments"] or "",
                                res.images or [], p["group"], p["source_url"],
                                res.extract, res)       # refresh the archive verdict
            if p["verdict"] != "MATCH" and nv == "MATCH":
                rescued += 1
            elif p["verdict"] in ("MATCH", "NEEDS_DATA") and nv in ("DROP", "NOT_AD"):
                demoted += 1

    mode = "LLM re-parse" if _USE_LLM else "stored parse"
    print(f"replayed {len(posts) - skipped} posts ({mode}); skipped {skipped}")
    print(f"now: {dict(now)}")
    print(f"changed: {len(changes)}")
    for p, nv, ns, res in changes[:50]:
        addr = ((res.extract.street_address_or_neighborhood or "") if res.extract else "")[:22]
        print(f"  {str(p['verdict']):10}/{str(p['score']):>4}  ->  {nv:10}/{str(ns):>4}   "
              f"{(res.location_tier or ''):6} {addr}")
    if _APPLY:
        # Drop rows whose key no longer maps to any archived parse (orphans left when a
        # post was re-parsed to a different key — e.g. an earlier Ollama-fallback run).
        pruned = storage.prune_orphan_listings() if _PRUNE_ORPHANS else 0
        # Re-deriving from the archive can re-introduce phone/hash duplicates that were
        # merged earlier — collapse them again before mirroring to the sheet.
        merged = storage.merge_duplicate_listings()
        n = sheets.rebuild_from_db()
        sheets.sort_by_score()
        print(f"APPLIED → DB updated ({rescued} rescued to MATCH, {demoted} dropped, "
              f"{pruned} orphans pruned, {merged} duplicates merged); sheet rebuilt "
              f"({n} rows). Run top_listings.py to broadcast the new top.")
    elif not changes:
        print("(nothing changed — current code agrees with the stored verdicts)")


if __name__ == "__main__":
    main()
