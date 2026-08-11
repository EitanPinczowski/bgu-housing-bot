"""
Buy the house-number anchors ONCE, from govmap, and never call the network again.

    python seed_anchors.py --dry-run    # which streets, how many requests. Spends nothing.
    python seed_anchors.py              # do it (resumable; safe to re-run)
    python seed_anchors.py --street "בני אור"

WHY
---
`geocode.interpolate_house` needs **two** house-number anchors on a street before it can
place anything there. 199 of the 237 streets this bot cares about have fewer than two, so
every flat on them collapses onto a single street centroid — which is what "most of the
points are clusters, even when zoomed in" actually is.

OSM will not fix that (only 3.7% of its buildings here carry a number) and no free
OSM-derived geocoder can either, because they all serve the same data. govmap does, for
free and without a key.

The anchors are written to disk, so this runs ONCE. Afterwards every listing on those
streets — including numbers no post has mentioned yet — is placed by local arithmetic
with no network at all. That is the whole point: buy the anchors, not the lookups.

HARVESTING, NOT PROBING
-----------------------
Asking for a number that does not exist makes govmap return a spread of real neighbours
instead: `בני אור 200` answers with 11, 12, 17, 18, 22, 24, 26, 29, 32 — nine anchors from
one request. So we deliberately ask high, read back whatever real numbers it offers, and
only fall back to more queries if that was not enough. Typical cost is 1–2 requests per
street rather than one per house number.
"""
from __future__ import annotations
import json
import math
import os
import re
import sys
import time

import config
import geocode
import govmap
import streets
import zones

OUT_PATH = config.ROOT / "govmap_anchors.json"

# Enough anchors to interpolate across the street rather than between two neighbours.
TARGET_ANCHORS = 6
# Never spend more than this in one run, however wrong the street list is.
MAX_REQUESTS = 800
# Same rule that guards OSM's own anchors: a point that is not near the street it claims
# is a different street with the same name, and five of those were once this project's
# entire multi-kilometre error tail.
MAX_OFFSET_M = geocode.MAX_ANCHOR_OFFSET_M

_ADDR_RE = re.compile(r"^(.*?)\s+(\d+[א-ת]?)\s+באר[- ]שבע\s*$")


def _street_length_m(name: str) -> float:
    return sum(geocode._haversine_m(a[0], a[1], b[0], b[1])
               for seg in streets.geometry(name) for a, b in zip(seg, seg[1:]))


def stranded_streets() -> list:
    """[(street, length_m, anchor_count)] for streets where a REAL listing's house number
    cannot be placed — whatever the anchor count.

    Anchor count is the wrong criterion on its own. `אלכסנדר ינאי` has two anchors, 8 and
    14, so the thin-street test skipped it — but every listing on it is numbered 17 to 32,
    far past that range and beyond the extrapolation cap. `ביאליק חיים נחמן` is anchored
    1–4 with listings at 11–139. Two anchors in the wrong place buy nothing."""
    anchors = geocode._load_anchors()
    out = []
    for st, nums in wanted_numbers().items():
        if not any(not geocode.place_house(st, hn)[0] for hn in nums):
            continue
        have = len([k for k in (anchors.get(st) or {}) if str(k).isdigit()])
        out.append((st, _street_length_m(st), have))
    return out


def in_zone(name: str) -> bool:
    """Does any part of this street lie in the GREEN or AMBER zone?

    "Matters" = a flat there could actually be a match. Seeding the whole city would be
    wasted requests.

    Extracted from `relevant_streets` so `missing_gaps` can share it. Those two want
    DIFFERENT street sets from the same test and conflating them cost a wrong answer:
    `relevant_streets` is in-zone AND THIN (<2 anchors), because a street with no anchors
    cannot interpolate at all. Densification wants in-zone REGARDLESS of thinness — the
    widest gaps are on well-anchored streets, `דרך מצדה` holding 19 anchors across numbers
    4..258. Reusing `relevant_streets` for "in-zone" found 7 streets where there are 177.
    """
    la0, lo0, la1, lo1 = geocode._bs_bounds()
    pts = [p for s in streets.geometry(name) for p in s]
    for p in pts[::5]:
        if not (la0 <= p[0] <= la1 and lo0 <= p[1] <= lo1):
            continue
        tier = zones.classify_location(p[0], p[1])
        if tier and str(tier[0]).upper().startswith(("G", "A")):
            return True
    return False


def relevant_streets() -> list:
    """[(street, length_m, existing_anchor_count)] for streets that matter and are thin."""
    anchors = geocode._load_anchors()
    out = []
    for name in sorted({s for s in streets._index().values()}):
        if not streets.geometry(name) or not in_zone(name):
            continue
        have = len([k for k in (anchors.get(name) or {}) if str(k).isdigit()])
        if have >= 2:
            continue
        out.append((name, _street_length_m(name), have))
    return out


def wanted_numbers() -> dict:
    """{street: {house numbers that real listings use}} — the numbers it would be
    embarrassing to still be unable to place after paying for a seed run.

    A harvest query returns whatever govmap ranks highest, which clusters at the low end:
    the בני אור harvest gave 1–28 while the actual listings are at 50 and 64. Asking for
    the numbers we hold listings for costs one request each and guarantees they land."""
    import storage
    out: dict = {}
    try:
        with storage._conn() as c:
            rows = [(r[0] or "") for r in
                    c.execute("SELECT address FROM listings").fetchall()]
    except Exception:
        return out
    for addr in rows:
        hn = geocode._house_number(addr)
        if not hn:
            continue
        for cand in geocode._candidate_tokens(addr)[:2]:
            real, _how = streets.canonical(cand)
            if real:
                out.setdefault(real, set()).add(hn)
                break
    return out


def _queries(length_m: float, wanted=()) -> list:
    """House numbers to ask for, best first.

    The first is deliberately PAST the end of the street so govmap answers with a spread
    of real addresses instead of one exact match — nine anchors for one request on
    בני אור. Then the numbers real listings actually use. The rest only run if that was
    still thin."""
    est_max = max(20, min(400, int(length_m / 11.2 * 2)))
    tail = [1, est_max // 2, est_max]
    return [est_max + 50] + sorted(wanted, key=lambda n: int(re.sub(r"\D", "", n) or 0)) \
        + tail


def _near_accepted(pt, found: dict) -> bool:
    """Is `pt` within MAX_OFFSET_M of an address we have ALREADY accepted here?

    OSM sometimes holds only a stub of a real road — `הבשור` is 48 m of geometry,
    `סוסו הכהן` 223 m for a street with 7 listings on it — so measuring every candidate
    against that stub rejects the whole street beyond its first block. Letting the
    street's known extent grow along a chain of confirmed addresses uses the addresses
    themselves as evidence of where the road goes, which is what they are. The chain must
    still START from something within MAX_OFFSET_M of the real geometry, and `_one_road`
    re-checks the finished set for the two-clump signature of two roads sharing a name."""
    for other in found.values():
        if geocode._haversine_m(pt[0], pt[1], other[0], other[1]) <= MAX_OFFSET_M:
            return True
    return False


def _accept(street: str, text: str, pt, found: dict | None = None) -> tuple:
    """(number, (lat, lon)) if this result is really an address on `street`, else None.

    govmap renames as it answers (`ביאליק חיים נחמן` -> `ביאליק`, `סמטת קדש` -> `קדש`),
    so the street is compared after canonicalisation, not as a string."""
    m = _ADDR_RE.match(text.strip())
    if not m:
        return None
    head, number = m.group(1).strip(), m.group(2)
    real, _how = streets.canonical(head)
    if real != street:
        return None
    off = geocode._off_street_m(street, pt[0], pt[1])
    if off is not None and off > MAX_OFFSET_M and not _near_accepted(pt, found or {}):
        return None
    # An address govmap places inside the campus or the hospital is a lecture hall, not a
    # flat — and as an anchor it drags its interpolated neighbours in after it.
    if zones.no_housing_here(pt[0], pt[1]):
        return None
    if seed_conflict(street, number, pt):
        return None
    return number, pt


# BEING NEAR THE STREET IS NOT ENOUGH, AND THE COST OF LEARNING THAT TWICE WAS A WHOLE
# SEEDING RUN. `_accept` above checks geometry — is this point near the road? — so govmap
# answering `שדרות יצחק רגר 163` with a point 409 m along רגר passes: it IS on רגר. It is
# simply not where 163 is. Measured 2026-08-11: 6 of the 18 addresses that got WORSE after
# 1,050 seeded anchors sat next to a seed like that, and each did double damage — the bad
# anchor made `geocode._contradicts_anchors` reject the CORRECT interpolation, then became
# the fallback answer itself (`גוש עציון 34`: 0 m -> 278 m, answered by `anchor_neighbour`
# at exactly the bad anchor).
#
# That query-time guard can only ever reject answers. This is the same arithmetic moved to
# the only place it can refuse the CAUSE.
#
# Graded against the OSM survey, never against other seeds: a bad seed must not become the
# evidence that admits the next one.
SEED_CONSISTENCY_FLOOR_M = 100.0

_osm_anchors_cache: dict | None = None


def _osm_anchors() -> dict:
    """The surveyed anchors alone — `_load_anchors()` merges govmap in, which would let a
    seed vouch for itself."""
    global _osm_anchors_cache
    if _osm_anchors_cache is None:
        try:
            _osm_anchors_cache = json.loads(
                geocode._ANCHORS_PATH.read_text(encoding="utf-8"))
        except Exception:
            _osm_anchors_cache = {}
    return _osm_anchors_cache


def seed_conflict(street: str, number: str, pt,
                  floor: float = SEED_CONSISTENCY_FLOOR_M) -> str | None:
    """Why this seed disagrees with the street's surveyed numbering, or None.

    Same shape as `geocode._contradicts_anchors` — nearest surveyed number, distance
    allowed to grow with how far away that number is — with one deliberate difference:
    a much lower floor. The query-time floor of 250 m protects answers on streets we know
    little about, where rejecting would leave nothing. Here we only judge a seed when the
    street HAS survey anchors, and dropping one costs nothing but a request. On a city
    whose houses run ~11 m apart, 250 m is 22 houses of slack.

    Returns None when the street has no surveyed numbers, so a street OSM has never
    numbered is seeded exactly as before.
    """
    if not str(number).isdigit():
        return None
    want = int(number)
    numbered = [(int(n), p) for n, p in _osm_anchors().get(street, {}).items()
                if str(n).isdigit() and int(n) != want]
    if not numbered:
        return None
    near_n, near_c = min(numbered, key=lambda a: abs(a[0] - want))
    d = geocode._haversine_m(near_c[0], near_c[1], pt[0], pt[1])
    allowed = max(floor, geocode.ANCHOR_CONSISTENCY_SLACK
                  * geocode._median_metres_per_number() * max(1, abs(near_n - want)))
    if d <= allowed:
        return None
    return (f"{d:.0f} m from surveyed anchor {near_n} "
            f"(allowed {allowed:.0f} m for {abs(near_n - want)} house number(s))")


# THE SURVEY CANNOT JUDGE WHAT IT HAS NEVER SEEN, AND THAT IS WHERE THE SEEDS ARE.
# `seed_conflict` grades a seed against `house_anchors.json`, which sounded complete until
# it was counted: 1,768 of 2,385 seeds — 74% — sit on 144 streets with NO surveyed number
# at all, so nothing judged them. `הכנסת` has 0 survey anchors and 14 seeds; `השלום` has 1
# and 61. That is exactly where the damage stayed after the first filter ran: interpolating
# `הכנסת 18` takes the tightest bracket it can (17 and 19, both seeds, both ~110 m from
# truth) and answers 133 m out, while seed 16 sits 11 m from truth and is never consulted.
#
# One authority exists on every street, surveyed or not: its own shape. `_street_axis`
# already says so — "House numbers increase monotonically along a street, so that
# coordinate is a natural, robust parametrization". An anchor whose position along that
# axis disagrees with its number is the outlier, and no survey is needed to see it.
MIN_ANCHORS_TO_JUDGE_ORDER = 4
ORDER_CONSENSUS = 0.6


def _longest_monotonic(values: list) -> set:
    """Indices of the longest run that increases (or decreases) with the list order.

    O(n^2) on purpose: a street holds tens of anchors, not thousands, and the quadratic
    version is one obvious loop instead of a patience-sort with reconstruction.
    """
    best: set = set()
    for sign in (1, -1):
        n = len(values)
        prev = [-1] * n
        length = [1] * n
        for i in range(n):
            for j in range(i):
                if sign * (values[i] - values[j]) >= 0 and length[j] + 1 > length[i]:
                    length[i], prev[i] = length[j] + 1, j
        if not length:
            continue
        # TIES DECIDE WHICH ANCHOR IS THE STRAY, so they are not arbitrary. For
        # [790, 791, 792, 850, 794] two runs reach length 4 — one ending on the 850 jump,
        # one on 794 — and taking the FIRST kept the jump and dropped the anchor that
        # continues the street. Prefer the chain reaching furthest along the numbering.
        end = max(range(n), key=lambda i: (length[i], i))
        chain, i = set(), end
        while i != -1:
            chain.add(i)
            i = prev[i]
        if len(chain) > len(best):
            best = chain
    return best


def order_outliers(street: str, anchors: dict) -> dict:
    """{number: why} for anchors that sit out of order along the street.

    Judged per parity — odd and even run up opposite sides, and mixing them adds noise the
    consensus then has to fight. Silent unless there are enough anchors to establish an
    order (`MIN_ANCHORS_TO_JUDGE_ORDER`) and the surviving run is a real majority
    (`ORDER_CONSENSUS`): with three anchors, "two agree and one does not" is not evidence
    of anything, and a street whose numbering genuinely jumps must not be gutted.
    """
    pts, idx = geocode._street_axis(street)
    if len(pts) < 2:
        return {}                                   # no geometry -> nothing to judge with
    out: dict = {}
    numbered = sorted((int(n), n, p) for n, p in anchors.items() if str(n).isdigit())
    for parity in (0, 1):
        group = [a for a in numbered if a[0] % 2 == parity]
        if len(group) < MIN_ANCHORS_TO_JUDGE_ORDER:
            continue
        keep = _longest_monotonic([a[2][idx] for a in group])
        if len(keep) < ORDER_CONSENSUS * len(group):
            continue                                # no consensus -> do not trust the test
        for i, (num, raw, _pt) in enumerate(group):
            if i not in keep:
                out[raw] = (f"out of order along the street: {len(keep)} of {len(group)} "
                            f"{'even' if parity == 0 else 'odd'} anchors run monotonically "
                            f"and {num} does not")
    return out


def audit_seeds(apply: bool = False, floor: float = SEED_CONSISTENCY_FLOOR_M) -> int:
    """Re-judge every anchor already in govmap_anchors.json against the survey.

    The seeds were written before `seed_conflict` existed, so the file holds anchors that
    would not be accepted today. Re-checking them costs no requests at all — the points
    are already on disk.
    """
    try:
        seeds = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        print(f"no {OUT_PATH.name} to audit")
        return 1

    kept, dropped = {}, []
    for street, nums in seeds.items():
        for num, pt in nums.items():
            why = seed_conflict(street, num, pt, floor)
            if why:
                dropped.append((street, num, why))
            else:
                kept.setdefault(street, {})[num] = pt
    by_survey = len(dropped)

    # Second pass, on what survived. The ORDER is established from the survey and the
    # surviving seeds TOGETHER — a surveyed number is the strongest evidence of where the
    # numbering runs — but only a seed may be dropped. OSM is the base authority; if a
    # survey point is the one out of line, that is a survey to fix, not to silently discard.
    for street in list(kept):
        together = dict(_osm_anchors().get(street, {}))
        together.update(kept[street])
        for num, why in order_outliers(street, together).items():
            if num in kept[street]:
                dropped.append((street, num, why))
                del kept[street][num]
        if not kept[street]:
            del kept[street]

    total = sum(len(v) for v in seeds.values())
    print(f"{total} seeded anchors -> {total - len(dropped)} kept")
    print(f"  {by_survey} contradict the survey (floor {floor:.0f} m)")
    print(f"  {len(dropped) - by_survey} sit out of order along their own street")
    print(f"{len(seeds)} streets -> {len(kept)}\n")
    for street, num, why in dropped[:20]:
        print(f"  drop {street} {num:>4}  {why}")
    if len(dropped) > 20:
        print(f"  ... and {len(dropped) - 20} more")

    if not apply:
        print("\n--audit without --apply: nothing written")
        return 0
    save_anchors(kept)
    print(f"\nwrote {OUT_PATH}")
    return 0


# Two roads sharing a name sit kilometres apart (the ההגנה case: anchors 10 m from the
# geometry AND anchors 2,887 m away). Anchors on ONE road never split like that.
MAX_CLUSTER_GAP_M = 300.0


def _one_road(street: str, found: dict) -> bool:
    """Do these anchors belong to a single road?

    The failure this guards against is govmap answering with points from two different
    roads that share a name, which is what once put `ההגנה 89` 3.5 km out. Those show up
    as two clumps of points far apart, so that is what we look for.

    It deliberately does NOT test whether the house numbers order neatly along the
    street's dominant axis. That version discarded 29 streets of good data, because OSM
    often holds only a FRAGMENT of a road: `רוטנברג` has 147 m of geometry but real house
    numbers running past 65, so projecting them onto that fragment's axis scrambles the
    order. All nine of its anchors sat within 49 m of the street — good data, thrown
    away. The per-anchor 200 m offset test in `_accept` is the real guard; this only
    catches the two-road case it cannot see."""
    pts = [tuple(p) for p in found.values()]
    if len(pts) < 3:
        return True                       # too few to judge; the offset test stands alone
    # single-link clustering along the widest axis: sort, then look for a big gap
    for axis in (0, 1):
        scale = 111320.0 * (math.cos(math.radians(pts[0][0])) if axis else 1.0)
        vals = sorted(p[axis] for p in pts)
        gaps = [(b - a) * scale for a, b in zip(vals, vals[1:])]
        if gaps and max(gaps) > MAX_CLUSTER_GAP_M:
            return False
    return True


def seed_street(street: str, length_m: float, wanted=()) -> dict:
    """{number: [lat, lon]} harvested for one street."""
    found: dict = {}
    wanted = set(wanted)
    for n in _queries(length_m, wanted):
        # keep going past TARGET_ANCHORS while numbers we hold listings for are missing
        if govmap.calls >= MAX_REQUESTS:
            break
        if len(found) >= TARGET_ANCHORS and not (wanted - set(found)):
            break
        for kind, text, pt in govmap.search(f"{street} {n} באר שבע"):
            if kind != "address":
                continue
            got = _accept(street, text, pt, found)
            if got:
                found[got[0]] = [round(got[1][0], 6), round(got[1][1], 6)]
    if not _one_road(street, found):
        print(f"  ! {street}: anchors split into clumps >{MAX_CLUSTER_GAP_M:.0f} m apart "
              f"— two roads share this name, discarding all {len(found)}")
        return {}
    return found


def missing_exact() -> list:
    """[(street, number)] for every address a real listing uses that we do NOT hold a
    surveyed anchor for.

    Interpolating between anchors is good to p50 13 m; asking govmap for the address
    itself measured 5.4 m against surveyed ground truth. For the ~140 addresses this bot
    actually has listings at, there is no reason to compute what we can look up — and a
    real anchor also densifies the street for every future listing on it."""
    anchors = geocode._load_anchors()
    out = []
    for st, nums in sorted(wanted_numbers().items()):
        for hn in sorted(nums, key=lambda n: int(re.sub(r"\D", "", n) or 0)):
            if hn not in (anchors.get(st) or {}):
                out.append((st, hn))
    return out


def missing_gaps(limit: int = 0) -> list:
    """[(street, number)] for same-parity house numbers missing INSIDE a street's anchored
    range, worst-covered streets first.

    WHY SAME PARITY, AND WHY "INSIDE". The hold-out that grades this removes an address's
    own anchor, so what places it is the span between its NEIGHBOURS — n-2 and n+2, which
    run up the same side of the street. Measured 2026-08-10, error tracks that span almost
    exactly:

        span <50 m   n=81   p50  6 m   p90  43 m      <- already meets the targets
        span 50-150  n=67   p50 14 m   p90  75 m
        span >=150   n=26   p50 35 m   p90 122 m

    So anchors every 50 m are NOT enough: hold one out and the bracket becomes 100 m. Only
    near-complete coverage of the odd (or even) numbers puts an address in the top bucket.

    STREETS ARE CHOSEN BY THEIR OWN COVERAGE, NEVER BY WHICH HOLD-OUT ADDRESSES THEY
    CONTAIN. Seeding the streets the sample happens to test would be fitting the ruler, and
    the resulting gain would not generalise to the addresses real listings use.

    Pooled spellings are one road (`דרך מצדה`/`מצדה`, `ביאליק חיים נחמן`/`חיים נחמן ביאליק`),
    so `streets.aliases` collapses them — otherwise the same house number is asked for twice
    and the request count reads nearly double what it is.
    """
    anchors = geocode._load_anchors()
    seen_pool: set = set()
    per_street = []
    for st in sorted(anchors):
        canon = sorted(streets.aliases(st))[0] if streets.aliases(st) else st
        if canon in seen_pool:
            continue
        if not in_zone(st):
            continue
        ns = sorted(int(k) for k in (anchors.get(st) or {}) if str(k).isdigit())
        if len(ns) < 2:
            continue
        seen_pool.add(canon)
        have = set(ns)
        miss = [n for n in range(ns[0], ns[-1] + 1)
                if n % 2 == ns[0] % 2 and n not in have]
        if miss:
            per_street.append((len(miss), st, miss))
    per_street.sort(reverse=True)                 # worst-covered first
    if not limit:
        return [(st, str(hn)) for _n, st, miss in per_street for hn in miss]
    # A CAPPED RUN NEEDS BOTH DEPTH AND REACH, and the two pull against each other.
    # Measured over the pinned 250 for a 400-request budget:
    #
    #     streets   per street   sample addresses touched
    #        20         20              31
    #        40         10              65        <- chosen
    #       161          2             173
    #
    # Worst-covered-first alone put all 400 on 6 streets (`דרך מצדה` wants 119 by itself),
    # touching almost nothing. Pure round-robin reached 161 streets but added 2 anchors
    # each, which cannot tighten a street holding 19 anchors across 128 numbers. Round
    # robin over the worst-covered ~limit/DEPTH streets gets enough of both to be a test.
    depth = 10
    chosen = per_street[:max(1, limit // depth)]
    out = []
    queues = [(st, list(miss)) for _n, st, miss in chosen]
    while queues and len(out) < limit:
        for st, miss in queues:
            if not miss:
                continue
            out.append((st, str(miss.pop(0))))
            if len(out) >= limit:
                break
        queues = [(st, miss) for st, miss in queues if miss]
    return out


OUTWARD_STOP_AFTER_MISSES = 3
OUTWARD_MAX_UP = 10


def save_anchors(existing: dict) -> None:
    """Write `govmap_anchors.json` atomically, retrying while it is locked.

    The seeders save after EVERY accepted anchor, so an abort loses nothing — which also
    means hundreds of writes to a file inside a OneDrive-synced folder. On 2026-08-11 a
    run died with `PermissionError: [Errno 13]` partway through because the sync client
    held the file, losing the rest of an 800-request budget.

    Two changes, and the atomicity matters more than the retry: writing straight to the
    real path means a lock (or a crash) mid-`write_text` can leave a TRUNCATED anchor file
    where a valid one used to be. Writing a temp file and `os.replace`-ing it means the
    anchors are either the old set or the new one, never half of either.
    """
    blob = json.dumps(existing, ensure_ascii=False, sort_keys=True, indent=1)
    tmp = OUT_PATH.with_name(OUT_PATH.name + ".tmp")
    last = None
    for attempt in range(6):
        try:
            tmp.write_text(blob, encoding="utf-8")
            os.replace(tmp, OUT_PATH)
            return
        except PermissionError as e:                # the sync client has it open
            last = e
            time.sleep(0.4 * (attempt + 1))
    print(f"  [warn] could not write {OUT_PATH.name} ({last}); anchors are in "
          f"{tmp.name} — the run continues rather than throwing them away")


def outward_runs(limit: int = 0) -> list:
    """[(street, [numbers])] to probe BELOW a street's lowest anchor and ABOVE its highest.

    `missing_gaps` only fills numbers INSIDE the anchored range, and `interpolate_house`
    refuses to place anything outside it — so an address past either end cannot be placed
    at all, whatever the density in between. Measured: 52 of the 76 hold-out addresses
    with no bracket are range-bound this way, 35 below the minimum and 17 above the
    maximum. `פולה בן-גוריון 18` where the anchors start at 35; `אברהם שופט 148` where
    they end at 146.

    Emitted as RUNS rather than a flat pair list because the caller stops a run after
    `OUTWARD_STOP_AFTER_MISSES` consecutive no-answers: a street that really ends at 40
    should cost three requests past it, not ten. Same parity as the end it walks from, and
    downward runs stop at 1.
    """
    anchors = geocode._load_anchors()
    seen_pool: set = set()
    runs = []
    for st in sorted(anchors):
        canon = sorted(streets.aliases(st))[0] if streets.aliases(st) else st
        if canon in seen_pool or not in_zone(st):
            continue
        ns = sorted(int(k) for k in (anchors.get(st) or {}) if str(k).isdigit())
        if not ns:
            continue
        seen_pool.add(canon)
        low, high = ns[0], ns[-1]
        down = [n for n in range(low - 2, 0, -2)][:OUTWARD_MAX_UP]
        up = list(range(high + 2, high + 2 + 2 * OUTWARD_MAX_UP, 2))
        if down:
            runs.append((st, down))
        if up:
            runs.append((st, up))
    # Longest reach first, so a capped run spends itself on the streets with most to gain.
    runs.sort(key=lambda r: -len(r[1]))
    if limit:
        runs = runs[:limit]
    return runs


def seed_outward(runs: list, existing: dict) -> int:
    """Walk each run outward, abandoning it after a few consecutive no-answers."""
    added = 0
    for st, numbers in runs:
        misses = 0
        for hn in numbers:
            if govmap.calls >= MAX_REQUESTS:
                print(f"\nrequest cap {MAX_REQUESTS} reached — re-run to continue")
                return added
            if govmap.throttled:
                print(f"\nSTOPPING: govmap is throttling us ({govmap.last_error}). "
                      f"Anchors so far are already saved.")
                return added
            if govmap.consecutive_errors >= 3:
                print(f"\nSTOPPING: 3 requests in a row failed ({govmap.last_error}). "
                      f"Anchors so far are already saved.")
                return added
            got = None
            for kind, text, pt in govmap.search(f"{st} {hn} באר שבע"):
                if kind != "address":
                    continue
                # `text`, NOT the number we asked for. `_accept` parses the street and
                # number out of govmap's OWN answer, because govmap renames as it answers
                # and the point of the check is that what came back is the address we
                # meant. Passing `str(hn)` makes `_ADDR_RE` fail to match every time, so
                # every result is rejected and the run reads as "this street has no
                # numbers past its end" — 710 requests returned 0 anchors that way.
                got = _accept(st, text, pt, existing.get(st) or {})
                if got:
                    break
            if not got:
                misses += 1
                if misses >= OUTWARD_STOP_AFTER_MISSES:
                    break                          # the street really does end here
                continue
            misses = 0
            existing.setdefault(st, {})[got[0]] = [round(got[1][0], 6), round(got[1][1], 6)]
            added += 1
            save_anchors(existing)
        print(f"  {st:26} {numbers[0]}..{numbers[-1]}  +{added} total "
              f"({govmap.calls} requests)")
    return added


def seed_exact(pairs: list, existing: dict) -> int:
    """Ask govmap for each address directly. Returns how many were accepted."""
    added = 0
    for i, (st, hn) in enumerate(pairs, 1):
        if govmap.calls >= MAX_REQUESTS:
            print(f"\nrequest cap {MAX_REQUESTS} reached — re-run to continue")
            break
        # STOP, DO NOT PUSH THROUGH. govmap is undocumented and the live pipeline is
        # forbidden to touch it; a 429/403 is the server saying back off, and retrying is
        # how a one-off seeding run becomes a blocked one. An empty result is data and
        # does not count — `govmap.search` resets the counter whenever the server answers,
        # so only a real run of failures trips this.
        if govmap.throttled:
            print(f"\nSTOPPING at {i - 1}/{len(pairs)}: govmap is throttling us "
                  f"({govmap.last_error}). Anchors so far are already saved.")
            break
        if govmap.consecutive_errors >= 3:
            print(f"\nSTOPPING at {i - 1}/{len(pairs)}: 3 requests in a row failed "
                  f"({govmap.last_error}). Anchors so far are already saved.")
            break
        got = None
        for kind, text, pt in govmap.search(f"{st} {hn} באר שבע"):
            if kind != "address":
                continue
            # anchors already banked for this street extend its known reach
            got = _accept(st, text, pt, existing.get(st) or {})
            if got:
                break
        if got:
            existing.setdefault(st, {})[got[0]] = [round(got[1][0], 6), round(got[1][1], 6)]
            added += 1
            save_anchors(existing)
        print(f"  [{i}/{len(pairs)}] {st} {hn:<5} {'exact' if got else '—'}")
    return added


def seed_unresolvable(dry: bool = False) -> int:
    """Numbered addresses whose STREET our index cannot resolve at all.

    `מגדלי קרן 5`, `רחוב יואל השופט 2`, `סמטת צקלג 5`, `יוטבתה 6` — CLAUDE.md records
    צקלג and יוטבתה as absent from OSM entirely, so no amount of anchoring helps. govmap
    resolves a whole address string without needing our street index, which makes it the
    only route to these. Stored as user pins, not anchors: with no street to hang them on
    they cannot interpolate anything, they just place their own listing.

    The city check still applies, and so does the house number — those are what stop
    govmap's habit of substituting a different place from doing damage here."""
    import storage
    with storage._conn() as c:
        rows = [(r[0] or "") for r in c.execute("SELECT DISTINCT address FROM listings")]
    todo = []
    for a in rows:
        hn = geocode._house_number(a)
        if not hn or geocode.geocode_cached(a):
            continue
        if any(streets.canonical(t)[0] for t in geocode._candidate_tokens(a)[:2]):
            continue
        todo.append((a.strip(), hn))
    print(f"{len(todo)} numbered addresses whose street cannot be resolved locally")
    if dry:
        for a, _hn in todo[:25]:
            print(f"   {a[:56]}")
        print("\n--dry-run: nothing sent")
        return 0
    added = 0
    for i, (a, hn) in enumerate(todo, 1):
        pt = None
        for kind, text, p in govmap.search(f"{a} באר שבע"):
            if kind != "address" or not any(c in text for c in ("באר שבע", "באר-שבע")):
                continue
            if not govmap._has_number(text, hn):
                continue                       # the `999 -> 13` substitution
            pt = p
            break
        if pt:
            geocode.add_pin(a, pt[0], pt[1])
            added += 1
        print(f"  [{i}/{len(todo)}] {a[:44]:46} {'pinned' if pt else '—'}")
    print(f"\n{added}/{len(todo)} pinned into user_pins.json")
    return 0


def main() -> int:
    dry = "--dry-run" in sys.argv
    if "--unresolved" in sys.argv:
        return seed_unresolvable(dry)
    only = None
    if "--street" in sys.argv:
        only = sys.argv[sys.argv.index("--street") + 1]

    if "--gaps" in sys.argv:
        limit = 0
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        pairs = missing_gaps(limit)
        print(f"{len(pairs)} same-parity house numbers missing inside anchored ranges"
              + (f" (capped at {limit})" if limit else ""))
        print(f"asking govmap for each one directly: {len(pairs)} requests")
        if dry:
            print("\n--dry-run: nothing sent.")
            for st, hn in pairs[:25]:
                print(f"   {st} {hn}")
            return 0
        try:
            existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        n = seed_exact(pairs, existing)
        print(f"\n{n}/{len(pairs)} became exact anchors -> {OUT_PATH}")
        return 0

    if "--max" in sys.argv:
        global MAX_REQUESTS
        MAX_REQUESTS = int(sys.argv[sys.argv.index("--max") + 1])

    if "--outward" in sys.argv:
        runs = outward_runs()
        print(f"{len(runs)} outward runs over {len({r[0] for r in runs})} streets, "
              f"up to {sum(len(r[1]) for r in runs)} requests "
              f"(fewer in practice — a run stops after "
              f"{OUTWARD_STOP_AFTER_MISSES} consecutive misses); cap {MAX_REQUESTS}")
        if dry:
            print("\n--dry-run: nothing sent. Longest reach first:")
            for st, nums in runs[:15]:
                print(f"   {st:26} {nums[0]}..{nums[-1]}")
            return 0
        try:
            existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        n = seed_outward(runs, existing)
        print(f"\n{n} anchors added past the ends of streets -> {OUT_PATH}")
        print(f"{govmap.calls} requests this run")
        return 0

    if "--audit" in sys.argv:
        floor = SEED_CONSISTENCY_FLOOR_M
        if "--floor" in sys.argv:
            floor = float(sys.argv[sys.argv.index("--floor") + 1])
        return audit_seeds(apply="--apply" in sys.argv, floor=floor)

    if "--exact" in sys.argv:
        pairs = missing_exact()
        print(f"{len(pairs)} addresses real listings use have no surveyed anchor")
        print(f"asking govmap for each one directly: {len(pairs)} requests")
        if dry:
            print("\n--dry-run: nothing sent.")
            for st, hn in pairs[:25]:
                print(f"   {st} {hn}")
            return 0
        try:
            existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        n = seed_exact(pairs, existing)
        print(f"\n{n}/{len(pairs)} became exact anchors -> {OUT_PATH}")
        return 0

    # Streets a real listing is waiting on come FIRST and are never skipped as
    # "already seeded" — the point is the numbers we still cannot place.
    stranded = stranded_streets()
    stranded_names = {t[0] for t in stranded}
    todo_all = stranded + [t for t in relevant_streets() if t[0] not in stranded_names]
    if only:
        todo_all = [t for t in todo_all if t[0] == only] or [(only, _street_length_m(only), 0)]

    try:
        existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        existing = {}
    todo = [t for t in todo_all if t[0] in stranded_names or t[0] not in existing]

    want = wanted_numbers()
    extra = sum(len(want.get(n, ())) for n, _L, _h in todo)
    print(f"{len(todo)} streets to seed ({len(existing)} already done)")
    print(f"estimated requests: {len(todo) + extra}–{len(todo) * 2 + extra} "
          f"(1 harvesting query each, plus {extra} for numbers real listings use)")
    if dry:
        print("\n--dry-run: nothing sent. Longest first:")
        for name, L, have in sorted(todo, key=lambda t: -t[1])[:15]:
            w = sorted(want.get(name, ()), key=lambda n: int(re.sub(r"\D", "", n) or 0))
            print(f"  {name:28} {L:6.0f} m  anchors={have}  ask for {_queries(L)[0]}"
                  + (f" + listings at {w}" if w else ""))
        print("\nstreets that have listings waiting on them:")
        for name, L, have in sorted(todo, key=lambda t: -len(want.get(t[0], ()))):
            w = want.get(name, ())
            if not w:
                break
            print(f"  {name:28} {len(w)} numbered listing address(es)")
        return 0

    t0 = time.time()
    for i, (name, L, _have) in enumerate(todo, 1):
        if govmap.calls >= MAX_REQUESTS:
            print(f"\nrequest cap {MAX_REQUESTS} reached — re-run to continue")
            break
        got = seed_street(name, L, want.get(name, ()))
        if got:
            # MERGE, don't replace: a stranded street is revisited for the numbers it
            # still cannot place, and its earlier anchors are just as good as the new ones
            existing.setdefault(name, {}).update(got)
            save_anchors(existing)
        print(f"  [{i}/{len(todo)}] {name:26} +{len(got):2} anchors "
              f"({govmap.calls} requests, {time.time() - t0:.0f}s)")

    total = sum(len(v) for v in existing.values())
    print(f"\n{len(existing)} streets, {total} anchors -> {OUT_PATH}")
    print(f"{govmap.calls} requests this run")
    usable = sum(1 for v in existing.values() if len(v) >= 2)
    print(f"{usable} streets now have the >=2 anchors interpolation needs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
