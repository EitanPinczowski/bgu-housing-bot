"""
Plot the stored listings on a self-contained HTML/SVG map, colored by tier —
the visual complement to replay.py for eyeballing whether the hand-drawn
green_zone.json is clipping good areas.

    python map_listings.py            # -> data/listings_map.html (open in a browser)

No internet / no tile server / no CDN: it draws the green polygon, the ב/ג/ד
neighborhood outlines, the campus gates, and a dot per listing (GREEN/AMBER/RED/
UNKNOWN) into an inline SVG, so it works forever and offline. Coordinates come from
geocode.geocode(address) (cached), so a listing whose address can't be mapped is
listed as unplaced rather than dropped silently.
"""
from __future__ import annotations
import html
import math
import sqlite3

import config
import geocode
import zones

OUT = config.DATA_DIR / "listings_map.html"
_TIER_COLOR = {"GREEN": "#2e7d32", "AMBER": "#e08e0b", "RED": "#c0392b", "UNKNOWN": "#7f8c8d"}
_W, _H, _PAD = 1000, 820, 34


def _load_listings():
    """(lat, lon, tier, score, address, price, walk) for each mappable listing, plus
    the count that couldn't be geocoded."""
    with sqlite3.connect(config.DB_PATH) as c:
        rows = c.execute("SELECT address, location_tier, score, price_per_room, walk_minutes "
                         "FROM listings").fetchall()
    placed, unplaced = [], 0
    for addr, tier, score, price, walk in rows:
        coords = geocode.geocode(addr)
        if coords:
            placed.append((coords[0], coords[1], tier or "UNKNOWN", score, addr, price, walk))
        else:
            unplaced += 1
    return placed, unplaced


def _projector(pts):
    """A lat/lon -> SVG (x,y) function fitted to the bounding box of `pts`, with the
    longitude squeezed by cos(lat) so the map isn't horizontally stretched."""
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    min_la, max_la, min_lo, max_lo = min(lats), max(lats), min(lons), max(lons)
    kx = math.cos(math.radians((min_la + max_la) / 2))
    span_lo = max((max_lo - min_lo) * kx, 1e-9)
    span_la = max(max_la - min_la, 1e-9)
    scale = min((_W - 2 * _PAD) / span_lo, (_H - 2 * _PAD) / span_la)

    def xy(la, lo):
        return (_PAD + (lo - min_lo) * kx * scale,
                _PAD + (max_la - la) * scale)          # invert: SVG y grows downward
    return xy


def _poly_points(xy, poly) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(la, lo) for la, lo in poly))


def build() -> str:
    placed, unplaced = _load_listings()
    zone = zones._polygon()
    gates = [(g["lat"], g["lon"], g.get("name", k)) for k, g in config.GATES.items()]
    nbhds = zones._neighborhood_polys()

    pts = [(la, lo) for la, lo, *_ in placed] + list(zone) + [(la, lo) for la, lo, _ in gates]
    for _, poly in nbhds:
        pts += [(la, lo) for la, lo in poly]
    if not pts:
        pts = [(31.26, 34.80)]
    xy = _projector(pts)

    svg = [f'<svg viewBox="0 0 {_W} {_H}" xmlns="http://www.w3.org/2000/svg">']
    svg.append(f'<rect width="{_W}" height="{_H}" fill="#f6f7f9"/>')
    # the green zone
    svg.append(f'<polygon points="{_poly_points(xy, zone)}" fill="#2e7d32" '
               f'fill-opacity="0.10" stroke="#2e7d32" stroke-width="2"/>')
    # neighborhood outlines + labels
    for letter, poly in nbhds:
        svg.append(f'<polygon points="{_poly_points(xy, poly)}" fill="none" '
                   f'stroke="#3367d6" stroke-width="1.4" stroke-dasharray="5,4"/>')
        cla = sum(p[0] for p in poly) / len(poly)
        clo = sum(p[1] for p in poly) / len(poly)
        lx, ly = xy(cla, clo)
        svg.append(f'<text x="{lx:.0f}" y="{ly:.0f}" font-size="18" fill="#3367d6" '
                   f'text-anchor="middle" font-weight="bold">{html.escape(letter)}</text>')
    # gates
    for la, lo, name in gates:
        gx, gy = xy(la, lo)
        svg.append(f'<text x="{gx:.1f}" y="{gy:.1f}" font-size="16" text-anchor="middle" '
                   f'dominant-baseline="central">★<title>{html.escape(name)}</title></text>')
    # listings
    for la, lo, tier, score, addr, price, walk in placed:
        cx, cy = xy(la, lo)
        color = _TIER_COLOR.get(tier, _TIER_COLOR["UNKNOWN"])
        tip = f"{addr or '—'} | {tier} | ⭐{score if score is not None else '?'}"
        if price:
            tip += f" | {price}₪"
        if walk is not None:
            tip += f" | {round(walk)}min"
        svg.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{color}" '
                   f'fill-opacity="0.85" stroke="#fff" stroke-width="1">'
                   f'<title>{html.escape(tip)}</title></circle>')
    svg.append("</svg>")

    counts: dict = {}
    for _, _, tier, *_ in placed:
        counts[tier] = counts.get(tier, 0) + 1
    legend = " &nbsp; ".join(
        f'<span style="color:{_TIER_COLOR[t]}">●</span> {t} {counts.get(t, 0)}'
        for t in ("GREEN", "AMBER", "RED", "UNKNOWN"))
    page = (
        "<!doctype html><meta charset='utf-8'><title>BGU listings map</title>"
        "<div style='font-family:system-ui;padding:12px'>"
        f"<h2 style='margin:0 0 6px'>Listings by tier — {len(placed)} placed, {unplaced} unmapped</h2>"
        f"<p style='margin:0 0 10px'>{legend} &nbsp;|&nbsp; "
        "<span style='color:#2e7d32'>▨</span> green zone &nbsp; "
        "<span style='color:#3367d6'>▭</span> ב/ג/ד &nbsp; ★ gate &nbsp;"
        "<em>(hover a dot for details)</em></p>"
        + "".join(svg) +
        "</div>")
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT}  ({len(placed)} placed, {unplaced} unmapped)")
    return page


if __name__ == "__main__":
    build()
