"""
Replay the classifier over every archived post — re-test filter / zone / threshold
/ scoring changes against your whole history, WITHOUT re-scraping Facebook.

    python replay.py            # reuse the stored LLM parse, re-run classify+score
                                #   (fast, no LLM, no quota) — for zone/threshold/score edits
    python replay.py --llm      # re-run the LLM extraction too — for prompt.py/llm.py edits
                                #   (uses Gemini quota)
    python replay.py --changed  # only list posts whose verdict/score changed
    python replay.py --llm --only-merged --min-score 1
                                # re-read ONLY the posts whose archived text runs on
                                #   into a second Facebook story (their stored parse
                                #   mixed two posts). --min-score 1 narrows 404 such
                                #   posts to the 58 that actually produce a listing —
                                #   no merged DROP/NOT_AD carries a score, measured
                                #   2026-08-05 — so it costs 58 Gemini calls, not 404.
    python replay.py --frozen   # place from the cache + local tiers only, never a live
                                #   geocoder — the ONLY reproducible mode. Warm the cache
                                #   first (`warm_cache.py --archive`), or use
                                #   `full_replay.py`, which does both in order.
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
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import dates
import geocode
import pipeline
import scraper          # for cut_at_next_story only — no browser is started
import sheets
import storage
from models import ListingExtract, Status

_USE_LLM = "--llm" in sys.argv
_CHANGED_ONLY = "--changed" in sys.argv
_APPLY = "--apply" in sys.argv
# --frozen : place from the CACHE AND LOCAL TIERS ONLY, never a live geocoder.
#
# A REPLAY THAT CALLS THE NETWORK PRODUCES A SAMPLE, NOT AN ANSWER. Measured 2026-08-13:
# two passes minutes apart over the same 10,565 posts disagreed on **1,144 rows** — 736 of
# them `street_geom -> overpass` — purely because the mirrors answered differently the
# second time. Only 116 rows were the code change under test. `--apply` WRITES those
# verdicts, so an un-frozen apply bakes one roll of the dice into the DB and a re-run
# rewrites a different set. It is the same root cause as the test-suite flake fixed in
# `b861ddc`.
#
# Lossless AFTER A WARM, which is why `full_replay.py` warms and then passes this:
# `warm_cache --archive` has already asked the network about every archived address, so
# the cache holds everything the network could place. Measured the same day, local-only
# places **2,425 of 2,683** archive addresses and the other 258 fail WITH the network too
# — same coverage, no dice. Without a warm first it is NOT lossless, so it is opt-in.
_FROZEN = "--frozen" in sys.argv
if _FROZEN:
    import config as _config
    _config.USE_OVERPASS_FALLBACK = False
    _config.USE_NOMINATIM_FALLBACK = False
    _config.USE_GOOGLE_GEOCODE = False
# --min-score N : only replay archived posts whose STORED score is >= N (focus the
# refresh on the alert-worthy top listings; keeps an LLM re-parse cheap).
_MIN_SCORE = (int(sys.argv[sys.argv.index("--min-score") + 1])
              if "--min-score" in sys.argv else None)
_PRUNE_ORPHANS = "--prune-orphans" in sys.argv   # drop rows whose key no longer maps to a parse
# --only-imprecise : replay ONLY posts whose stored location is IMPRECISE — a bare
# neighborhood OR a bare street with no house number. Pair with --llm to cheaply
# re-extract just those under the improved prompt (recover a missing house number /
# neighborhood), spending Gemini quota only where it can help. (--only-bare-nbhd kept
# as an alias for the narrower bare-neighborhood-only subset.)
_ONLY_IMPRECISE = "--only-imprecise" in sys.argv
_ONLY_BARE = "--only-bare-nbhd" in sys.argv
# --only-merged : replay ONLY posts whose archived raw_text runs on into a SECOND
# Facebook story. Those were parsed from the merged blob, so the listing can carry one
# post's flat under another post's permalink. Pair with --llm to re-read just those.
_ONLY_MERGED = "--only-merged" in sys.argv


def _is_merged_post(post) -> bool:
    """True if the archived text still contains a next-story author+age header.

    Measured 2026-08-05: 404 of 6,606 archived posts, 58 of them producing a live
    listing. `scraper._clean_story` cuts these at scrape time now, so this only ever
    matches history."""
    raw = post.get("raw_text") or ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return len(scraper.cut_at_next_story(lines)) < len(lines)


def _is_imprecise_post(post, bare_nbhd_only: bool = False) -> bool:
    """True if the post's stored location is a bare neighborhood, or (unless
    bare_nbhd_only) a bare street with no house number — the imprecise placements a
    re-extract might sharpen."""
    pj = post.get("parsed_json")
    if not pj:
        return False
    try:
        loc = ListingExtract.model_validate_json(pj).street_address_or_neighborhood
    except Exception:
        return False
    return geocode.is_bare_neighborhood(loc) or (not bare_nbhd_only and geocode.is_bare_street(loc))


def _age_hours(post):
    """How old the post is NOW, from its archived publish time — or None when the
    archive never captured one.

    Passing None here (which is what replay did) silently changed the score: a live
    run supplies a real age so the freshness factor contributes, replay supplied
    nothing so it contributed 0, and every `replay --apply` therefore rewrote scores
    2-4 points downward. Same input, different number, depending on which code path
    last touched the row. Now both paths measure freshness the same way.

    UTC, NOT LOCAL. `posted_at` is stored on SQLite's UTC clock, so subtracting a local
    `datetime.now()` added 3 hours to every replayed age — see `dates.utc_now`, which
    exists because this is the second module to get it wrong."""
    stamp = post.get("posted_at")
    if not stamp:
        return None
    try:
        posted = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return max(0.0, (dates.utc_now() - posted).total_seconds() / 3600.0)


def _reclassify(post):
    """Current-code verdict for one archived post, or None if unusable."""
    imgs = json.loads(post["images"]) if post["images"] else []
    if _USE_LLM:
        if not post["raw_text"]:
            return None
        # ONE STORY PER POST, applied HERE rather than to the archive. A block can run
        # on into a second story carrying its own price, address and phone, and the
        # stored parse came from that merged text. `posts.raw_text` is deliberately left
        # alone: it is the record of what was actually scraped, and the trailing content
        # is sometimes the only copy of a flat never captured on its own. Truncating at
        # read time gives the same verdict and destroys nothing.
        lines = [ln.strip() for ln in post["raw_text"].splitlines() if ln.strip()]
        text = "\n".join(scraper.cut_at_next_story(lines))
        return pipeline.process_post(text, source_url=post["source_url"],
                                     group=post["group"], images=imgs,
                                     comments=post["comments"], commit=False)
    if not post["parsed_json"]:
        return None
    e = ListingExtract.model_validate_json(post["parsed_json"])
    e = pipeline._postprocess_extract(e, post["raw_text"] or "", post["comments"] or "")
    return pipeline._classify(e, post["raw_text"] or "", post["source_url"],
                              post["group"], imgs, _age_hours(post), commit=False)


def main() -> None:
    posts = storage.all_posts()
    now = Counter()
    changes = []
    skipped = rescued = demoted = 0
    for p in posts:
        if _MIN_SCORE is not None and (p["score"] or 0) < _MIN_SCORE:
            skipped += 1
            continue
        if (_ONLY_IMPRECISE or _ONLY_BARE) and not _is_imprecise_post(p, bare_nbhd_only=_ONLY_BARE):
            skipped += 1
            continue
        if _ONLY_MERGED and not _is_merged_post(p):
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
        if _APPLY:
            if res.status in (Status.MATCH, Status.NEEDS_DATA) and res.dedup_key:
                storage.save_listing(res)              # upsert (update or add)
            elif res.extract:
                # now RED/NOT_AD/blacklisted → drop any stored row for this listing.
                # Delete by ALL the extract's keys: an EARLY drop (blacklist / non-ב-ג-ד
                # neighborhood) returns no dedup_key, so a stale MATCH row would survive
                # a plain delete(res.dedup_key) — that was the "שכונה ו" leak.
                for k in storage.dedup_keys(res.extract):
                    storage.delete_listing(k)
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
        # Retire pin-queue names the geocoder now answers. This is the moment to do it:
        # an apply has just re-placed every listing, so whatever is still unplaceable is
        # genuinely unplaceable. Left alone the queue read 199 items of which 66 were real,
        # and a list that is two-thirds dead work does not get worked.
        retired = storage.retire_unknown_locations(
            geocode.resolved_unknown_names(storage.unknown_locations(days=3650)))
        n = sheets.rebuild_from_db()
        sheets.sort_by_score()
        print(f"APPLIED → DB updated ({rescued} rescued to MATCH, {demoted} dropped, "
              f"{pruned} orphans pruned, {merged} duplicates merged, "
              f"{retired} resolved names retired from the pin queue); sheet rebuilt "
              f"({n} rows). Run top_listings.py to broadcast the new top.")
    elif not changes:
        print("(nothing changed — current code agrees with the stored verdicts)")


if __name__ == "__main__":
    main()
