"""
Retry geocoding on the backlog of addresses the bot couldn't map, and propose pins
for `geocode.STATIC_TABLE` so recurring names resolve permanently.

Pulls the logged `unknown_locations` plus every NEEDS_DATA listing's address, runs each
through the current (hardened) geocoder chain, and prints: how many now resolve, the
ones still unmapped (split into "has a street" vs "vague — no address"), and a
ready-to-paste STATIC_TABLE block for the resolved names (review before pinning).

    python resolve_unknowns.py

Read-only: it never writes code or the DB — you copy the proposals you trust.
Slow (Overpass is paced ~1 req/s); run it occasionally, not per scrape.
"""
from __future__ import annotations
import re
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import geocode
import storage


def _backlog() -> list:
    """Distinct unmapped address strings: logged unknowns + NEEDS_DATA listing addresses."""
    seen, out = set(), []
    for loc, _c, _t in storage.unknown_locations(days=3650):
        if loc and loc not in seen:
            seen.add(loc)
            out.append(loc)
    with sqlite3.connect(config.DB_PATH) as c:
        for (a,) in c.execute("SELECT DISTINCT address FROM listings "
                              "WHERE status='NEEDS_DATA' AND address IS NOT NULL"):
            if a and a not in seen:
                seen.add(a)
                out.append(a)
    return out


# a digit right after Hebrew text = a house number ("אברהם אבינו 38"), unlike a stray
# number in prose ("5 דקות מהאוניברסיטה").
_HOUSENUM = re.compile(r"[א-ת]\s*\d")


def _is_vague(a: str) -> bool:
    """No street word, no house number, not a neighborhood — nothing to pin."""
    if geocode.is_bare_neighborhood(a):
        return False
    if any(w in a for w in geocode._STREET_WORDS):
        return False
    return not _HOUSENUM.search(a)


def resolve(addresses: list) -> tuple:
    """(resolved, still_unknown, proposals). resolved/proposals map name→(coords,source);
    a proposal is a fuzzy (overpass/nominatim/osm_addr) hit worth pinning."""
    resolved, still, proposals = {}, [], {}
    for a in addresses:
        coords, source = geocode.geocode_detailed(a)
        if coords:
            resolved[a] = (coords, source)
            if source in ("overpass", "osm_addr", "nominatim"):
                proposals[a] = (coords, source)
        else:
            still.append(a)
    return resolved, still, proposals


def main() -> None:
    backlog = _backlog()
    print(f"retrying {len(backlog)} unmapped addresses through the geocoder …\n")
    resolved, still, proposals = resolve(backlog)
    vague = [a for a in still if _is_vague(a)]
    hard = [a for a in still if not _is_vague(a)]
    print(f"resolved now: {len(resolved)}   still unmapped: {len(still)} "
          f"({len(hard)} with a street, {len(vague)} vague/no-address)\n")
    if hard:
        print("— still unmapped but HAS a street (worth a manual pin / static entry):")
        for a in hard:
            print(f"    {a!r}")
    if proposals:
        print("\n— proposed STATIC_TABLE pins (review, then paste into geocode.STATIC_TABLE):")
        for a, (c, src) in sorted(proposals.items()):
            print(f'    "{a}": ({c[0]:.5f}, {c[1]:.5f}),  # via {src}')


if __name__ == "__main__":
    main()
