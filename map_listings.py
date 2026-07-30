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
import json
import math
import sqlite3

import config
import geocode
import zones

OUT = config.DATA_DIR / "listings_map.html"
_TIER_COLOR = {"GREEN": "#2e7d32", "AMBER": "#e08e0b", "RED": "#c0392b", "UNKNOWN": "#7f8c8d"}
_W, _H, _PAD = 1000, 820, 34


def _load_listings():
    """(lat, lon, tier, score, address, price, walk, dedup_key) for each mappable
    listing, plus the count that couldn't be geocoded.

    `dedup_key` is LAST on purpose: build()'s `for _, _, tier, *_ in placed` counter and
    any other positional reader keep working, and it's what lets the dashboard tie a map
    dot to its table row."""
    with sqlite3.connect(config.DB_PATH) as c:
        rows = c.execute("SELECT address, location_tier, score, price_per_room, "
                         "walk_minutes, dedup_key FROM listings").fetchall()
    placed, unplaced = [], 0
    for addr, tier, score, price, walk, key in rows:
        coords = geocode.geocode_cached(addr)
        if coords:
            placed.append((coords[0], coords[1], tier or "UNKNOWN", score, addr, price,
                           walk, key))
        else:
            unplaced += 1
    return placed, unplaced


def _projector(pts):
    """(xy, params) — a lat/lon -> SVG (x,y) function fitted to the bounding box of
    `pts`, with the longitude squeezed by cos(lat) so the map isn't horizontally
    stretched, PLUS the constants it closes over.

    The params are what let the browser place dots itself: the dashboard draws the
    backdrop server-side but the dots client-side (so they can follow the filters), and
    both must use the exact same projection or the dots drift off the map."""
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
    return xy, {"min_lon": min_lo, "max_lat": max_la, "kx": kx, "scale": scale,
                "pad": _PAD, "w": _W, "h": _H}


def xy_from(projection):
    """Rebuild the projection function from its parameters alone.

    Used by build_svg here and mirrored line-for-line by the dashboard's JS. Deriving the
    server-side dots through the SAME params the browser gets means the two can't drift:
    if these constants were insufficient, the standalone map would break first."""
    p = projection

    def xy(la, lo):
        return (p["pad"] + (lo - p["min_lon"]) * p["kx"] * p["scale"],
                p["pad"] + (p["max_lat"] - la) * p["scale"])
    return xy


def _poly_points(xy, poly) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(la, lo) for la, lo in poly))


# Only pin targets with a HANDFUL of locations. The "bus toward the train station"
# target legitimately matches 428 stops — nearly every stop in the city — so pinning it
# would bury the map under icons while telling you nothing. The 669 stops (2) and the
# gym (1) are distinctive and worth showing.
_MAX_PINS_PER_TARGET = 12


def _amenity_pins():
    """(lat, lon, icon, label) for the transit stops and places in amenities.json, so the
    map can show WHY a listing's amenity line says what it says. Missing file -> none."""
    try:
        data = json.loads(config.AMENITIES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for target in (data.get("targets") or {}).values():
        places = (target.get("stops") or []) + (target.get("points") or [])
        if len(places) > _MAX_PINS_PER_TARGET:
            continue
        icon = target.get("icon") or "•"
        for p in places:
            if p.get("lat") is not None:
                out.append((p["lat"], p["lon"], icon, p.get("name", "")))
    return out


def build_base_svg(placed=None):
    """(backdrop_svg, projection) — the map WITHOUT any listing dots: the green zone,
    the ב/ג/ד outlines, the campus gates and the amenity pins.

    Split out so the dashboard can draw dots in the browser (where they can follow the
    filters) on top of a backdrop rendered here. `placed` is only used to fit the
    viewport, so the same projection covers every listing."""
    zone = zones._polygon()
    gates = [(g["lat"], g["lon"], g.get("name", k)) for k, g in config.GATES.items()]
    nbhds = zones._neighborhood_polys()
    pins = _amenity_pins()

    pts = [(la, lo) for la, lo, *_ in (placed or [])] + list(zone)
    pts += [(la, lo) for la, lo, _ in gates]
    for _, poly in nbhds:
        pts += [(la, lo) for la, lo in poly]
    if not pts:
        pts = [(31.26, 34.80)]
    xy, projection = _projector(pts)

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
    # amenity pins (the 669 stops, the bus to the train, the gym)
    for la, lo, icon, name in pins:
        px, py = xy(la, lo)
        svg.append(f'<text x="{px:.1f}" y="{py:.1f}" font-size="11" text-anchor="middle" '
                   f'dominant-baseline="central" opacity="0.75">{html.escape(icon)}'
                   f'<title>{html.escape(name)}</title></text>')
    return "".join(svg), projection


def build_svg():
    """(svg_markup, placed_rows, unplaced_count) — the whole map, dots included, for the
    standalone page. Built on top of build_base_svg so there is one backdrop renderer."""
    placed, unplaced = _load_listings()
    base, projection = build_base_svg(placed)
    xy = xy_from(projection)

    svg = [base]                       # build_base_svg leaves the <svg> open
    for la, lo, tier, score, addr, price, walk, _key in placed:
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
    return "".join(svg), placed, unplaced


def build() -> str:
    svg_markup, placed, unplaced = build_svg()
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
        + svg_markup +
        "</div>")
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT}  ({len(placed)} placed, {unplaced} unmapped)")
    return page


if __name__ == "__main__":
    build()
