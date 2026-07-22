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
import dates
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


_IMMEDIATE_RE = re.compile(r"מיידית|מידית|מייד|מיד\b|עכשיו|היום")
_FLEX_RE = re.compile(r"גמיש")
_DATE_RE = dates.DATE_RE                               # shared 1.9 / 01/10 / 1-9 -> DD.MM


def _normalize_entry_date(s: Optional[str]) -> Optional[str]:
    """Uniform lease-start: `DD.MM` for dates (a month name alone → the 1st of that
    month, `01.MM`; a day before the month name is kept, `15 בספטמבר` → `15.09`),
    `גמיש` for flexible, `מיידי` for immediate. Multiple values joined with ', '.
    Falls back to the trimmed original if nothing parses."""
    if not s:
        return s
    out = []
    for m in _DATE_RE.finditer(s):                 # 1.9 / 01/10 / 15.8.26 -> DD.MM
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            out.append(f"{d:02d}.{mo:02d}")
    for name, num in dates.HE_MONTHS.items():      # month name -> day (or 1st) . month
        i = s.find(name)
        if i == -1:
            continue
        dm = re.search(r"(\d{1,2})\s*ב?ל?\s*$", s[max(0, i - 6):i])
        day = int(dm.group(1)) if (dm and 1 <= int(dm.group(1)) <= 31) else 1
        out.append(f"{day:02d}.{num:02d}")
    if _FLEX_RE.search(s):
        out.append("גמיש")
    if _IMMEDIATE_RE.search(s):
        out.append("מיידי")
    seen: list = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return ", ".join(seen) if seen else (s.strip() or None)


_PHONE_CHUNK_RE = re.compile(r"(?:\+?972|0)[\d\s().\-]{8,}")


def _normalize_phone(contact: Optional[str]) -> Optional[str]:
    """Format Israeli mobile number(s) as `05X-XXXXXXX` — from any 0/+972 form (incl.
    the number inside a wa.me/972… link), tolerating spaces/dots/dashes. Multiple
    distinct numbers joined with ', '. A non-mobile string (landline, plain link with
    no 05X) is returned unchanged."""
    if not contact:
        return contact
    nums: list = []
    for chunk in _PHONE_CHUNK_RE.findall(contact):
        d = re.sub(r"\D", "", chunk)
        if d.startswith("972"):
            d = "0" + d[3:]
        if len(d) == 10 and d.startswith("05"):
            f = f"{d[:3]}-{d[3:]}"
            if f not in nums:
                nums.append(f)
    return ", ".join(nums) if nums else contact


_PRICE_MARKERS = ("₪", 'ש"ח', "ש״ח", "שח", 'שכ"ד', "שכ״ד", "שכד", "לחודש", "מחיר")
_PRICE_NUM_RE = re.compile(r"\b\d{3,4}\b")   # standalone 3–4 digits, not part of a phone


def _price_second_chance(text: Optional[str]) -> Optional[int]:
    """When the LLM returned no price, recover a per-room-plausible price (500–3000)
    sitting within ~20 chars of a price marker. Conservative range so it never
    grabs a phone number or a whole-apartment total."""
    if not text:
        return None
    t = re.sub(r"(\d)[.,](\d{3})", r"\1\2", text)   # join thousands separators
    for marker in _PRICE_MARKERS:
        i = t.find(marker)
        while i != -1:
            window = t[max(0, i - 20): i + len(marker) + 20]
            for num in _PRICE_NUM_RE.findall(window):
                v = int(num)
                if 500 <= v <= 3000:
                    return v
            i = t.find(marker, i + 1)
    return None


def _clean_address(s: Optional[str]) -> Optional[str]:
    """Trim junk and drop Latin transliteration noise — but keep a real address:
    only strips Latin when there's no house number (numbered addresses are real),
    and only if Hebrew survives (an all-Latin address is left for Nominatim)."""
    if not s:
        return s
    s = re.sub(r"\s{2,}", " ", s.replace("__", " ")).strip()
    if not re.search(r"\d", s):
        stripped = re.sub(r"\s{2,}", " ", re.sub(r"[A-Za-z]+", "", s)).strip(" -,/'\"")
        if re.search(r"[א-ת]", stripped):
            return stripped
    return s.strip(" -,/'\"") or None


def _postprocess_extract(e, raw_text, comments):
    """Deterministic cleanups applied to every LLM extract — shared by process_post
    and replay so both reflect the same rules. Idempotent."""
    if e.price_per_room_ils is None:                     # recover a price the LLM missed
        p = _price_second_chance((raw_text or "") + " " + (comments or ""))
        if p is not None:
            e.price_per_room_ils = p
            e.price_from_comment = True                  # recovered -> treat as uncertain
    e.street_address_or_neighborhood = _clean_address(e.street_address_or_neighborhood)
    e.street_address_or_neighborhood = _recover_house_number(
        e.street_address_or_neighborhood, raw_text)
    e.lease_start_date = _normalize_entry_date(e.lease_start_date)
    e.contact_phone_or_link = _normalize_phone(e.contact_phone_or_link)
    return e


def _recover_house_number(address, raw_text):
    """If the LLM returned a numberless street but the post text has '<that street>
    <number>', append the number so it geocodes precisely (safety net, like
    _price_second_chance). Bare neighborhoods and already-numbered addresses untouched."""
    if not address or not raw_text or any(ch.isdigit() for ch in address):
        return address
    if geocode.is_bare_neighborhood(address):
        return address
    core = geocode._overpass_name(address)               # the street token, no street-words
    if len(core) < 3:
        return address
    m = re.search(re.escape(core) + r"\s+(\d{1,4})\b", raw_text)
    return f"{address} {m.group(1)}" if m else address


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


# "שכונה ב" / "בשכונה ג'" / "שכונת ד" -> the letter. The trailing negative lookahead
# keeps it to a standalone letter (so "שכונה ברושים" doesn't read as ב).
_NBHD_STRIP = str.maketrans("", "", "״׳'`\"")
_NBHD_RE = re.compile(r"שכונ[הת]?\s+([א-י])(?![א-ת])")


def _neighborhood_letter(location: Optional[str]) -> Optional[str]:
    """The שכונה letter named in the address text ('ב'/'ג'/…), else None."""
    if not location:
        return None
    m = _NBHD_RE.search(location.translate(_NBHD_STRIP))
    return m.group(1) if m else None


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
                 commit: bool = True,
                 alert: bool = True) -> PipelineResult:
    """Run one post through the funnel.

    commit=True  (default, manual mode / --live scraper): persist state —
        honour the is_seen early-return, mark_seen, save_listing, and notify.
    commit=False (dry-run scraper): pure classify-and-return. No dedup skip,
        no DB writes, no Telegram — so a dry run never mutates anything and a
        post you've already stored is still shown, not silently swallowed.
    alert=False (batch mode): save/persist as usual but DON'T send the per-post
        Telegram alert — the caller (main.py) collects results and sends one
        ranked, capped batch at the end of the run instead.
    """
    images = images or []
    raw_text = _strip_bidi(raw_text)      # kill FB's invisible RTL control chars
    comments = _strip_bidi(comments)

    # OCR path: a post that is really a PHOTO of the ad — tiny caption + an image.
    # Its text lives in the picture, so the keyword pre-filter and text-signature
    # dedup below (which key on the text) are meaningless and would wrongly drop or
    # collapse it; skip them and let the LLM read the image instead. The URL dedup
    # and the post-LLM key dedup still apply, so re-reads never re-alert.
    ocr = (getattr(config, "SCRAPER_OCR_IMAGE_ONLY", False) and bool(images)
           and len((raw_text or "").strip()) < config.OCR_MIN_TEXT_CHARS)

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
    if (config.PREFILTER_KEYWORDS and not ocr
            and not any(k in raw_text for k in config.PREFILTER_KEYWORDS)):
        return PipelineResult(status=Status.NOT_AD, reason="no housing keywords (pre-filter)",
                              source_url=source_url, group=group, images=images)

    # 0c) Text-signature dedup BEFORE the LLM. A comment-less post has no permalink
    #     to dedup on, so it's re-read every run; without this, inconsistent
    #     extraction (phone found one run, not the next) makes a second row with a
    #     different dedup_key. Keying on the post text collapses those. (Skipped for
    #     OCR posts — their text is too thin to sign reliably.)
    sig = _text_sig(raw_text)
    if commit and not ocr and storage.is_seen(sig):
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

    e = _postprocess_extract(
        llm.extract(raw_text, comments=comments, images=(images if ocr else None)),
        raw_text, comments)

    if commit:
        if not ocr:                    # thin OCR text isn't a reliable seen-key
            storage.mark_seen(sig)
        if source_url:
            storage.mark_url_seen(source_url)

    res = _classify(e, raw_text, source_url, group, images, age_hours, commit, alert)
    if commit:
        storage.record_post(sig, raw_text, comments, images, group, source_url, e, res)
    return res


def _classify(e, raw_text: str, source_url, group, images: list,
              age_hours, commit: bool, alert: bool = True) -> PipelineResult:
    """Steps 1-6: grade an already-extracted listing into a PipelineResult,
    and (when commit) persist + notify. Split out so replay.py can re-run it
    on a STORED extract -- re-testing zone/threshold/scoring changes with no
    browser and no LLM call. commit=False = pure, side-effect-free classify."""
    def result(status: Status, reason: str = "", walk=None, walk_gate=None,
               lat=None, lon=None, key=None, tier=None, preferred=None, geo_source=None):
        return PipelineResult(status=status, reason=reason, walk_minutes=walk,
                              walk_gate=walk_gate, location_tier=tier, preferred=preferred,
                              lat=lat, lon=lon, dedup_key=key, geo_source=geo_source,
                              source_url=source_url, group=group, images=images,
                              extract=e)

    # 1) not an apartment ad at all
    if not e.is_apartment_ad:
        return result(Status.NOT_AD, "not an apartment rental ad")

    # 2) blacklisted neighborhood -> drop before touching the router
    if _blacklisted(e.street_address_or_neighborhood):
        return result(Status.DROP, f"blacklisted area: {e.street_address_or_neighborhood}")

    # 2b) allowed-neighborhood gate: if the post NAMES a שכונה that isn't ב/ג/ד,
    #     drop it — only those neighborhoods are relevant. Text-based (what the post
    #     says); a plain street or a named area (הבלוק, הרובע…) names no letter and
    #     passes. Coordinates are never used to drop (a boundary miss must not lose a
    #     good listing) — only ב/ג/ד polygons exist, and they feed the score only.
    nbhd_letter = _neighborhood_letter(e.street_address_or_neighborhood)
    if nbhd_letter and nbhd_letter not in config.ALLOWED_NEIGHBORHOODS:
        return result(Status.DROP, f"שכונה {nbhd_letter} מחוץ לאזורים הרלוונטיים (ב/ג/ד)")

    # 3) dedup. Check ALL of the listing's stable keys (phone, content-hash, and —
    #    for a numbered address — the address key), so the SAME flat is caught even
    #    when the LLM extracted the phone (or the price) on only one read. This is
    #    what stops רינגלבלום 1 / רגר 164 being alerted twice. Skipped on a dry run
    #    so already-stored posts still surface instead of short-circuiting to DROP.
    key = storage.make_dedup_key(e)
    if commit and storage.is_seen_any(storage.dedup_keys(e)):
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
    coords, geo_source = geocode.geocode_detailed(e.street_address_or_neighborhood)
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
    # A bare neighborhood ("שכונה ג") is a whole area we can't pin — cap it at
    # AMBER (shown as a near-miss, not preferred). An accurate street address keeps
    # its real tier and can still be GREEN.
    elif tier == "GREEN" and geocode.is_bare_neighborhood(e.street_address_or_neighborhood):
        tier = "AMBER"
    # Address precision: an IMPRECISELY-placed listing (a street-name / bare point, not a
    # precise house/POI hit) can't be a confident GREEN. On a boundary-crossing street
    # (green on one end, red on the other) a name-only point could be the wrong side →
    # RED; a numberless street elsewhere → cap GREEN to AMBER. A precise hit keeps its tier.
    addr = e.street_address_or_neighborhood
    boundary = False
    if tier in ("GREEN", "AMBER") and not geocode.is_precise_source(geo_source):
        if geocode.is_boundary_street(addr):
            tier, boundary = "RED", True
        elif tier == "GREEN" and geocode.is_bare_street(addr):
            tier = "AMBER"
    # ב/ג/ד-ONLY: only the three imported neighborhoods are acceptable. Any in-range
    # point outside them is RED — even inside the hand-drawn green zone (the polygons
    # win). Fail-open if neighborhoods.json is missing (in_allowed_neighborhood).
    outside_bgd = tier in ("GREEN", "AMBER") and not zones.in_allowed_neighborhood(lat, lon)
    if outside_bgd:
        tier = "RED"
    mark_seen(key)

    if tier == "RED":
        reason = ("רחוב שחוצה את גבול האזור — כתובת מדויקת לא נקבעה" if boundary
                  else "מחוץ לשכונות ב/ג/ד" if outside_bgd
                  else "שכונה ד' מחוץ לפוליגון הירוק (ללא מרווח)" if no_amber
                  else f"more than {config.MAX_WALK_MINUTES} min walk from a gate")
        return result(Status.DROP, reason, geo_source=geo_source,
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
        res = result(Status.NEEDS_DATA, "; ".join(reasons), geo_source=geo_source,
                     walk=walk, walk_gate=walk_gate, lat=lat, lon=lon, key=key, tier=tier, preferred=preferred)
    else:
        label = ("in green zone (preferred)" if tier == "GREEN"
                 else "within a 20-min walk of a gate (acceptable, not preferred)")
        res = result(Status.MATCH, label, geo_source=geo_source,
                     walk=walk, walk_gate=walk_gate, lat=lat, lon=lon, key=key, tier=tier, preferred=preferred)

    # Preferred-neighborhood tie-breaker (ב > ג = ד): the letter the post NAMES wins
    # (the user's rule is about what the post says); else infer from the coordinate.
    neighborhood = nbhd_letter or zones.neighborhood_of(lat, lon)
    res.score = fit.score(e.price_per_room_ils, walk, tier,
                          e.available_rooms_count, e.total_roommates_in_apt,
                          e.price_from_comment, age_hours, e.lease_start_date,
                          e.furnished, e.floor, e.has_elevator, e.balcony_or_garden,
                          neighborhood)

    if commit:
        # Mark this flat seen under ALL its stable keys (phone/content-hash/address)
        # so a later re-read whose phone or price the LLM extracts differently is
        # recognised as a duplicate and never re-alerted.
        storage.mark_seen_all(storage.dedup_keys(e))
        storage.save_listing(res)
        storage.record_fingerprint(res.dedup_key, _tokens(raw_text))   # for fuzzy dedup of reposts
        sheets.save_listing(res)   # optional Google Sheets sink (no-op if unset)
        if alert:
            notifier.notify(res)   # batch mode (alert=False) defers this to main.py
    return res
