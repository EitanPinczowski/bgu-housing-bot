"""
How far from the real building does the geocoder actually land?

    python geo_accuracy.py              # error in metres, by confidence tier
    python geo_accuracy.py --sample 400 # bigger hold-out
    python geo_accuracy.py --truth new_anchors.json   # grade against a DIFFERENT file

WHY THIS EXISTS
---------------
"More accurate" was an opinion, because nothing measured it. `audit_geocode.py` answers
a different question — is this point ON the street it claims? — which catches blunders
(the 520 m וינגייט bug) but says nothing about whether we found the right building.

Ground truth comes from OSM's own `addr:housenumber` anchors: surveyed points for real
addresses. The trick is the HOLD-OUT — each address under test has its own anchor hidden
before the geocoder runs, so we measure what the geocoder does for an address it has no
direct answer for. Without that it would grade itself against its own answer key and
report a perfect zero.

Report p50/p90/max by tier. A tier that claims `exact` and lands 300 m out is a lie the
map is telling; this is what makes that visible.

`--truth` exists because the answer key and the geocoder read the SAME file, so improving
the anchors improves the ruler at the same time and the two gains are indistinguishable.
Pointing the truth at the new file while the geocoder still reads the old one measures
the geocoder alone.
"""
from __future__ import annotations
import json
import random
import statistics
import sys
from pathlib import Path

import geocode
import streets

SAMPLE = 250


def _held_out(street: str, number: str, anchors: dict) -> dict:
    """A copy of `anchors` with this one address removed, so the geocoder must reach
    it the way it reaches an address OSM has never heard of."""
    out = {k: dict(v) for k, v in anchors.items()}
    if street in out:
        out[street].pop(number, None)
        if not out[street]:
            del out[street]
    return out


def run(sample: int = SAMPLE, seed: int = 7, truth_path: str | None = None) -> dict:
    anchors = geocode._load_anchors()
    key = anchors
    if truth_path:
        key = json.loads(Path(truth_path).read_text(encoding="utf-8"))
        print(f"grading against {truth_path}, geocoding with house_anchors.json")
    # the address list comes from the ANSWER KEY, not from the geocoder's own file, so
    # two runs graded against the same key test exactly the same addresses even when
    # their anchor files differ
    truth = sorted((st, num, tuple(pt))
                   for st, nums in key.items() for num, pt in nums.items()
                   if str(num).isdigit())
    if not truth:
        print("no anchors — run load_osm_addresses.py first")
        return {}
    random.Random(seed).shuffle(truth)
    truth = truth[:sample]
    print(f"holding out {len(truth)} known addresses, one at a time\n")

    by_tier: dict = {}
    unresolved = 0
    original = geocode._anchors
    real_cache = geocode._load_cache()
    real_save = geocode._save_cache
    # Blanking the cache is how the hold-out avoids answering from a previous run. But
    # `geocode_detailed` PERSISTS the cache after any successful external lookup, so the
    # blank dict was being written straight to disk — one run of this tool destroyed the
    # real cache (measured: 1 entry left). Disable the write for the duration.
    geocode._save_cache = lambda: None
    try:
        for street, number, (tlat, tlon) in truth:
            geocode._anchors = _held_out(street, number, anchors)
            geocode._cache = {}                      # never answer from a cached hit
            got, source = geocode.geocode_detailed(f"{street} {number}")
            if not got:
                unresolved += 1
                continue
            err = geocode._haversine_m(tlat, tlon, got[0], got[1])
            by_tier.setdefault(geocode.confidence(source), []).append(err)
    finally:
        geocode._anchors = original
        geocode._cache = real_cache               # the REAL one back, not a blank
        geocode._save_cache = real_save

    order = ["exact", "high", "street", "area", "none"]
    print(f"{'tier':10} {'n':>5} {'p50':>8} {'p90':>8} {'max':>8}")
    for tier in order:
        errs = sorted(by_tier.get(tier) or [])
        if not errs:
            continue
        p90 = errs[min(len(errs) - 1, int(len(errs) * 0.9))]
        print(f"{tier:10} {len(errs):5} {statistics.median(errs):7.0f}m "
              f"{p90:7.0f}m {errs[-1]:7.0f}m")
    allerrs = sorted(e for v in by_tier.values() for e in v)
    if allerrs:
        p90 = allerrs[min(len(allerrs) - 1, int(len(allerrs) * 0.9))]
        print(f"{'ALL':10} {len(allerrs):5} {statistics.median(allerrs):7.0f}m "
              f"{p90:7.0f}m {allerrs[-1]:7.0f}m")
    if unresolved:
        print(f"\n{unresolved} of {len(truth)} could not be placed at all "
              f"(no dot on the map)")
    return by_tier


if __name__ == "__main__":
    n = SAMPLE
    if "--sample" in sys.argv:
        n = int(sys.argv[sys.argv.index("--sample") + 1])
    tp = None
    if "--truth" in sys.argv:
        tp = sys.argv[sys.argv.index("--truth") + 1]
    # streets.py is imported for its side-effect-free index; keep ruff happy about it
    assert streets
    run(n, truth_path=tp)
