"""
A simple 0–100 "fit" score for a listing, so the best options surface from the
flood — used to sort the digest best-first and to show ⭐ on alerts.

Weights the things you care about: inside vs near the green zone, short walk,
low price. Unknown fields are treated as mild uncertainty, never as good.
"""
from __future__ import annotations
from typing import Optional

import config


def score(price: Optional[int], walk_min: Optional[float], tier: Optional[str]) -> int:
    s = 50
    if tier == "GREEN":
        s += 20
    elif tier == "AMBER":
        s += 5

    if walk_min is not None:
        s += (20 if walk_min < 10 else 12 if walk_min < 15
              else 5 if walk_min < 20 else -5)

    if price is not None:
        cap = config.MAX_PRICE_PER_ROOM_ILS
        s += (18 if price < cap * 0.6 else 12 if price < cap * 0.75
              else 6 if price < cap * 0.9 else 0)
    else:
        s -= 4   # unknown price = mild uncertainty

    return max(0, min(100, s))


def stars(points: int) -> str:
    """1–5 ⭐ from a 0–100 score."""
    return "⭐" * max(1, min(5, round(points / 20)))
