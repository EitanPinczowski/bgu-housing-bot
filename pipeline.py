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
import fit
import geocode
import llm
import notifier
import osrm
import sheets
import storage
import zones
from models import PipelineResult, Status


# Invisible bidirectional-control characters Facebook injects into RTL (Hebrew)
# text. They make the SAME post hash to a different _text_sig / fuzzy fingerprint
# and can split numbers, so we strip them before anything else touches the text.
_BIDI_RE = re.compile("[‎‏‪-‮⁦-⁩؜]")


def _strip_bidi(text):
    return _BIDI_RE.sub("", text) if text else text


def _text_sig(text: str) -> str:
    """Stable signature of a post's text, for deduping the SAME post re-read on a
    later run (comment-less posts have no permalink to dedup on). Uses the first
    ~150 chars — enough to identify the post, before any See-more expansion adds
    to the end, so it matches whether or not the post was expanded."""
    norm = re.sub(r"\s+", " ", (text or "")).strip()[:150]
    return "text:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


_TOKEN_RE = re.compile(r"[א-ת]{3,}")


def _tokens(text: str) -> set:
    """Set of Hebrew word tokens (≥3 letters) — the fingerprint used for fuzzy
    cross-post dedup (catches the same flat reposted with slightly different text
    / phone shown in only one copy)."""
    return set(_TOKEN_RE.findall(text or ""))


def _blacklisted(location: Optional[str]) -> bool:
    if not location:
        return False
    return any(bad in location for bad in config.BLACKLIST_NEIGHBORHOODS)


def _no_amber_area(location: Optional[str]) -> bool:
    """True if the address is in a neighborhood that gets NO 500m amber grace
    (e.g. שכונה ד'): outside the green polygon there is red, not amber."""
    if not location:
        return False
    norm = location.replace("״", "").replace("׳", "").replace("'", "").replace("`", "")
    return any(m in norm for m in config.NO_AMBER_NEIGHBORHOODS)


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
                 age_hours: Optional[float] = None,
                 commit: bool = True) -> PipelineResult:
    """Run one post through the funnel.

    commit=True  (default, manual mode / --live scraper): persist state —
        honour the is_seen early-return, mark_seen, save_listing, and notify.
    commit=False (dry-run scraper): pure classify-and-return. No dedup skip,
        no DB writes, no Telegram — so a dry run never mutates anything and a
        post you've already stored is still shown, not silently swallowed.
    """
    images = images or []
    raw_text = _strip_bidi(raw_text)      # kill FB's invisible RTL control chars
    comments = _strip_bidi(comments)

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

    # 0d) Fuzzy cross-post dedup BEFORE the LLM: a near-identical repost of a flat
    #     we already stored (same text, but a different permalink and the phone
    #     shown in only one copy -> different exact keys) is caught here by text
    #     similarity, saving both the duplicate row and the LLM call.
    toks = _tokens(raw_text)
    if commit:
        dup = storage.find_similar(toks)
        if dup:
            return PipelineResult(status=Status.DROP, reason=f"cross-post duplicate of {dup}",
                                  source_url=source_url, group=group, images=images)

    e = llm.extract(raw_text, comments=comments)
    if commit:
        storage.mark_seen(sig)
        if source_url:
            storage.mark_url_seen(source_url)

    res = _classify(e, raw_text, source_url, group, images, age_hours, commit)
    if commit:
        storage.record_post(sig, raw_text, comments, images, group, source_url, e, res)
    return res


def _classify(e, raw_text: str, source_url, group, images: list,
              age_hours, commit: bool) -> PipelineResult:
    """Steps 1-6: grade an already-extracted listing into a PipelineResult,
    and (when commit) persist + notify. Split out so replay.py can re-run it
    on a STORED extract -- re-testing zone/threshold/scoring changes with no
    browser and no LLM call. commit=False = pure, side-effect-free classify."""
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
    # A name we HAVE but couldn't map -> log it so the daily DM digest can suggest
    # pinning it to the static table (this is exactly how "הבלוק" was missed).
    if commit and coords is None and e.street_address_or_neighborhood:
        storage.record_unknown_location(e.street_address_or_neighborhood)
    lat, lon = (coords if coords else (None, None))
    walk, walk_gate = osrm.walk_to_nearest(lat, lon)
    # AMBER = within MAX_WALK_MINUTES of a gate; pass the real OSRM walk time so
    # the boundary is the actual walk, not a straight-line estimate.
    tier = zones.classify_location(lat, lon, walk_min=walk)
    # No-amber neighborhoods (e.g. שכונה ד'): the buffer doesn't rescue them —
    # outside the green polygon there = red. Caught geographically (point inside
    # the ד' polygon) OR by address text (the post says שכונה ד').
    no_amber = tier == "AMBER" and (zones.in_no_amber_zone(lat, lon)
                                    or _no_amber_area(e.street_address_or_neighborhood))
    if no_amber:
        tier = "RED"
    mark_seen(key)

    if tier == "RED":
        reason = ("שכונה ד' מחוץ לפוליגון הירוק (ללא מרווח)" if no_amber
                  else f"more than {config.MAX_WALK_MINUTES} min walk from a gate")
        return result(Status.DROP, reason,
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
            reasons.append("within a 20-min walk of a gate (acceptable, not preferred)")
        res = result(Status.NEEDS_DATA, "; ".join(reasons),
                     walk=walk, walk_gate=walk_gate, lat=lat, lon=lon, key=key, tier=tier, preferred=preferred)
    else:
        label = ("in green zone (preferred)" if tier == "GREEN"
                 else "within a 20-min walk of a gate (acceptable, not preferred)")
        res = result(Status.MATCH, label,
                     walk=walk, walk_gate=walk_gate, lat=lat, lon=lon, key=key, tier=tier, preferred=preferred)

    res.score = fit.score(e.price_per_room_ils, walk, tier,
                          e.available_rooms_count, e.total_roommates_in_apt,
                          e.price_from_comment, age_hours, e.lease_start_date)

    if commit:
        storage.save_listing(res)
        storage.record_fingerprint(res.dedup_key, _tokens(raw_text))   # for fuzzy dedup of reposts
        sheets.save_listing(res)   # optional Google Sheets sink (no-op if unset)
        notifier.notify(res)
    return res
