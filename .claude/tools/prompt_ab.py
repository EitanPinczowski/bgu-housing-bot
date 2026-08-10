#!/usr/bin/env python
"""Did the new PRICE DIVISION RULE help? Old prompt vs new, same model, same posts.

`model_ab.py report` compares two MODELS. This compares two PROMPTS, which is a different
question and needs a different control: the same model, the same pinned sample, only the
prompt changed. The old answers live in `data/model_ab/oldprompt-20260807/` — copied there
before re-running, because `cmd_ask` OVERWRITES `answers-<model>.json` and would otherwise
have destroyed the only control that existed.

    python .claude/tools/prompt_ab.py [model ...]

Reports, per model:
  * the noise floor (pass 0 vs pass 1) for each prompt — a change smaller than this is
    not a finding
  * price: gained / lost / changed, and specifically whether a WHOLE-FLAT total stopped
    being reported as a per-room price
  * the gate from the prompt-tuning skill: no post may flip `is_apartment_ad`, and no
    MATCH-eligible post may lose price, rooms or address beyond the noise floor
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
AB = ROOT / "data" / "model_ab"
OLD = AB / "oldprompt-20260807"
GATE_FIELDS = ("price_per_room_ils", "available_rooms_count",
               "street_address_or_neighborhood")


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))["runs"]


def _self_agree(runs, field):
    a, b = runs[0], runs[1]
    n = min(len(a), len(b))
    return sum(1 for i in range(n) if a[i].get(field) == b[i].get(field)), n


def _looks_like_total(price, mates, text):
    """Is this price plausibly the WHOLE FLAT's rent reported as a per-room price?

    Not a proof — a heuristic for REPORTING, never for a gate. It asks whether the exact
    number appears in the post AND dividing it by the stated resident count would land in
    the plausible per-room band. That is the shape of both measured failures: 2,800 for a
    4-room flat for 2, and 2,950 for a 3-room flat for couples."""
    if not price or not mates or mates < 2:
        return False
    if price < 2000:                       # already inside the per-room filter band
        return False
    digits = re.sub(r"[^\d]", "", str(int(price)))
    flat = re.sub(r"[^\d]", "", text)
    return digits in flat and 500 <= price / mates <= 2500


def compare(model: str) -> None:
    new_p, old_p = AB / f"answers-{model}.json", OLD / f"answers-{model}.json"
    if not new_p.exists() or not old_p.exists():
        print(f"  {model}: missing answers ({'new' if not new_p.exists() else 'old'})")
        return
    new, old = _load(new_p), _load(old_p)
    sample = json.loads((AB / "sample.json").read_text(encoding="utf-8"))
    n = min(len(new[0]), len(old[0]), len(sample))

    print(f"\n=== {model} — {n} posts ===")
    for label, runs in (("old prompt", old), ("new prompt", new)):
        bits = []
        for f in ("price_per_room_ils", "available_rooms_count", "is_apartment_ad"):
            a, t = _self_agree(runs, f)
            bits.append(f"{f.split('_')[0]} {a}/{t}")
        print(f"  noise floor, {label}: " + ", ".join(bits))

    gained = lost = changed = 0
    fixed_totals, new_totals, flips = [], [], []
    for i in range(n):
        o, w = old[0][i], new[0][i]
        po, pw = o.get("price_per_room_ils"), w.get("price_per_room_ils")
        text = sample[i].get("text", "")
        if o.get("is_apartment_ad") != w.get("is_apartment_ad"):
            flips.append(i)
        if po is None and pw is not None:
            gained += 1
        elif po is not None and pw is None:
            lost += 1
            if _looks_like_total(po, o.get("total_roommates_in_apt"), text):
                fixed_totals.append((i, po, o.get("total_roommates_in_apt")))
        elif po != pw:
            changed += 1
            if _looks_like_total(po, o.get("total_roommates_in_apt"), text):
                fixed_totals.append((i, po, o.get("total_roommates_in_apt")))
        if _looks_like_total(pw, w.get("total_roommates_in_apt"), text):
            new_totals.append((i, pw, w.get("total_roommates_in_apt")))

    print(f"  price: +{gained} gained, -{lost} lost, {changed} changed")
    print(f"  whole-flat totals FIXED by the new rule: {len(fixed_totals)}"
          + (f"  {[f'[{i}] {p}/{m}' for i, p, m in fixed_totals[:6]]}" if fixed_totals else ""))
    print(f"  whole-flat totals STILL reported:        {len(new_totals)}"
          + (f"  {[f'[{i}] {p}/{m}' for i, p, m in new_totals[:6]]}" if new_totals else ""))

    # --- the gate ---
    print("  GATE:")
    print(f"    is_apartment_ad flips: {len(flips)}"
          f"{'  <== FAIL' if flips else '  ok'}{'  ' + str(flips[:8]) if flips else ''}")
    for f in GATE_FIELDS:
        floor_a, floor_t = _self_agree(new, f)
        floor_loss = floor_t - floor_a                 # what the model loses against ITSELF
        real_loss = sum(1 for i in range(n)
                        if old[0][i].get(f) is not None and new[0][i].get(f) is None)
        verdict = "ok" if real_loss <= floor_loss else "<== FAIL"
        print(f"    {f:32} lost {real_loss:3}  (noise floor {floor_loss:3})  {verdict}")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    models = argv or ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    if not OLD.exists():
        print(f"no control at {OLD} — the old answers were not preserved.")
        return 1
    for m in models:
        compare(m)
    print("\nA change smaller than the noise floor is not a finding (see `evidence-rules`).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
