"""
A picture of how the bot understands the whole area: the GREEN / AMBER / RED tier
field (computed with the real classifier — zones.classify_effective, straight-line
walk estimate per cell), the hand-drawn green zone, the ב/ג/ד neighborhoods, the
BGU campus and Soroka hospital, the campus gates, and the main streets.

    python area_map.py            # -> data/area_map.html (open in a browser)

Fully self-contained SVG (no tiles / no CDN). Landmarks + streets come from the
cached area_features.json (run load_area_features.py to refresh them).
"""
from __future__ import annotations
import html
import json
import math

import config
import zones

OUT = config.DATA_DIR / "area_map.html"
_W, _H, _PAD = 1100, 900, 30
_NX, _NY = 150, 125                     # tier-grid resolution (cells across / down)

_TIER_FILL = {"GREEN": "#48b04d", "AMBER": "#f3b64a", "RED": "#e8776b", "UNKNOWN": "#cfd4d9"}


def _features() -> dict:
    try:
        return json.loads((config.ROOT / "area_features.json").read_text(encoding="utf-8"))
    except Exception:
        return {"landmarks": [], "streets": []}


def _projector(pts):
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    min_la, max_la, min_lo, max_lo = min(lats), max(lats), min(lons), max(lons)
    # ~8% padding so nothing sits on the edge
    dla, dlo = (max_la - min_la) * 0.08, (max_lo - min_lo) * 0.08
    min_la, max_la, min_lo, max_lo = min_la - dla, max_la + dla, min_lo - dlo, max_lo + dlo
    kx = math.cos(math.radians((min_la + max_la) / 2))
    span_lo = max((max_lo - min_lo) * kx, 1e-9)
    span_la = max(max_la - min_la, 1e-9)
    scale = min((_W - 2 * _PAD) / span_lo, (_H - 2 * _PAD) / span_la)

    def xy(la, lo):
        return (_PAD + (lo - min_lo) * kx * scale, _PAD + (max_la - la) * scale)
    return xy, (min_la, max_la, min_lo, max_lo)


def _poly(xy, poly) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(la, lo) for la, lo in poly))


def _tier_grid(xy, bounds) -> list:
    """The tier color field. RED (out of range) is the whole-area base wash; only the
    in-range GREEN/AMBER cells get their own rect — that keeps the file light (a few
    thousand cells, not tens of thousands) instead of one rect per grid cell."""
    min_la, max_la, min_lo, max_lo = bounds
    cw = (max_lo - min_lo) / _NX
    ch = (max_la - min_la) / _NY
    x0, y0 = xy(max_la, min_lo)                       # top-left of the grid in SVG
    x1, y1 = xy(min_la, max_lo)                       # bottom-right
    px = (x1 - x0) / _NX + 0.6                        # +overlap to avoid hairline seams
    py = (y1 - y0) / _NY + 0.6
    out = [f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" height="{y1 - y0:.1f}" '
           f'fill="{_TIER_FILL["RED"]}"/>']           # RED base = out of range
    for j in range(_NY):
        la = max_la - (j + 0.5) * ch
        ry = y0 + j * (y1 - y0) / _NY
        for i in range(_NX):
            lo = min_lo + (i + 0.5) * cw
            tier = zones.classify_effective(la, lo)
            if tier == "RED":
                continue                              # already the base wash
            rx = x0 + i * (x1 - x0) / _NX
            out.append(f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{px:.1f}" height="{py:.1f}" '
                       f'fill="{_TIER_FILL.get(tier, _TIER_FILL["UNKNOWN"])}"/>')
    return out


def _in_bounds(la, lo, bounds) -> bool:
    min_la, max_la, min_lo, max_lo = bounds
    return min_la <= la <= max_la and min_lo <= lo <= max_lo


def build() -> str:
    zone = zones._polygon()
    gates = [(g["lat"], g["lon"], g.get("name", k)) for k, g in config.GATES.items()]
    nbhds = zones._neighborhood_polys()
    feats = _features()

    # Fit the view to the meaningful anchors (NOT the far ends of every street).
    anchors = list(zone) + [(la, lo) for la, lo, _ in gates]
    for _, poly in nbhds:
        anchors += list(poly)
    for lm in feats.get("landmarks", []):
        anchors += [(la, lo) for la, lo in lm["polygon_latlon"]]
    xy, bounds = _projector(anchors)

    svg = [f'<svg viewBox="0 0 {_W} {_H}" xmlns="http://www.w3.org/2000/svg" '
           f'font-family="system-ui,Arial">']
    svg.append(f'<rect width="{_W}" height="{_H}" fill="#eef1f4"/>')
    svg += _tier_grid(xy, bounds)                    # 1) the tier color field

    # 2) streets — a white casing + a line so they read over the colored tier grid.
    #    Main arteries are drawn boldly and always named; minor (residential) streets
    #    are a finer mesh and named only when a long-enough segment is in view.
    street_labels = []
    for st in feats.get("streets", []):
        main = st.get("main", True)                    # old files had arteries only
        cw, lw, lc, lo = (3.6, 1.7, "#2b333b", 0.9) if main else (2.2, 0.9, "#68727d", 0.6)
        best_seg, best_len = None, 0.0
        for seg in st.get("segments", []):
            if not any(_in_bounds(la, lo, bounds) for la, lo in seg):
                continue
            pts = _poly(xy, seg)
            svg.append(f'<polyline points="{pts}" fill="none" stroke="#ffffff" '
                       f'stroke-width="{cw}" stroke-opacity="0.5" stroke-linejoin="round"/>')
            svg.append(f'<polyline points="{pts}" fill="none" stroke="{lc}" '
                       f'stroke-width="{lw}" stroke-opacity="{lo}" stroke-linejoin="round"/>')
            (ax, ay), (bx, by) = xy(*seg[0]), xy(*seg[-1])
            seglen = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
            if seglen > best_len:
                best_len, best_seg = seglen, seg
        # name only the longer arteries (the dense network of lines carries the rest,
        # so labels stay readable, not a wall of text)
        if best_seg and best_len > (95 if main else 200):
            street_labels.append((xy(*best_seg[len(best_seg) // 2]), st["name"], main))

    # 3) green zone outline
    svg.append(f'<polygon points="{_poly(xy, zone)}" fill="none" stroke="#1b5e20" '
               f'stroke-width="3"/>')

    # 4) landmarks: BGU (blue), Soroka (magenta)
    _LM_STYLE = {"university": ("#3949ab", "אוניברסיטת בן גוריון"),
                 "hospital": ("#ad1457", "סורוקה")}
    for lm in feats.get("landmarks", []):
        color, label = _LM_STYLE.get(lm["kind"], ("#444", lm["name"]))
        svg.append(f'<polygon points="{_poly(xy, lm["polygon_latlon"])}" fill="{color}" '
                   f'fill-opacity="0.5" stroke="{color}" stroke-width="2"/>')
        cla = sum(p[0] for p in lm["polygon_latlon"]) / len(lm["polygon_latlon"])
        clo = sum(p[1] for p in lm["polygon_latlon"]) / len(lm["polygon_latlon"])
        lx, ly = xy(cla, clo)
        svg.append(f'<text x="{lx:.0f}" y="{ly:.0f}" font-size="15" fill="#fff" '
                   f'text-anchor="middle" font-weight="bold" '
                   f'style="paint-order:stroke;stroke:{color};stroke-width:3px">'
                   f'{html.escape(label)}</text>')

    # 5) neighborhood outlines + big labels
    for letter, poly in nbhds:
        svg.append(f'<polygon points="{_poly(xy, poly)}" fill="none" stroke="#1a237e" '
                   f'stroke-width="1.6" stroke-dasharray="6,4"/>')
        cla = sum(p[0] for p in poly) / len(poly)
        clo = sum(p[1] for p in poly) / len(poly)
        lx, ly = xy(cla, clo)
        svg.append(f'<text x="{lx:.0f}" y="{ly:.0f}" font-size="26" fill="#1a237e" '
                   f'text-anchor="middle" font-weight="bold" opacity="0.75">'
                   f'שכונה {html.escape(letter)}</text>')

    # 6) street names — on top, white-haloed so they read over everything. Main
    #    arteries a touch larger/darker than minor streets.
    for (sx, sy), name, main in street_labels:
        fs, fill = (11, "#1c2229") if main else (9, "#3c454e")
        svg.append(f'<text x="{sx:.0f}" y="{sy:.0f}" font-size="{fs}" fill="{fill}" '
                   f'text-anchor="middle" style="paint-order:stroke;stroke:#fff;'
                   f'stroke-width:2.6px">{html.escape(name)}</text>')

    # 7) gates
    for la, lo, name in gates:
        gx, gy = xy(la, lo)
        svg.append(f'<circle cx="{gx:.1f}" cy="{gy:.1f}" r="4.5" fill="#111"/>')
        svg.append(f'<text x="{gx + 7:.1f}" y="{gy + 4:.1f}" font-size="12" fill="#111" '
                   f'font-weight="bold" style="paint-order:stroke;stroke:#fff;stroke-width:3.5px">'
                   f'★ {html.escape(name)}</text>')
    svg.append("</svg>")

    legend = (
        f'<span style="background:{_TIER_FILL["GREEN"]}">&nbsp;&nbsp;</span> GREEN — inside the zone &nbsp; '
        f'<span style="background:{_TIER_FILL["AMBER"]}">&nbsp;&nbsp;</span> AMBER — ≤20 min walk to a gate &nbsp; '
        f'<span style="background:{_TIER_FILL["RED"]}">&nbsp;&nbsp;</span> RED — out of range')
    page = (
        "<!doctype html><meta charset='utf-8'><title>How the bot sees the area</title>"
        "<div style='font-family:system-ui;padding:12px;max-width:1140px'>"
        "<h2 style='margin:0 0 4px'>How the bot understands the area</h2>"
        f"<p style='margin:0 0 8px;font-size:14px'>{legend}</p>"
        "<p style='margin:0 0 10px;font-size:13px;color:#333'>"
        "<b style='color:#1b5e20'>▭ green zone</b> (hand-drawn) &nbsp; "
        "<b style='color:#1a237e'>▭ ב/ג/ד</b> neighborhoods &nbsp; "
        "<b style='color:#3949ab'>■ BGU</b> &nbsp; <b style='color:#ad1457'>■ Soroka</b> &nbsp; "
        "● ★ campus gates &nbsp; — dark named lines are the main streets.</p>"
        + "".join(svg) +
        "</div>")
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT}")
    return page


if __name__ == "__main__":
    build()
