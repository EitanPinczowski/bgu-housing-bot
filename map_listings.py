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


def _bounds_of(projection):
    """(min_lat, max_lat, min_lon, max_lon) actually visible in the canvas — the inverse
    of the projection at the four corners. Used to cull street geometry to the view."""
    p = projection
    min_lo = p["min_lon"]
    max_lo = min_lo + (p["w"] - 2 * p["pad"]) / (p["kx"] * p["scale"])
    max_la = p["max_lat"]
    min_la = max_la - (p["h"] - 2 * p["pad"]) / p["scale"]
    return (min_la, max_la, min_lo, max_lo)


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


# --- the street network + landmarks, shared with area_map.py ----------------------
# area_features.json holds 1,174 named streets (4,038 segments) and the BGU/Soroka
# footprints, fetched once by load_area_features.py. Rendering lives here because both
# maps need it and a second copy of these constants would drift.
_FEATURES_PATH = config.ROOT / "area_features.json"
# Arteries are drawn boldly and named readily; the residential mesh is thinner, fainter
# and named only on a long run — that label rule is what keeps 1,174 streets from
# becoming a wall of text.
_ARTERY = (3.6, 1.7, "#2b333b", 0.9, 95)      # casing, line, colour, opacity, min label px
_MINOR = (2.2, 0.9, "#68727d", 0.6, 200)
# Labels are now revealed BY ZOOM rather than filtered once at build time. Measured on
# the dashboard's viewport: 412 streets are drawn but only 26 cleared the old
# thresholds — every one an artery, because a residential street never spans 200 px of
# a 1000 px canvas covering ~4 km. So emit down to a much shorter run and let each
# label carry the zoom at which it becomes legible.
_LABEL_MIN_PX = 25            # shortest visible run that can ever earn a name
_LABEL_CAP = 250              # keep the file sane; longest runs win
_LABEL_TARGET_PX = 90         # a name shows once its run reaches this on screen
_LANDMARK_STYLE = {"university": ("#3949ab", "אוניברסיטת בן גוריון"),
                   "hospital": ("#ad1457", "סורוקה")}


def features() -> dict:
    try:
        return json.loads(_FEATURES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"landmarks": [], "streets": []}


def _in_bounds(la, lo, bounds) -> bool:
    min_la, max_la, min_lo, max_lo = bounds
    return min_la <= la <= max_la and min_lo <= lo <= max_lo


# Styling lives in CSS and the geometry in four combined <path>s, rather than ~110
# characters of stroke attributes on each of ~2,800 <polyline>s. That took the street
# layer from 620 KB to ~210 KB and, just as importantly, from 2,804 DOM nodes to 4 —
# which is what keeps the dashboard's zoom smooth. Casings all render beneath all
# lines, which is also how a road map is supposed to look at junctions.
STREET_CSS = """
.st{fill:none;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke}
.st-cas{stroke:#fff;stroke-opacity:.5}
.st-art{stroke:#2b333b;stroke-opacity:.9;stroke-width:1.7}
.st-min{stroke:#68727d;stroke-opacity:.6;stroke-width:.9}
.st-cas-art{stroke-width:3.6}.st-cas-min{stroke-width:2.2}
/* display-only neighborhood areas: present for orientation, deliberately quieter
   than the ב/ג/ד outlines, which are the ones that actually gate a listing */
.nbhd{fill:#6b7280;fill-opacity:.045;stroke:#6b7280;stroke-opacity:.45;stroke-width:1;
      stroke-dasharray:3,5;vector-effect:non-scaling-stroke}
.nbhd-l{fill:#6b7280;opacity:.7;paint-order:stroke;stroke:#fff;stroke-width:2.4px}
/* walking contours around the campus gates — the AMBER/RED boundary, made visible */
.ring{fill:none;stroke:#2e7d32;stroke-opacity:.30;stroke-width:1;stroke-dasharray:4,4;
      vector-effect:non-scaling-stroke}
.ring-max{stroke:#e08e0b;stroke-opacity:.55;stroke-width:1.6;stroke-dasharray:none}
.amen-bg{fill:#fff;fill-opacity:.72;stroke:#8a94a6;stroke-opacity:.5;stroke-width:1;
      vector-effect:non-scaling-stroke}
/* the amenities of the listing whose card is open — drawn brighter than the fixed
   pins, with a hairline back to the flat, because they answer "from HERE" */
.myamen-leg{stroke:#7b1fa2;stroke-width:1.4;stroke-opacity:.75;stroke-dasharray:4,3;
      vector-effect:non-scaling-stroke;pointer-events:none}
.myamen-bg{fill:#7b1fa2;fill-opacity:.16;stroke:#7b1fa2;stroke-width:1.6;
      vector-effect:non-scaling-stroke}
/* layer switches — one class on the <svg> hides a whole layer. Each layer has its
   own marker class rather than a :not() chain, so adding a label somewhere else on
   the map can't accidentally join a layer it has nothing to do with. */
.no-streets .st,.no-streets .st-l{display:none!important}
.no-nbhd .nbhd,.no-nbhd .nbhd-l,.no-nbhd .nbhd-abc{display:none!important}
.no-amen .amen{display:none!important}
.no-gates .gate{display:none!important}
.gate-l{fill:#5d4037;paint-order:stroke;stroke:#fff;stroke-width:2.6px}
"""


def _path_d(xy, seg) -> str:
    """One subpath: 'M x,y x,y …' — after an M, further pairs are implicit lineto."""
    return "M" + " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(la, lo) for la, lo in seg))


def streets_svg(xy, bounds, feats=None):
    """(path markup, label list) for every street with geometry inside `bounds`.

    Culling matters: only ~8,900 of the 30,300 street points fall inside the listings
    map's viewport. Labels come back separately so the caller draws them ON TOP of
    everything else."""
    arteries, minors, labels = [], [], []
    for st in (feats or features()).get("streets", []):
        main = st.get("main", True)                    # old files had arteries only
        into = arteries if main else minors
        best_seg, best_len = None, 0.0
        for seg in st.get("segments", []):
            if not any(_in_bounds(la, lo, bounds) for la, lo in seg):
                continue
            into.append(_path_d(xy, seg))
            (ax, ay), (bx, by) = xy(*seg[0]), xy(*seg[-1])
            seglen = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
            if seglen > best_len:
                best_len, best_seg = seglen, seg
        if best_seg and best_len >= _LABEL_MIN_PX:
            # the zoom at which this run is long enough on screen to read. An artery
            # spanning the view shows at 1x; a 30 px side street waits until ~3x.
            minzoom = max(1.0, round(_LABEL_TARGET_PX / best_len, 1))
            labels.append((xy(*best_seg[len(best_seg) // 2]), st["name"], main,
                           minzoom, best_len))
    # cap by run length so the longest (most useful) names survive
    labels.sort(key=lambda item: item[4], reverse=True)
    labels = [item[:4] for item in labels[:_LABEL_CAP]]

    out = []
    for d, cls in ((minors, "st-min"), (arteries, "st-art")):
        if not d:
            continue
        joined = "".join(d)
        casing = "st-cas-art" if cls == "st-art" else "st-cas-min"
        out.append(f'<path class="st st-cas {casing}" d="{joined}"/>')
        out.append(f'<path class="st {cls}" d="{joined}"/>')
    return out, labels


def street_labels_svg(labels):
    """Street names, white-haloed so they read over anything beneath them.

    `data-minzoom` is the zoom at which each becomes legible; the dashboard's
    applyView() shows/hides on it, so zooming in reveals side-street names instead of
    dumping 400 labels on the first view. A static viewer that ignores the attribute
    simply shows them all."""
    out = []
    for (sx, sy), name, main, minzoom in labels:
        fs, fill = (11, "#1c2229") if main else (9, "#3c454e")
        out.append(f'<text class="slabel st-l" data-minzoom="{minzoom}" x="{sx:.0f}" '
                   f'y="{sy:.0f}" font-size="{fs}" fill="{fill}" text-anchor="middle" '
                   f'style="paint-order:stroke;stroke:#fff;stroke-width:2.6px">'
                   f'{html.escape(name)}</text>')
    return out


_MAP_NBHD_PATH = config.ROOT / "map_neighborhoods.json"


def display_neighborhoods_svg(xy, bounds):
    """Faint outlines + names for the WIDER set of neighborhoods, purely for
    orientation — knowing a dot sits in שכונה ו is why you'd want the outline.

    Read from map_neighborhoods.json, NEVER neighborhoods.json: zones.py treats every
    polygon in that second file as an ACCEPTED neighborhood, so mixing display areas in
    would silently let rejected ones pass the ב/ג/ד gate. Drawn subordinate to the
    ב/ג/ד outlines, which stay bolder because those are the ones that matter."""
    try:
        data = json.loads(_MAP_NBHD_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for area in data.get("neighborhoods", []):
        poly = area.get("polygon_latlon") or []
        if len(poly) < 3 or not any(_in_bounds(la, lo, bounds) for la, lo in poly):
            continue
        out.append(f'<polygon class="nbhd" points="{_poly_points(xy, poly)}"/>')
        cla = sum(p[0] for p in poly) / len(poly)
        clo = sum(p[1] for p in poly) / len(poly)
        lx, ly = xy(cla, clo)
        out.append(f'<text class="slabel nbhd-l" data-minzoom="1" x="{lx:.0f}" '
                   f'y="{ly:.0f}" font-size="13" text-anchor="middle">'
                   f'{html.escape(area.get("name", ""))}</text>')
    return out


def landmarks_svg(xy, feats=None):
    """BGU campus (blue) and Soroka (magenta) footprints — the two anchors every
    address in this search is described relative to."""
    out = []
    for lm in (feats or features()).get("landmarks", []):
        poly = lm.get("polygon_latlon") or []
        if not poly:
            continue
        color, label = _LANDMARK_STYLE.get(lm.get("kind"), ("#444", lm.get("name", "")))
        out.append(f'<polygon points="{_poly_points(xy, poly)}" fill="{color}" '
                   f'fill-opacity="0.5" stroke="{color}" stroke-width="2"/>')
        cla = sum(p[0] for p in poly) / len(poly)
        clo = sum(p[1] for p in poly) / len(poly)
        lx, ly = xy(cla, clo)
        out.append(f'<text class="slabel" x="{lx:.0f}" y="{ly:.0f}" font-size="15" '
                   f'fill="#fff" text-anchor="middle" font-weight="bold" '
                   f'style="paint-order:stroke;stroke:{color};stroke-width:3px">'
                   f'{html.escape(label)}</text>')
    return out


# Only pin targets with a HANDFUL of locations. The "bus toward the train station"
# target legitimately matches 428 stops — nearly every stop in the city — so pinning it
# would bury the map under icons while telling you nothing. The 669 stops (2) and the
# gym (1) are distinctive and worth showing.
_MAX_PINS_PER_TARGET = 12


def _amenity_pins():
    """(lat, lon, icon, label) for the FIXED landmarks in amenities.json — the 669 stops
    and the gym — so the map shows the handful of places worth always seeing.

    A target with more candidates than _MAX_PINS_PER_TARGET is skipped, which in
    practice drops the "a stop with a bus to the train station" target: it legitimately
    matches 428 of the city's stops, and pinning any subset of them says nothing.
    The train-bound stop that MATTERS is the one a given listing would actually use, and
    that is drawn per-listing when its card opens (see amenities.locate) rather than
    scattered over the whole map. Missing file -> none."""
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
    bounds = _bounds_of(projection)

    svg = [f'<svg viewBox="0 0 {_W} {_H}" xmlns="http://www.w3.org/2000/svg">']
    svg.append(f'<rect width="{_W}" height="{_H}" fill="#f6f7f9"/>')
    # the street network FIRST, so everything else reads on top of it. Without this the
    # dots floated over an empty polygon and you couldn't tell where anything was.
    feats = features()
    street_lines, street_labels = streets_svg(xy, bounds, feats)
    svg += street_lines
    svg += display_neighborhoods_svg(xy, bounds)   # orientation, beneath ב/ג/ד
    svg += landmarks_svg(xy, feats)
    # the green zone
    svg.append(f'<polygon points="{_poly_points(xy, zone)}" fill="#2e7d32" '
               f'fill-opacity="0.10" stroke="#2e7d32" stroke-width="2"/>')
    # neighborhood outlines + labels
    for letter, poly in nbhds:
        svg.append(f'<polygon class="nbhd-abc" points="{_poly_points(xy, poly)}" '
                   f'fill="none" stroke="#3367d6" stroke-width="1.4" '
                   f'stroke-dasharray="5,4"/>')
        cla = sum(p[0] for p in poly) / len(poly)
        clo = sum(p[1] for p in poly) / len(poly)
        lx, ly = xy(cla, clo)
        svg.append(f'<text class="nbhd-abc" x="{lx:.0f}" y="{ly:.0f}" font-size="18" '
                   f'fill="#3367d6" text-anchor="middle" font-weight="bold">'
                   f'{html.escape(letter)}</text>')
    # Gates. Every AMBER decision on the map keys off the walk to one of these, yet they
    # were a bare ★ glyph with no class — the only thing here that could not be toggled,
    # and the only landmark with no name drawn.
    for la, lo, name in gates:
        gx, gy = xy(la, lo)
        svg.append(f'<text class="slabel gate" data-minzoom="1" x="{gx:.1f}" '
                   f'y="{gy:.1f}" font-size="16" text-anchor="middle" '
                   f'dominant-baseline="central">★<title>{html.escape(name)}</title></text>')
        # the name only once you are zoomed in among them, like a side street
        svg.append(f'<text class="slabel gate gate-l" data-minzoom="2.6" x="{gx:.1f}" '
                   f'y="{gy - 11:.1f}" font-size="10" text-anchor="middle">'
                   f'{html.escape(name)}</text>')
    # amenity pins (the 669 stops, the bus to the train, the gym)
    for la, lo, icon, name in pins:
        px, py = xy(la, lo)
        # 11px was a ~11px tap target and easy to miss entirely; 15 with a soft disc
        # behind it reads as a thing you can point at.
        svg.append(f'<circle class="amen amen-bg" cx="{px:.1f}" cy="{py:.1f}" r="9"/>')
        svg.append(f'<text class="slabel amen" data-minzoom="1" x="{px:.1f}" '
                   f'y="{py:.1f}" font-size="15" text-anchor="middle" '
                   f'dominant-baseline="central">{html.escape(icon)}'
                   f'<title>{html.escape(name)}</title></text>')
    svg += walk_rings_svg(xy)
    svg += street_labels_svg(street_labels)      # names last, so they're never buried
    return "".join(svg), projection


def units_per_metre(xy, lat: float, lon: float) -> float:
    """Canvas units per metre on the ground, measured rather than derived.

    Projects a point 0.01° due east and divides. Measured this way so it works with any
    `xy` callable — area_map's differently-padded projector included — instead of
    depending on the projection params dict. (Only correct because `_projector` already
    applied the cos(lat) squeeze, so x and y share one scale.)"""
    x0, _ = xy(lat, lon)
    x1, _ = xy(lat, lon + 0.01)
    return abs(x1 - x0) / (0.01 * 111320 * math.cos(math.radians(lat)))


def walk_rings_svg(xy):
    """5/10/15/20-minute walking contours around each campus gate.

    MAX_WALK_MINUTES is what separates AMBER from RED for every listing, and until now
    that boundary existed only as a number in a column. Radii come from the same
    calibrated straight-line estimate zones.est_walk_to_gate_min uses (measured against
    OSRM: median error -0.4 min), so the rings sit where the rule actually bites.
    Hidden by default — turned on from the legend's layer toggles."""
    out = ['<g id="rings" style="display:none">']
    metres_per_min = config.WALK_SPEED_M_PER_MIN / config.WALK_DETOUR_FACTOR
    for g in config.GATES.values():
        cx, cy = xy(g["lat"], g["lon"])
        px_per_m = units_per_metre(xy, g["lat"], g["lon"])
        for minutes in (5, 10, 15, config.MAX_WALK_MINUTES):
            r = minutes * metres_per_min * px_per_m
            last = minutes == config.MAX_WALK_MINUTES
            out.append(f'<circle class="ring{" ring-max" if last else ""}" '
                       f'cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}"/>')
    out.append("</g>")
    return out


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
