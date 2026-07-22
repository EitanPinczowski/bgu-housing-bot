"""
Daily DM-only digest — sent to your PRIVATE chat, never the group.

Right now it reports the locations the bot extracted from posts but couldn't map
(so they went NEEDS_DATA/UNKNOWN and were likely silenced). Pinning the frequent
ones to geocode.STATIC_TABLE closes that whole area's gap — this is exactly how
"הבלוק" was being missed.

    python dm_digest.py [days]      # default 1 (the day)
"""
from __future__ import annotations
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import config
import notifier
import storage

import re

_OVERPASS_EPS = ("https://maps.mail.ru/osm/tools/overpass/api/interpreter",
                 "https://overpass-api.de/api/interpreter")
_BS_BBOX = "31.22,34.75,31.29,34.85"          # Be'er Sheva query box (S,W,N,E)
# generic address words to drop when picking a street's distinctive token
_GENERIC = {"רחוב", "רח", "שדרות", "שד", "דרך", "סמטת", "סמטה", "שביל", "שכונה",
            "שכונת", "רחבת", "כיכר", "באר", "שבע", "ליד", "מול", "ישראל", "קומה", "דירה"}
_sugg_cache: dict = {}


def _core_token(name: str):
    """The most distinctive Hebrew word in a location string to search OSM by —
    drop generic address words (רחוב/שכונה/באר שבע…), digits and punctuation, take
    the longest remaining word. Returns None if nothing distinctive is left."""
    toks = [w for w in re.findall(r"[א-ת]{2,}", name) if w not in _GENERIC]
    return max(toks, key=len) if toks else None


def _suggest(name: str):
    """A candidate pin for an unmapped name via OpenStreetMap/Overpass — a street
    or place whose name contains the location's distinctive Hebrew token, within
    the Be'er Sheva box (Overpass resolves BS street names that Nominatim can't).
    Paced/cached; returns (osm_name, lat, lon) or None. Rough — labelled for review."""
    token = _core_token(name)
    if not token:
        return None
    if token in _sugg_cache:
        return _sugg_cache[token]
    import time
    import requests
    time.sleep(1.2)                           # be gentle with Overpass
    q = (f'[out:json][timeout:25];'
         f'(way["highway"]["name"~"{token}"]({_BS_BBOX});'
         f'node["place"]["name"~"{token}"]({_BS_BBOX}););out center 1;')
    res = None
    for ep in _OVERPASS_EPS:
        try:
            r = requests.post(ep, data={"data": q}, timeout=40,
                              headers={"User-Agent": config.NOMINATIM_USER_AGENT})
            r.raise_for_status()
            els = r.json().get("elements", [])
            if els:
                e = els[0]
                c = e.get("center") or e
                res = (e.get("tags", {}).get("name", ""), round(c["lat"], 5), round(c["lon"], 5))
            break
        except Exception:
            continue
    _sugg_cache[token] = res
    return res


def _low_confidence_section() -> list:
    """Kept listings geocoded by a fuzzy source (Overpass/Nominatim) rather than the
    trusted static table — flagged so a wrong pin gets a human glance."""
    rows = storage.low_confidence_geocodes()
    if not rows:
        return []
    out = [notifier._esc("📍 מוקמו ע\"י גאוקוד לא-ודאי (Overpass/Nominatim) — שווה מבט:")]
    for addr, tier, src in rows:
        out.append(notifier._esc(f"• {addr or '—'} [{tier or '?'}] ({src})"))
    return out


def build(days: int = 1, suggest: bool = True) -> str | None:
    rows = storage.unknown_locations(days)
    low = _low_confidence_section()
    if not rows and not low:
        return None
    lines: list = []
    if rows:
        head = f"🗺️ מקומות שלא הצלחתי למפות ({days} ימים אחרונים) — שווה להוסיף לטבלת הגאוקוד:"
        lines += [notifier._esc(head), ""]
        for i, (loc, cnt, _) in enumerate(rows[:20]):
            line = f"• {loc} ×{cnt}"
            s = _suggest(loc) if (suggest and i < 15) else None      # cap the paced calls
            if s:
                osm_name, la, lo = s
                line += f"  →  {osm_name} {la},{lo} (לאימות)"
            lines.append(notifier._esc(line))
    if low:
        if lines:
            lines.append("")
        lines += low
    return "\n".join(lines)


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    text = build(days)
    if not text:
        print("no unknown locations to report")
        return
    notifier.send(text, target="primary")     # your DM only, never the group
    print("sent DM digest")


if __name__ == "__main__":
    main()
