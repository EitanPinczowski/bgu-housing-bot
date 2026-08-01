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


def relevant_streets() -> list:
    """[(street, length_m, existing_anchor_count)] for streets that matter and are thin.

    "Matter" = some part of the street is GREEN or AMBER, i.e. a flat there could
    actually be a match. Seeding the whole city would be wasted requests."""
    anchors = geocode._load_anchors()
    la0, lo0, la1, lo1 = geocode._bs_bounds()
    out = []
    for name in sorted({s for s in streets._index().values()}):
        segs = streets.geometry(name)
        pts = [p for s in segs for p in s]
        if not pts:
            continue
        hit = False
        for p in pts[::5]:
            if not (la0 <= p[0] <= la1 and lo0 <= p[1] <= lo1):
                continue
            tier = zones.classify_location(p[0], p[1])
            if tier and str(tier[0]).upper().startswith(("G", "A")):
                hit = True
                break
        if not hit:
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


def _accept(street: str, text: str, pt) -> tuple:
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
    if off is not None and off > MAX_OFFSET_M:
        return None
    return number, pt


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
            got = _accept(street, text, pt)
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


def seed_exact(pairs: list, existing: dict) -> int:
    """Ask govmap for each address directly. Returns how many were accepted."""
    added = 0
    for i, (st, hn) in enumerate(pairs, 1):
        if govmap.calls >= MAX_REQUESTS:
            print(f"\nrequest cap {MAX_REQUESTS} reached — re-run to continue")
            break
        got = None
        for kind, text, pt in govmap.search(f"{st} {hn} באר שבע"):
            if kind != "address":
                continue
            got = _accept(st, text, pt)
            if got:
                break
        if got:
            existing.setdefault(st, {})[got[0]] = [round(got[1][0], 6), round(got[1][1], 6)]
            added += 1
            OUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, sort_keys=True,
                                           indent=1), encoding="utf-8")
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
            OUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, sort_keys=True,
                                           indent=1), encoding="utf-8")
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
