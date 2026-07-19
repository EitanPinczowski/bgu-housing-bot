"""
The funnel. Takes one raw post's text and runs the full logic, independent of
where the text came from (manual paste now, auto-scraper later).

Order matters (cheap/decisive checks first):
  is_apartment_ad -> blacklist -> dedup -> hard field gates -> geocode+route
"""
from __future__ import annotations
import hashlib
import re
from typing import Optional

import config
import geocode
import llm
import notifier
import osrm
import sheets
import storage
import zones
from models import PipelineResult, Status


def _text_sig(text: str) -> str:
    """Stable signature of a post's text, for deduping the SAME post re-read on a
    later run (comment-less posts have no permalink to dedup on). Uses the first
    ~150 chars — enough to identify the post, before any See-more expansion adds
    to the end, so it matches whether or not the post was expanded."""
    norm = re.sub(r"\s+", " ", (text or "")).strip()[:150]
    return "text:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _blacklisted(location: Optional[str]) -> bool:
    if not location:
        return False
    return any(bad in location for bad in config.BLACKLIST_NEIGHBORHOODS)


def _missing_critical(e) -> bool:
    # NEEDS_DATA only when a field we truly need is absent: rooms (for the >=2
    # gate) or street/neighborhood (to geocode). We deliberately do NOT trust the
    # LLM's own missing_critical_data flag — it was over-eager and pushed
    # complete-enough posts into NEEDS_DATA. Price stays optional; a known price
    # is still enforced against MAX_PRICE_PER_ROOM_ILS in process_post.
    return (e.available_rooms_count is None
            or e.street_address_or_neighborhood is None)


def process_post(raw_text: str,
                 source_url: Optional[str] = None,
                 group: Optional[str] = None,
                 images: Optional[list] = None,
                 comments: Optional[str] = None,
                 commit: bool = True) -> PipelineResult:
    """Run one post through the funnel.

    commit=True  (default, manual mode / --live scraper): persist state —
        honour the is_seen early-return, mark_seen, save_listing, and notify.
    commit=False (dry-run scraper): pure classify-and-return. No dedup skip,
        no DB writes, no Telegram — so a dry run never mutates anything and a
        post you've already stored is still shown, not silently swallowed.
    """
    images = images or []

    # 0) URL-level dedup BEFORE the LLM. A 2×/day scraper re-sees the same posts
    #    near the top of a group; skipping them here saves an API call each,
    #    which matters on the free tier's tight daily quota. Only in commit mode
    #    (a dry run must classify everything). Cross-posts with a different URL
    #    still get caught by the phone/content dedup below.
    if commit and source_url and storage.is_url_seen(source_url):
        return PipelineResult(status=Status.DROP, reason="already seen (url)",
                              source_url=source_url, group=group)

    # 0b) Cheap keyword pre-filter BEFORE the LLM: a post with no housing word at
    #     all isn't a rental ad (lost pet, furniture sale, chit-chat) — drop it
    #     without spending an LLM call. Saves Gemini quota and the slow local
    #     fallback. Not marked url-seen: re-checking is free (just a keyword scan).
    if config.PREFILTER_KEYWORDS and not any(k in raw_text for k in config.PREFILTER_KEYWORDS):
        return PipelineResult(status=Status.NOT_AD, reason="no housing keywords (pre-filter)",
                              source_url=source_url, group=group, images=images)

    # 0c) Text-signature dedup BEFORE the LLM. A comment-less post has no permalink
    #     to dedup on, so it's re-read every run; without this, inconsistent
    #     extraction (phone found one run, not the next) makes a second row with a
    #     different dedup_key. Keying on the post text collapses those.
    sig = _text_sig(raw_text)
    if commit and storage.is_seen(sig):
        return PipelineResult(status=Status.DROP, reason="already seen (text)",
                              source_url=source_url, group=group, images=images)

    e = llm.extract(raw_text, comments=comments)
    if commit:
        storage.mark_seen(sig)
        if source_url:
            storage.mark_url_seen(source_url)

    def result(status: Status, reason: str = "", walk=None, walk_gate=None,
               lat=None, lon=None, key=None, tier=None, preferred=None):
        return PipelineResult(status=status, reason=reason, walk_minutes=walk,
                              walk_gate=walk_gate, location_tier=tier, preferred=preferred,
                              lat=lat, lon=lon, dedup_key=key,
                              source_url=source_url, group=group, images=images,
                              extract=e)

    # 1) not an apartment ad at all
    if not e.is_apartment_ad:
        return result(Status.NOT_AD, "not an apartment rental ad")

    # 2) blacklisted neighborhood -> drop before touching the router
    if _blacklisted(e.street_address_or_neighborhood):
        return result(Status.DROP, f"blacklisted area: {e.street_address_or_neighborhood}")

    # 3) dedup (prefer phone; survives cross-posting). Skipped on a dry run so
    #    already-stored posts still surface instead of short-circuiting to DROP.
    key = storage.make_dedup_key(e)
    if commit and storage.is_seen(key):
        return result(Status.DROP, "already seen", key=key)

    def mark_seen(k: str) -> None:
        if commit:
            storage.mark_seen(k)

    # 4) hard field gates that don't need routing
    if e.price_per_room_ils is not None and e.price_per_room_ils > config.MAX_PRICE_PER_ROOM_ILS:
        mark_seen(key)
        return result(Status.DROP, f"price {e.price_per_room_ils} > {config.MAX_PRICE_PER_ROOM_ILS}", key=key)
    if e.available_rooms_count is not None and e.available_rooms_count < config.MIN_AVAILABLE_ROOMS:
        mark_seen(key)
        return result(Status.DROP, f"only {e.available_rooms_count} rooms free", key=key)
    if e.total_roommates_in_apt is not None and e.total_roommates_in_apt > config.MAX_TOTAL_ROOMMATES:
        mark_seen(key)
        return result(Status.DROP, f"{e.total_roommates_in_apt} total roommates > {config.MAX_TOTAL_ROOMMATES}", key=key)

    # 5) locate it: geocode -> tier (GREEN/AMBER/RED/UNKNOWN). OSRM minutes are
    #    informational only now; your green zone + 500m buffer make the call.
    coords = geocode.geocode(e.street_address_or_neighborhood)
    lat, lon = (coords if coords else (None, None))
    walk, walk_gate = osrm.walk_to_nearest(lat, lon)
    tier = zones.classify_location(lat, lon)
    mark_seen(key)

    if tier == "RED":
        return result(Status.DROP, f"beyond {config.BUFFER_METERS:.0f}m of green zone",
                      walk=walk, walk_gate=walk_gate, lat=lat, lon=lon, key=key, tier=tier, preferred=False)

    # 6) classify. GREEN/AMBER + complete -> MATCH (amber = acceptable, not
    #    preferred). Missing fields or ungeocodable -> NEEDS_DATA, kept not lost.
    missing = _missing_critical(e)
    preferred = (tier == "GREEN")

    if missing or tier == "UNKNOWN":
        reasons = []
        if missing:
            reasons.append("missing rooms/street")
        if tier == "UNKNOWN":
            reasons.append("location not geocoded")
        elif tier == "AMBER":
            reasons.append("within 500m of green zone (acceptable, not preferred)")
        res = result(Status.NEEDS_DATA, "; ".join(reasons),
                     walk=walk, walk_gate=walk_gate, lat=lat, lon=lon, key=key, tier=tier, preferred=preferred)
    else:
        label = ("in green zone (preferred)" if tier == "GREEN"
                 else "within 500m of green zone (acceptable, not preferred)")
        res = result(Status.MATCH, label,
                     walk=walk, walk_gate=walk_gate, lat=lat, lon=lon, key=key, tier=tier, preferred=preferred)

    if commit:
        storage.save_listing(res)
        sheets.save_listing(res)   # optional Google Sheets sink (no-op if unset)
        notifier.notify(res)
    return res
