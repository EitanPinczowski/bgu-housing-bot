"""
One page showing everything the bot knows, for browsing by hand.

    python dashboard.py            # -> data/dashboard.html (open in a browser)
    python dashboard.py --open     # …and open it

Telegram is good at "here is a new flat right now" and bad at "show me every
2-room place under 1500 in ב, sorted by walk". This is the second thing: the
zone map plus a sortable, filterable table of every stored listing — score,
price, rooms, walk, neighborhood, amenities, contact — and the ⭐ shortlist.

Self-contained by design, exactly like map_listings.py / area_map.py: the CSS
and JS are inline, there is no CDN, no tile server and no build step, so it
works offline and keeps working years from now. Everything is read-only; the
bot's data is never modified here.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import html
import json
import sys
import webbrowser

import amenities
import config
import fit
import geocode
import map_listings
import osrm
import storage
import streets

OUT = config.DATA_DIR / "dashboard.html"

_SQL = """
    SELECT l.dedup_key, l.status, l.location_tier, l.score, l.price_per_room,
           l.available_rooms, l.total_roommates, l.address, l.walk_minutes,
           l.lease_start, l.contact, l.source_url, l."group", l.floor, l.furnished,
           l.balcony, l.elevator, l.images, l.amenities, l.first_seen, l.summary,
           l.price_from_comment, l.geocode_source
    FROM listings l
"""


def _rows() -> list:
    # storage._conn() rather than a bare sqlite3.connect: it creates/migrates the
    # schema, so running the dashboard on a fresh install renders an empty table
    # instead of "no such table: listings".
    with storage._conn() as c:
        cur = c.execute(_SQL)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    saved = {r["dedup_key"] for r in storage.saved_listings(limit=500)}
    contacted = storage.contacted_keys()
    stale = storage.stale_keys()
    notes = storage.all_notes()
    manual = storage.all_manual_locations()
    keys = [r["dedup_key"] for r in rows]
    post_text = storage.post_text_for(keys)          # one pass over the archive, not N
    for r in rows:
        key = r["dedup_key"]
        # the effective score is what the bot actually ranks by: quality + group votes
        r["eff_score"] = storage.effective_score(key, r["score"] or 0)
        r["saved"] = key in saved
        r["contacted"] = key in contacted
        r["stale"] = key in stale
        r["broker"] = storage.phone_listing_count(r["contact"])
        r["note"] = notes.get(key, "")
        r["post_text"] = post_text.get(key, "")
        try:
            am = json.loads(r["amenities"] or "{}")
            r["amenity_text"] = " · ".join(amenities.describe(am))
        except Exception:
            am, r["amenity_text"] = {}, ""
        # coordinates for the map dot — geocode is cached, so this is a dict lookup.
        # A hand-placed listing wins, exactly as it does in pipeline._classify; the
        # map must show where the user PUT the flat, not where the geocoder guessed.
        pinned = manual.get(key)
        if pinned:
            r["lat"], r["lon"] = pinned
            r["geocode_source"] = "manual"
        else:
            coords = geocode.geocode_cached(r["address"])
            r["lat"], r["lon"] = (coords if coords else (None, None))
        r["manual_location"] = bool(pinned)
        # where THIS listing's bus stops and gym actually are, so the map can point at
        # them and the card can offer a walking route to each
        r["amenity_points"] = amenities.locate(am, r["lat"], r["lon"])
        # How much to trust that dot. 41% of listings resolve only to a street or
        # neighbourhood centroid, which is why so many sit on top of each other; the
        # map has to say so rather than drawing them all as if they were surveyed.
        r["geo_confidence"] = geocode.confidence(r["geocode_source"])
        r["photos"] = _photo_hashes(r["images"])
        r["breakdown"] = _breakdown_for(r)
        r["street"] = _street_of(r["address"])
    rows.sort(key=lambda r: r["eff_score"], reverse=True)
    return rows


def street_shapes(rows) -> dict:
    """{street: [[lat, lon], …]} for the streets that IMPRECISE listings sit on.

    134 listings can never be placed to a building — the post gave a street, an area, or
    nothing. Drawing each as a dot at a centroid still says "here", and it is the honest
    reason so many dots overlap. With the street's own polyline in hand the page can show
    what we actually know: somewhere along THIS line.

    Only the streets that need it are shipped — 22 of them, 14 KB — not the whole city,
    which `map_listings` already draws as four combined paths."""
    want = {r.get("street") for r in rows
            if r.get("street") and r.get("geo_confidence") in ("street", "area")}
    out = {}
    for name in sorted(filter(None, want)):
        pts = [p for seg in streets.geometry(name) for p in seg]
        if pts:
            out[name] = [[round(p[0], 5), round(p[1], 5)] for p in pts]
    return out


def _street_of(address) -> str:
    """The canonical street an address names, or ''.

    Computed here rather than in the page, because the page can only strip the digits
    off the text and that is not a street: it turned `רחוב בן גוריון, באר שבע` into the
    CITY, and filed `דרך השלום` and `רחוב השלום` as two different places. The worklist
    counts listings per street, so those two mistakes both hide work."""
    text = (address or "").strip()
    if not text:
        return ""
    for cand in geocode._candidate_tokens(text)[:2]:
        real, _how = streets.canonical(cand)
        if real:
            return real
    return ""


def _photo_hashes(images_json) -> list:
    """sha1 of each stored image URL. The page requests /img/<sha1>, never the URL
    itself, so the proxy can only ever serve pictures we already had."""
    try:
        urls = json.loads(images_json or "[]") or []
    except Exception:
        return []
    return [hashlib.sha1(u.encode()).hexdigest() for u in urls
            if isinstance(u, str) and u.startswith(("http://", "https://"))][:6]


def _breakdown_for(r) -> list:
    """[[label, delta], …] — the same per-factor scoring the Telegram alert shows, so
    'why 71 and not 85' is answerable from the dashboard too."""
    try:
        parts = fit.breakdown(
            r["price_per_room"], r["walk_minutes"], r["location_tier"],
            r["available_rooms"], r["total_roommates"],
            bool(r.get("price_from_comment")),
            lease_start=r["lease_start"], furnished=r["furnished"],
            floor=r["floor"], has_elevator=r["elevator"], has_balcony=r["balcony"],
            has_photos=bool(r.get("photos")), broker_listings=r.get("broker") or 0)
        return [[lbl, d] for lbl, d in parts]
    except Exception:
        return []


def rows_for_api() -> list:
    """JSON-safe rows for the live server: drop the columns the page never reads and
    add the derived contact link."""
    out = []
    for r in _rows():
        out.append({k: r.get(k) for k in (
            "dedup_key", "status", "location_tier", "eff_score", "score",
            "price_per_room", "available_rooms", "total_roommates", "address",
            "walk_minutes", "lease_start", "contact", "source_url", "group", "floor",
            "amenity_text", "first_seen", "summary", "saved", "contacted", "stale",
            "broker", "note", "post_text", "lat", "lon", "photos", "breakdown",
            "geocode_source", "geo_confidence", "manual_location",
            "amenity_points", "street")}
            | {"wa": _wa_link(r["contact"])})
    return out


def _wa_link(contact) -> str:
    """Reuse the notifier's rule so the dashboard's contact link behaves like the
    button in Telegram."""
    import notifier
    return notifier._contact_link(contact) or ""


_CSS = """
:root{--fg:#1c2024;--mut:#667;--line:#e3e6ea;--bg:#fff;--card:#fafbfc;--accent:#3367d6}
@media (prefers-color-scheme:dark){
  :root{--fg:#e6e8ea;--mut:#98a2ad;--line:#2a2f36;--bg:#14171a;--card:#1b1f24;--accent:#7aa2f7}}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 system-ui,Segoe UI,Arial;color:var(--fg);background:var(--bg)}
.wrap{max-width:1500px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mut);margin:0 0 12px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 10px;padding:10px;
     background:var(--card);border:1px solid var(--line);border-radius:8px}
.bar label{color:var(--mut);font-size:12px}
input,select,button{font:inherit;padding:5px 8px;border:1px solid var(--line);
     border-radius:6px;background:var(--bg);color:var(--fg)}
button{cursor:pointer}button:hover{border-color:var(--accent);color:var(--accent)}
input[type=search]{min-width:200px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:1100px}
th,td{padding:7px 9px;text-align:right;border-bottom:1px solid var(--line);
      white-space:nowrap;vertical-align:top}
th{position:sticky;top:0;background:var(--card);cursor:pointer;user-select:none;font-size:12px;z-index:2}
th:hover{color:var(--accent)}
td.addr{white-space:normal;min-width:180px}
td.am{white-space:normal;min-width:210px;color:var(--mut);font-size:12px}
tr.row:hover td,tr.row.cursor td{background:var(--card)}
tr.row.cursor td{box-shadow:inset 2px 0 0 var(--accent)}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;font-weight:600}
.GREEN{background:#e5f4e6;color:#1e6b23}.AMBER{background:#fdf1dc;color:#8a5a06}
.RED{background:#fbe6e4;color:#a3271c}.UNKNOWN{background:#eceff2;color:#5b666f}
@media (prefers-color-scheme:dark){
 .GREEN{background:#183a1c;color:#8fd694}.AMBER{background:#3d2f11;color:#e5bb6a}
 .RED{background:#3d1a17;color:#f0a79d}.UNKNOWN{background:#262b31;color:#9fb0bd}}
.thumb{width:54px;height:40px;object-fit:cover;border-radius:4px;cursor:zoom-in;
       background:var(--card);border:1px solid var(--line)}
.new{background:var(--accent);color:#fff;border-radius:99px;padding:0 6px;font-size:10px}
.act button{padding:2px 6px;font-size:13px;line-height:1.2}
.exp td{background:var(--card);white-space:normal}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}
.chip{font-size:11px;padding:1px 7px;border-radius:99px;border:1px solid var(--line)}
.chip.pos{color:#1e6b23}.chip.neg{color:#a3271c}
.raw{white-space:pre-wrap;font-size:12px;color:var(--mut);max-height:220px;overflow:auto;
     border-right:2px solid var(--line);padding-right:8px;margin:6px 0}
.note{width:100%;min-height:46px;font:inherit}
/* The map IS the page now — where a flat is, is the first question. */
.map{margin:10px 0;border:1px solid var(--line);border-radius:8px;overflow:hidden;
     position:relative;height:min(78vh,900px);background:#f6f7f9}
/* touch-action must sit on the SVG, because that is where the pointer handlers are —
   it does NOT inherit from .map. With pan-y here the browser scrolled the page while
   our own handler panned the map, and one finger did both at once: the "glitchy on
   phone" report. `none` hands every gesture to us; the page is scrolled from the
   header, the filter bar and the list, all of which sit outside the map. */
.map,.map svg{touch-action:none}
.map svg{display:block;width:100%;height:100%;cursor:grab}
.map svg.drag{cursor:grabbing}
.maphint{position:absolute;inset-inline-start:8px;bottom:6px;font-size:11px;
         color:var(--mut);background:var(--bg);opacity:.82;padding:1px 7px;border-radius:99px}
.mapbtns{position:absolute;inset-inline-end:8px;top:8px;z-index:15;display:flex;gap:4px}
.mapbtns button{font-size:12px;padding:3px 8px;background:var(--bg);opacity:.92;
       border:1px solid var(--line);border-radius:5px;color:var(--fg);cursor:pointer}
.mapbtns button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
/* legend + layer switches: a corner panel, collapsed by default so it explains the
   map to someone opening it cold without covering it for someone who knows it */
#legend{position:absolute;inset-inline-end:8px;top:40px;z-index:16;width:216px;
        background:var(--bg);border:1px solid var(--line);border-radius:8px;
        box-shadow:0 4px 16px #0002;font-size:11.5px}
#legend>summary{cursor:pointer;padding:5px 9px;font-weight:600;user-select:none;
        list-style:none}
#legend>summary::-webkit-details-marker{display:none}
#legend .body{padding:2px 9px 8px;max-height:min(56vh,420px);overflow:auto}
#legend h4{margin:7px 0 3px;font-size:11px;color:var(--mut);font-weight:600}
#legend .li{display:flex;gap:6px;align-items:center;line-height:1.7}
#legend .sw{flex:0 0 auto;width:13px;height:13px;border-radius:3px;border:1px solid #0002}
#legend .ln{flex:0 0 auto;width:16px;height:0;border-top-style:solid}
#legend label.lay{display:flex;gap:6px;align-items:center;line-height:1.9;cursor:pointer}
#legend label.lay input{margin:0}
/* the list is secondary: collapsed until asked for */
details.list{margin:10px 0;border:1px solid var(--line);border-radius:8px;background:var(--card)}
details.list>summary{cursor:pointer;padding:9px 12px;font-weight:600;user-select:none}
details.list>summary:hover{color:var(--accent)}
details.list .scroll{border:0;border-top:1px solid var(--line);border-radius:0}
/* the apartment card — hover on a pointer device, bottom sheet on touch */
/* Small on purpose: it annotates the map, it shouldn't cover it. A 52x40 thumbnail
   beside the title instead of a full-width banner is most of the height saving. */
#card{position:absolute;z-index:20;width:200px;background:var(--bg);border:1px solid var(--line);
      border-radius:9px;box-shadow:0 6px 20px #0003;padding:7px;display:none;font-size:12px}
#card.on{display:block}
#card .hd{display:flex;gap:6px;align-items:flex-start}
#card img{width:52px;height:40px;object-fit:cover;border-radius:4px;flex:0 0 auto}
#card .ttl{font-weight:700;font-size:12.5px;line-height:1.25}
#card .kv{color:var(--mut);font-size:11.5px;line-height:1.45}
#card .am{color:var(--mut);font-size:11px;display:-webkit-box;-webkit-line-clamp:2;
      -webkit-box-orient:vertical;overflow:hidden}
#card .btns{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
#card .btns a,#card .btns button{font-size:13px;line-height:1;padding:3px 6px;
      border:1px solid var(--line);border-radius:5px;text-decoration:none;
      color:var(--fg);background:var(--bg);cursor:pointer}
#card .x{display:none}
/* Finger-sized targets wherever the pointer is coarse. 13px text with 3px of padding
   is a ~19px target; the guidance is 44px, and a missed tap on 🚶 is indistinguishable
   from a broken button. Not tied to the width media query — a tablet is coarse too. */
@media (pointer:coarse){
  #card .btns a,#card .btns button{min-width:40px;min-height:40px;font-size:18px;
        display:inline-flex;align-items:center;justify-content:center}
  .mapbtns button{min-height:36px;padding:6px 12px}
  #legend label.lay{line-height:2.4}
  #legend>summary{padding:9px}
}
#boxchip{position:absolute;inset-inline-start:8px;top:8px;z-index:15;font-size:12px;
       background:var(--accent);color:#fff;border-radius:99px;padding:2px 6px 2px 10px}
#boxchip button{background:none;border:0;color:#fff;cursor:pointer;font-size:12px;
       padding:0 3px}
#boxrect{fill:#3367d6;fill-opacity:.13;stroke:#3367d6;stroke-width:1.4;
       stroke-dasharray:5,4;vector-effect:non-scaling-stroke;pointer-events:none}
#walknote{position:absolute;inset-inline-start:8px;bottom:28px;z-index:15;font-size:11.5px;
       background:var(--bg);opacity:.94;padding:2px 8px;border-radius:99px;
       border:1px solid var(--line)}
.wr,.wr-cas{fill:none;vector-effect:non-scaling-stroke;pointer-events:none;
       stroke-linecap:round;stroke-linejoin:round}
.wr-cas{stroke:#fff;stroke-width:6}
.wr{stroke:#7b1fa2;stroke-width:3}
/* strokes in SCREEN px, not world units: a dot's radius counter-scales but its outline
   used to be given in viewBox units, so it swelled as you zoomed in */
.dot{cursor:pointer;vector-effect:non-scaling-stroke}
/* the table-hover highlight. It used to set stroke:#111, which overrode an approx dot's
   tier-coloured outline and destroyed the hollow/solid precision signal at exactly the
   moment you were looking closely at one flat. A shadow highlights without repainting. */
.dot.hi{stroke-width:3.2;filter:drop-shadow(0 0 3px rgba(0,0,0,.85))}
/* ⭐ your own shortlist, findable without hovering 310 dots */
.halo{fill:none;stroke:#f5a623;stroke-width:2.4;vector-effect:non-scaling-stroke;
      pointer-events:none}
.hit{fill:transparent;cursor:pointer}
.map svg.placing .hit{pointer-events:none}
.cl{cursor:zoom-in}.cl text{pointer-events:none;user-select:none}
.cl.stack{cursor:cell}                     /* a stack fans out, it doesn't zoom */
.clhit{fill:transparent;cursor:inherit}    /* the badge's real tap target — see mkHit */
/* the street an imprecise listing names: thick, soft, under the dots — it says "somewhere
   along here", which is the whole truth about a listing with no house number */
.sthint{fill:none;stroke:var(--accent);stroke-width:7;stroke-opacity:.32;
        stroke-linecap:round;stroke-linejoin:round;pointer-events:none;
        vector-effect:non-scaling-stroke}
.map svg.placing .clhit{pointer-events:none}
.spider-leg{stroke:#8a94a6;stroke-width:1;stroke-opacity:.65;
      vector-effect:non-scaling-stroke;pointer-events:none}
.spider-hub{fill:#8a94a6;pointer-events:none}
/* place-mode: an armed, obvious state, because the next tap moves a flat */
.map svg.placing{cursor:crosshair}
.map svg.placing .dot,.map svg.placing .cl{pointer-events:none}
#placebar{position:absolute;inset-inline:8px;top:8px;z-index:18;font-size:12.5px;
      background:var(--accent);color:#fff;border-radius:8px;padding:7px 10px;
      display:flex;gap:8px;align-items:center;flex-wrap:wrap;box-shadow:0 3px 12px #0003}
#placebar button{background:#fff3;border:1px solid #fff6;color:#fff;font-size:12px}
#placebar .live{color:#fff}
/* the guided flow rides in the same slot as place-mode — the two are never on together
   (pinmanual hands over to setPlacing), so one position is enough */
#pinflow{position:absolute;inset-inline:8px;top:8px;z-index:18;font-size:12.5px;
      background:#5b3fa8;color:#fff;border-radius:8px;padding:7px 10px;
      display:flex;gap:8px;align-items:center;flex-wrap:wrap;box-shadow:0 3px 12px #0003}
#pinflow button{background:#fff3;border:1px solid #fff6;color:#fff;font-size:12px}
#pinflow button[disabled]{opacity:.45;cursor:default}
#pinflow .live{color:#fff}
/* the candidate is deliberately NOT a dot: a dashed pulsing ring reads as "proposed",
   so an unconfirmed govmap guess can't be mistaken for a saved location */
.pincand{fill:none;stroke:#5b3fa8;stroke-width:2.5;stroke-dasharray:4,3;
      vector-effect:non-scaling-stroke;pointer-events:none}
.pincand.halo{stroke:#fff;stroke-width:5;stroke-dasharray:none;stroke-opacity:.85}
@media (prefers-reduced-motion:no-preference){
  .pincand{animation:pinpulse 1.4s ease-in-out infinite}
}
@keyframes pinpulse{50%{stroke-opacity:.35}}
.muted{color:var(--mut)}a{color:var(--accent)}
.count{color:var(--mut);font-size:12px;margin:6px 2px}
/* a count that is also the way in — looks like the text it sits in, not a chrome button */
.linkish{background:none;border:0;padding:0;font:inherit;color:var(--accent);
         cursor:pointer;text-decoration:underline}
/* the flats with no coordinate. A sheet over the map rather than a panel beside it,
   because the phone is the case that matters and there is no room beside anything. */
#noloc{position:absolute;inset-inline:8px;bottom:8px;max-height:62%;z-index:19;
       background:var(--card);border:1px solid var(--line);border-radius:10px;
       box-shadow:0 6px 22px #0004;display:flex;flex-direction:column;overflow:hidden}
.nlhead{display:flex;gap:8px;align-items:center;padding:8px 10px;
        border-bottom:1px solid var(--line);font-size:13px}
.nlhead b{flex:1}
.nlbody{overflow-y:auto;-webkit-overflow-scrolling:touch}
.nlrow{display:flex;gap:8px;width:100%;text-align:start;background:none;border:0;
       border-bottom:1px solid var(--line);padding:9px 10px;font:inherit;
       color:var(--fg);cursor:pointer;align-items:baseline}
.nlrow:hover{background:var(--bg)}
.nladdr{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (pointer:coarse){ .nlrow{padding:13px 10px} }
#worklist{color:var(--mut);font-size:12px;margin:2px 2px 8px;
          display:flex;flex-wrap:wrap;gap:6px;align-items:center}
#worklist button{font-size:12px;padding:2px 8px}
.panel{border:1px solid var(--line);border-radius:8px;padding:10px;margin:10px 0;
       background:var(--card);display:none}
.panel.on{display:block}
.panel table{min-width:0}
#lb{position:fixed;inset:0;background:#000c;display:none;align-items:center;
    justify-content:center;z-index:50}
#lb.on{display:flex}#lb img{max-width:94vw;max-height:90vh;border-radius:6px}
.live{font-size:11px;color:var(--mut)}
.snap{background:#fdf1dc;color:#8a5a06;border:1px solid #e6c58a;border-radius:8px;
      padding:7px 10px;margin:0 0 10px;font-size:13px}
@media (prefers-color-scheme:dark){
  .snap{background:#3d2f11;color:#e5bb6a;border-color:#6b5417}}
/* --- phone: the table becomes cards (it is 1100px wide otherwise) --- */
@media (max-width:700px){
  .wrap{padding:10px}
  .scroll{border:0;overflow:visible}
  table,tbody,tr,td{display:block;width:100%}
  thead{display:none}
  table{min-width:0}
  tr.row{border:1px solid var(--line);border-radius:8px;margin-bottom:8px;padding:8px;
         background:var(--card)}
  tr.row td{border:0;padding:2px 0;white-space:normal;text-align:right}
  td[data-lbl]:before{content:attr(data-lbl) ": ";color:var(--mut);font-size:11px}
  td.hide-sm{display:none}
  tr.exp{border:1px solid var(--line);border-radius:8px;margin-bottom:8px}
  .thumb{width:100%;height:150px}
  .map{height:62vh}                       /* leave room for the sheet */
  #legend{width:min(74vw,206px);top:38px;font-size:11px}
  /* the card becomes a bottom sheet: a 265px popover is unusable on a phone */
  #card{position:fixed;inset:auto 0 0 0;width:auto;border-radius:14px 14px 0 0;
        max-height:62vh;overflow:auto;box-shadow:0 -8px 26px #0004;padding:12px}
  #card img{height:150px}
  #card .x{display:block;position:absolute;inset-inline-end:10px;top:8px;font-size:18px;
           line-height:1;background:none;border:0;color:var(--mut);cursor:pointer}
}
"""

_JS = r"""
const $ = id => document.getElementById(id);
const TOKEN = new URLSearchParams(location.search).get('token') || '';
/* A snapshot is a file someone was SENT: no server behind it. Write controls are
   removed rather than disabled — a button that alerts "unavailable" is worse than
   no button — while contacts, WhatsApp links and the map stay fully usable. */
const SNAP = !!window.__SNAPSHOT__;
const fmt = v => (v === null || v === undefined || v === '') ? '' : v;
let DATA = window.__BOOT__ || [];
let sortCol = 'eff_score', sortDir = -1, cursor = 0;
const compare = new Set();

/* "new since last visit" — localStorage only, no server state */
const LAST = localStorage.getItem('bgu_last_visit') || '';
localStorage.setItem('bgu_last_visit',
  new Date().toISOString().slice(0, 19).replace('T', ' '));
const isNew = r => LAST && r.first_seen && r.first_seen > LAST;

/* the SAME projection the backdrop was drawn with (mirrors map_listings.xy_from) */
const P = window.__PROJ__ || null;
const project = (la, lo) => [P.pad + (lo - P.min_lon) * P.kx * P.scale,
                             P.pad + (P.max_lat - la) * P.scale];
// the exact inverse of project(), for turning a tap on the map back into lat/lon
const unproject = (x, y) => [P.max_lat - (y - P.pad) / P.scale,
                             (x - P.pad) / (P.kx * P.scale) + P.min_lon];
const TIER_COLOR = {GREEN: '#2e7d32', AMBER: '#e08e0b', RED: '#c0392b', UNKNOWN: '#7f8c8d'};
function scoreColor(s){
  const t = Math.max(0, Math.min(100, s || 0)) / 100;
  return 'hsl(' + Math.round(t * 130) + ' 65% ' + (42 + (1 - t) * 8) + '%)';
}
function esc(s){ const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

function passes(r){
  const text = $('q').value.trim().toLowerCase();
  if (text){
    // r.street is the CANONICAL name, which an address often doesn't spell out
    // ("רגר 5" -> "שדרות יצחק רגר"). Searching it is what makes the worklist's
    // one-click filter land on the whole street rather than nothing.
    const hay = [r.address, r.street, r.contact, r.summary, r.amenity_text, r.note,
                 r.post_text].join(' ').toLowerCase();
    if (!hay.includes(text)) return false;
  }
  if ($('st').value && r.status !== $('st').value) return false;
  if ($('tier').value && r.location_tier !== $('tier').value) return false;
  if ($('maxp').value && (r.price_per_room === null || r.price_per_room > +$('maxp').value)) return false;
  if ($('minr').value && (r.available_rooms === null || r.available_rooms < +$('minr').value)) return false;
  if ($('stale').checked && r.stale) return false;
  if ($('nobroker').checked && r.broker >= 4) return false;
  if ($('saved').checked && !r.saved) return false;
  if ($('onlynew').checked && !isNew(r)) return false;
  if ($('approx').checked && !isApprox(r)) return false;
  if (!inBox(r)) return false;
  return true;
}

function visible(){
  const rows = DATA.filter(passes);
  const k = sortCol;
  rows.sort((a, b) => {
    let x = a[k], y = b[k];
    if (typeof x === 'string' || typeof y === 'string')
      return String(x || '').localeCompare(String(y || ''), 'he') * sortDir;
    if (x === null || x === undefined) x = -Infinity;
    if (y === null || y === undefined) y = -Infinity;
    return (x - y) * sortDir;
  });
  return rows;
}

function rowHtml(r){
  const flags = [r.saved ? '⭐' : '', r.contacted ? '📵' : '',
                 r.stale ? '🕒' : '', r.broker >= 4 ? '⚠️' + r.broker : '',
                 isNew(r) ? '<span class="new">חדש</span>' : ''
                ].filter(Boolean).join(' ');
  // photos come from the server's /img proxy, so a snapshot has none to show — omit
  // the tag rather than ship a request that can only fail behind an onerror
  const thumb = (!SNAP && r.photos && r.photos.length)
    ? '<img class="thumb" loading="lazy" src="/img/' + r.photos[0] + '?token=' + TOKEN +
      '" onerror="this.style.visibility=\'hidden\'">' : '';
  const addr = r.source_url
    ? '<a href="' + esc(r.source_url) + '" target="_blank" rel="noreferrer">' + esc(r.address || '—') + '</a>'
    : esc(r.address || '—');
  const contact = r.wa
    ? '<a href="' + esc(r.wa) + '" target="_blank" rel="noreferrer">' + esc(r.contact || '') + '</a>'
    : esc(r.contact || '');
  return '<tr class="row" data-key="' + esc(r.dedup_key) + '">' +
    '<td class="hide-sm">' + thumb + '</td>' +
    '<td data-lbl="ציון"><b>' + fmt(r.eff_score) + '</b></td>' +
    '<td data-lbl="סטטוס" class="hide-sm">' + esc(r.status || '') + '</td>' +
    '<td data-lbl="אזור"><span class="pill ' + esc(r.location_tier || 'UNKNOWN') + '">' +
      esc(r.location_tier || '?') + '</span></td>' +
    '<td data-lbl="מחיר">' + fmt(r.price_per_room) + '</td>' +
    '<td data-lbl="פנויים">' + fmt(r.available_rooms) + '</td>' +
    '<td data-lbl="שותפים" class="hide-sm">' + fmt(r.total_roommates) + '</td>' +
    '<td data-lbl="כתובת" class="addr">' + addr +
      ' <span class="muted">' + flags + '</span></td>' +
    '<td data-lbl="הליכה">' +
      (r.walk_minutes === null ? '' : Math.round(r.walk_minutes)) + '</td>' +
    '<td data-lbl="כניסה" class="hide-sm">' + esc(r.lease_start || '') + '</td>' +
    '<td data-lbl="תחבורה" class="am">' + esc(r.amenity_text || '') + '</td>' +
    '<td data-lbl="קשר">' + contact + '</td>' +
    '<td class="act">' +
      (SNAP ? '' : '<button data-act="save">⭐</button>' +
                   '<button data-act="dismiss">🗑</button>' +
                   '<button data-act="contacted">📵</button>') +
      '<button data-act="exp">▾</button>' +
      '<input type="checkbox" data-act="cmp" title="השווה"></td></tr>';
}

function expandHtml(r){
  const chips = (r.breakdown || []).map(p =>
    '<span class="chip ' + (p[1] >= 0 ? 'pos' : 'neg') + '">' + esc(p[0]) + ' ' +
    (p[1] > 0 ? '+' : '') + p[1] + '</span>').join('');
  const gallery = (SNAP ? [] : (r.photos || [])).map(h =>
    '<img class="thumb" loading="lazy" src="/img/' + h + '?token=' + TOKEN +
    '" onerror="this.style.display=\'none\'">').join(' ');
  return '<tr class="exp" data-key="' + esc(r.dedup_key) + '"><td colspan="13">' +
    '<div class="chips">' + chips + '</div>' +
    (gallery ? '<div style="margin:6px 0">' + gallery + '</div>' : '') +
    (r.post_text ? '<div class="raw">' + esc(r.post_text) + '</div>'
                 : '<div class="muted">הפוסט המקורי לא נשמר</div>') +
    (SNAP
      ? (r.note ? '<div class="raw">📝 ' + esc(r.note) + '</div>' : '')
      : '<textarea class="note">' + esc(r.note || '') + '</textarea>' +
        '<div><button data-act="savenote">שמור הערה</button> ' +
        '<span class="live" data-note-status></span></div>') + '</td></tr>';
}

/* ---------- zoom & pan: animate the viewBox, no library ----------
   The map is the main view now, so it has to zoom: אברהם אבינו alone holds 21
   listings, an unreadable blob at full extent. Strokes are non-scaling (CSS), and
   dots and labels are counter-scaled here so only the geography grows. */
const WORLD = {x: 0, y: 0, w: (P ? P.w : 1000), h: (P ? P.h : 820)};
let view = {...WORLD};
const zoomFactor = () => WORLD.w / view.w;
let lastRows = [], lastZoom = 0;

/* The street/neighbourhood/amenity labels are STATIC backdrop markup — build_base_svg
   emits them once and nothing ever rebuilds them — so the NodeList is cached instead of
   re-queried. It is also the largest node set on the page (264 measured). */
let slabelCache = null;
const slabels = () => (slabelCache = slabelCache ||
                       document.querySelectorAll('.map svg .slabel'));

/* PANNING MUST NOT DO ZOOM WORK.
   Everything below that divides by `k` — dot radii, label font sizes, which labels are
   legible, the pin candidate — is a function of the ZOOM FACTOR ALONE. A pan changes
   neither. This used to run on every pointermove: querySelectorAll('.dot') plus
   querySelectorAll('.slabel') and a write to all 419 of them, per frame, to produce
   values identical to the previous frame's. The `lastZoom` guard already existed but
   gated only drawDots. */
function rescaleForZoom(svg, k){
  svg.querySelectorAll('.dot').forEach(d => d.setAttribute('r', (5 / k).toFixed(2)));
  // halos are sized in screen px and only ever created by drawDots, which runs below
  slabels().forEach(t => {
    if (!t.dataset.fs) t.dataset.fs = t.getAttribute('font-size') || '11';
    t.setAttribute('font-size', (+t.dataset.fs / k).toFixed(2));
    // reveal names as they become legible: arteries at 1x, side streets once you're
    // zoomed in among them (see map_listings.street_labels_svg)
    const mz = +t.dataset.minzoom || 1;
    t.style.display = (k >= mz) ? '' : 'none';
  });
  // clusters are a function of zoom too, so they are rebuilt on the same signal
  drawDots(lastRows);
  // AFTER drawDots, which re-appends #dots: the proposed point has to stay on top of
  // the dots it is about, and counter-scale so it stays a ring rather than a blob
  const cand = document.getElementById('pincand');
  if (cand){
    cand.querySelectorAll('circle').forEach(c => c.setAttribute('r', (12 / k).toFixed(2)));
    svg.appendChild(cand);
  }
}

function applyView(){
  const svg = document.querySelector('.map svg');
  if (!svg) return;
  // keep the window inside the world so the map can't be lost off-screen
  view.w = Math.min(WORLD.w, Math.max(WORLD.w / 12, view.w));
  view.h = view.w * (WORLD.h / WORLD.w);
  view.x = Math.max(0, Math.min(WORLD.w - view.w, view.x));
  view.y = Math.max(0, Math.min(WORLD.h - view.h, view.y));
  svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
  const k = zoomFactor();
  if (Math.abs(Math.log(k / (lastZoom || k))) > 0.01 || !lastZoom){
    lastZoom = k;
    rescaleForZoom(svg, k);
  } else if (culls()){
    drawDots(lastRows);            // panned into new territory — see drawDots' culling
  }
}

function zoomAt(px, py, factor){
  const svg = document.querySelector('.map svg');
  const box = svg.getBoundingClientRect();
  const fx = (px - box.left) / box.width, fy = (py - box.top) / box.height;
  const ax = view.x + fx * view.w, ay = view.y + fy * view.h;   // anchor stays put
  view.w = view.w / factor;
  view.h = view.w * (WORLD.h / WORLD.w);
  view.x = ax - fx * view.w;
  view.y = ay - fy * view.h;
  applyView();
}

function initMapGestures(){
  const svg = document.querySelector('.map svg');
  if (!svg) return;
  /* Zoom on Ctrl/⌘+wheel only. A plain wheel must scroll the PAGE: the map is 78vh
     tall, so grabbing every scroll event means you can't reach the list below it —
     and a casual scroll silently zooms the map, which is exactly what happened the
     first time this was tried.
     TOUCH is different: one finger pans the map and two pinch to zoom, the normal
     maps convention. The page is scrolled from outside the map. */
  svg.addEventListener('wheel', ev => {
    if (!ev.ctrlKey && !ev.metaKey) return;          // let the page scroll
    ev.preventDefault();
    zoomAt(ev.clientX, ev.clientY, ev.deltaY < 0 ? 1.18 : 1 / 1.18);
  }, {passive: false});

  let drag = null, pinch = null, sel = null;   // sel: the box-select rubber band
  const pts = new Map();
  const toWorld = (cx, cy) => {
    const b = svg.getBoundingClientRect();
    return [view.x + ((cx - b.left) / b.width) * view.w,
            view.y + ((cy - b.top) / b.height) * view.h];
  };
  svg.addEventListener('pointerdown', ev => {
    pts.set(ev.pointerId, ev);
    if (pts.size === 1 && (ev.shiftKey || boxPending)){
      ev.preventDefault();
      const [wx, wy] = toWorld(ev.clientX, ev.clientY);
      sel = {x0: wx, y0: wy};
      const r = el('rect');
      r.id = 'boxrect';
      svg.appendChild(r);
      svg.setPointerCapture(ev.pointerId);
      return;
    }
    // `.cl` belongs here as much as `.dot`. Pressing a badge used to start a pan AND
    // call setPointerCapture, which retargets the following `click` to the <svg> — so
    // the badge handler's closest('.cl') found nothing and clusters never opened.
    if (pts.size === 1 && !ev.target.closest('.dot, .cl')){
      drag = {x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y};
      svg.classList.add('drag');
      svg.setPointerCapture(ev.pointerId);
    } else if (pts.size === 2){
      drag = null;
      const [a, b] = [...pts.values()];
      pinch = {d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY),
               mx: (a.clientX + b.clientX) / 2, my: (a.clientY + b.clientY) / 2};
    }
  });
  svg.addEventListener('pointermove', ev => {
    if (pts.has(ev.pointerId)) pts.set(ev.pointerId, ev);
    if (sel){
      const [wx, wy] = toWorld(ev.clientX, ev.clientY);
      sel.x1 = wx; sel.y1 = wy;
      const r = document.getElementById('boxrect');
      if (r){
        r.setAttribute('x', Math.min(sel.x0, wx));
        r.setAttribute('y', Math.min(sel.y0, wy));
        r.setAttribute('width', Math.abs(wx - sel.x0));
        r.setAttribute('height', Math.abs(wy - sel.y0));
      }
      return;
    }
    if (pinch && pts.size === 2){
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      const mx = (a.clientX + b.clientX) / 2, my = (a.clientY + b.clientY) / 2;
      const box = svg.getBoundingClientRect();
      // two fingers also PAN — with one finger reserved for page scroll on touch,
      // this is the only way to move the map on a phone
      view.x -= (mx - pinch.mx) * (view.w / box.width);
      view.y -= (my - pinch.my) * (view.h / box.height);
      pinch.mx = mx; pinch.my = my;
      if (pinch.d > 0 && Math.abs(d - pinch.d) > 1){
        zoomAt(mx, my, d / pinch.d);
        pinch.d = d;
      } else {
        applyView();
      }
      return;
    }
    if (!drag) return;
    const box = svg.getBoundingClientRect();
    view.x = drag.vx - (ev.clientX - drag.x) * (view.w / box.width);
    view.y = drag.vy - (ev.clientY - drag.y) * (view.h / box.height);
    applyView();
  });
  const release = ev => {
    pts.delete(ev.pointerId);
    if (sel){
      const b = sel; sel = null; boxPending = false;
      // a tap, not a drag: treat it as "cancel", not as an empty selection
      const tiny = !b.x1 || (Math.abs(b.x1 - b.x0) < 4 && Math.abs(b.y1 - b.y0) < 4);
      setBox(tiny ? null : {x0: Math.min(b.x0, b.x1), x1: Math.max(b.x0, b.x1),
                            y0: Math.min(b.y0, b.y1), y1: Math.max(b.y0, b.y1)});
    }
    if (pts.size < 2) pinch = null;
    if (pts.size === 0){ drag = null; svg.classList.remove('drag'); }
  };
  svg.addEventListener('pointerup', release);
  svg.addEventListener('pointercancel', release);
  svg.addEventListener('dblclick', () => { view = {...WORLD}; applyView(); });
  $('reset').addEventListener('click', () => {
    view = {...WORLD};
    if (boxSel) setBox(null); else applyView();
  });
  $('fitbtn').addEventListener('click', () => fitTo(visible()));
  $('boxbtn').addEventListener('click', () => {
    boxPending = !boxPending;
    $('boxbtn').classList.toggle('on', boxPending);
  });
  $('boxclear').addEventListener('click', () => setBox(null));
  /* A badge either zooms or fans out, depending on which kind of pile it is.
     Zooming a stack would do nothing at all — that is the "clusters don't open up"
     report — so those fan instead. Clicking empty map closes an open fan. */
  svg.addEventListener('click', ev => {
    const grp = ev.target.closest('.cl');
    if (grp && grp.__rows){
      // Zooming only helps if the dots are actually apart. A stack shares one exact
      // coordinate, and a group already tighter than a cluster cell at MAX ZOOM cannot
      // be separated either — fitTo would clamp and the badge would sit there being
      // clicked forever with nothing happening. Both of those fan instead.
      if (grp.__sameSpot || grp.__tight) openFan({rows: grp.__rows, x: grp.__x, y: grp.__y});
      else fitTo(grp.__rows);
      return;
    }
    if (manualFans.length && !ev.target.closest('.dot')) closeFans();
  });
}

/* ---------- layer switches ----------
   Each checkbox toggles one class on the <svg>; the CSS (map_listings.STREET_CSS)
   does the hiding. Cheaper than walking thousands of nodes, and it survives
   drawDots() rebuilding the dot layer. Choices persist per browser. */
const LAYER_KEY = 'bgu.layers';
function applyLayers(){
  const svg = document.querySelector('.map svg');
  if (!svg) return;
  const state = {};
  document.querySelectorAll('#legend input[data-layer]').forEach(cb => {
    state[cb.dataset.layer] = cb.checked;
    if (cb.dataset.layer === 'autofit'){
      /* not a layer — a behaviour. It lives here because this is where the map's
         own switches are, but it has nothing to hide. */
    } else if (cb.dataset.layer === 'rings'){
      const g = svg.querySelector('#rings');
      if (g) g.style.display = cb.checked ? '' : 'none';
    } else {
      svg.classList.toggle('no-' + cb.dataset.layer, !cb.checked);
    }
  });
  try { localStorage.setItem(LAYER_KEY, JSON.stringify(state)); } catch (e) {}
  applyView();                            // street labels also obey zoom
}

function initLayers(){
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(LAYER_KEY) || '{}'); } catch (e) {}
  document.querySelectorAll('#legend input[data-layer]').forEach(cb => {
    if (cb.dataset.layer in saved) cb.checked = !!saved[cb.dataset.layer];
    cb.addEventListener('change', applyLayers);
  });
  applyLayers();
}

/* ---------- clustering ----------
   אברהם אבינו alone holds 21 listings on one street: at full extent they are a
   single blob that reads as one flat. Dots closer together than CLUSTER_PX collapse
   into a badge with the count, and split apart again as you zoom in — so the map
   never lies about how much is there. Clustering runs over the FILTERED rows, so
   the badges always add up to the counter above the map. */
const CLUSTER_PX = 19;
// Invisible tap target around each dot, in REAL screen pixels. Sizing it in viewBox
// units looked right until measured: the map renders ~0.36 px per unit in a narrow
// pane, so an 11-unit radius came out as an 8px target. unitsPerPx converts.
const HIT_RADIUS_PX = window.matchMedia('(pointer:coarse)').matches ? 22 : 14;
const el = t => document.createElementNS('http://www.w3.org/2000/svg', t);

function clusterOf(rows){
  const svg = document.querySelector('.map svg');
  const box = svg.getBoundingClientRect();
  const cell = CLUSTER_PX * (view.w / (box.width || 1));
  const bins = new Map();
  for (const r of rows){
    if (r.lat === null || r.lat === undefined) continue;
    const xy = project(r.lat, r.lon);
    const id = Math.floor(xy[0] / cell) + ':' + Math.floor(xy[1] / cell);
    let b = bins.get(id);
    if (!b){ b = {x: 0, y: 0, rows: []}; bins.set(id, b); }
    b.x += xy[0]; b.y += xy[1]; b.rows.push(r);
  }
  for (const b of bins.values()){
    b.x /= b.rows.length; b.y /= b.rows.length;
    /* Does this group sit on ONE coordinate, or merely near each other? 138 of 338
       listings geocode to a street or neighbourhood centroid, so 19 flats can share a
       single point — and no zoom level can ever separate those. The two cases need
       different answers: zoom in on a spread, fan out a stack. */
    b.sameSpot = b.rows.every(r => Math.abs(r.lat - b.rows[0].lat) < 1e-5 &&
                                   Math.abs(r.lon - b.rows[0].lon) < 1e-5);
  }
  return [...bins.values()];
}

/* Stacks that are fanned open: [{rows, x, y}] in world coords, so they survive the
   redraw that every zoom triggers. A LIST, not one — past AUTO_FAN_ZOOM every stack in
   view opens by itself, and there are 38 of them. */
let manualFans = [];

/* Zoom cannot separate dots that share a coordinate, and 38 of the 48 clusters at full
   zoom are exactly that: 184 listings whose post gave no house number, plus flats that
   really are in the same building. Once you are zoomed in far enough to be looking at
   one neighbourhood, opening them automatically is the only way the map stops being a
   wall of badges. Leader lines keep it honest — they show the dots share one origin. */
const AUTO_FAN_ZOOM = 6;

function isApprox(r){
  return r.geo_confidence === 'street' || r.geo_confidence === 'area' ||
         r.geo_confidence === 'none' || !r.geo_confidence;
}

/* A dot is 5/zoom units — about 4px on screen, which is not a target anyone can hit,
   least of all on a phone. Each one gets an invisible companion circle carrying the
   same key, so the thing you aim at is ~22px while the thing you SEE stays small. */
function mkHit(r, x, y, unitsPerPx){
  const h = el('circle');
  h.setAttribute('cx', x.toFixed(1));
  h.setAttribute('cy', y.toFixed(1));
  h.setAttribute('r', (HIT_RADIUS_PX * unitsPerPx).toFixed(2));
  h.setAttribute('class', 'hit');
  h.dataset.key = r.dedup_key;
  return h;
}

/* TWO extra channels on the dot, and only two.
   The payload already carries saved, contacted, broker, stale, first_seen, note and
   photos, and none of them changed the dot — you could not find your own ⭐ among 310
   without hovering each one. But a dot is ~4px on a phone and already encodes tier (or
   score) as fill and precision as hollow/solid, so it can carry two more things, not
   seven. Broker, stale, notes and photos each have a filter and a line in the card;
   they stay there. */
const DOT_FADED = '0.4';        // contacted: recede, don't disappear
/* SCREEN pixels, not viewBox units — the same trap mkHit records. Sized in world units
   the ring measured 7px around a 4px dot at 375px wide (1.5px of gold a side) while
   looking fine on a desktop, because the map renders ~0.36px per unit in a narrow pane.
   unitsPerPx converts, so the ring is the same size on every screen. */
const HALO_PX = 7, CL_HALO_PX = 10;

function mkHalo(r, x, y, unitsPerPx){
  if (!r.saved) return null;
  const h = el('circle');
  h.setAttribute('cx', x.toFixed(1));
  h.setAttribute('cy', y.toFixed(1));
  h.setAttribute('r', (HALO_PX * unitsPerPx).toFixed(2));
  h.setAttribute('class', 'halo');
  if (r.contacted) h.setAttribute('opacity', DOT_FADED);
  return h;
}

function mkDot(r, x, y, k, colour){
  /* Hollow = we do not actually know where this flat is, only its street or
     neighbourhood. Solid = a house number. Same tier colour either way, so the
     precision reads as a separate axis rather than competing with the zone. */
  const c = el('circle');
  const approx = isApprox(r);
  c.setAttribute('cx', x.toFixed(1));
  c.setAttribute('cy', y.toFixed(1));
  c.setAttribute('r', (5 / k).toFixed(2));         // constant on screen under zoom
  c.setAttribute('class', 'dot' + (approx ? ' approx' : ''));
  c.setAttribute('fill', approx ? 'var(--bg)' : colour);
  c.setAttribute('fill-opacity', approx ? '0.95' : '0.85');
  c.setAttribute('stroke', approx ? colour : '#fff');
  c.setAttribute('stroke-width', approx ? '2.2' : '1');   // px — see .dot in the CSS
  if (r.contacted) c.setAttribute('opacity', DOT_FADED);
  c.dataset.key = r.dedup_key;
  const t = el('title');
  t.textContent = (r.address || '—') + ' | ' + r.location_tier + ' | ⭐' + r.eff_score +
                  (r.price_per_room ? ' | ' + r.price_per_room + '₪' : '') +
                  (approx ? ' | מיקום משוער' : '') +
                  (r.saved ? ' | ⭐ שמור' : '') + (r.contacted ? ' | 📵 יצרתי קשר' : '');
  c.appendChild(t);
  return c;
}

/* the halo sits UNDER its dot, so append them as a pair wherever a dot is drawn */
function addDot(parent, r, x, y, k, colour, unitsPerPx){
  parent.appendChild(mkHit(r, x, y, unitsPerPx));
  const halo = mkHalo(r, x, y, unitsPerPx);
  if (halo) parent.appendChild(halo);
  parent.appendChild(mkDot(r, x, y, k, colour));
}

/* ---------- draw only what can be seen ----------
   drawDots used to build every group in the world no matter where the view was: at 12x
   that is a few hundred nodes for the handful on screen, rebuilt on every filter
   keystroke, vote, poll tick and zoom step.

   The box is deliberately larger than the viewport, so an ordinary pan reveals dots that
   are already drawn and costs nothing; a redraw happens only once the window leaves the
   box it was drawn for (`culls()`). At 1x the box covers the world, so nothing changes
   there — this only bites when zoomed in, which is exactly when it was wasteful. */
const CULL_MARGIN = 0.6;              // extra screens on each side
let drawnBox = null;                  // the box #dots was last built for

function cullBox(){
  const mx = view.w * CULL_MARGIN, my = view.h * CULL_MARGIN;
  return {x0: view.x - mx, y0: view.y - my,
          x1: view.x + view.w + mx, y1: view.y + view.h + my};
}

function culls(){
  // has the visible window left the box the dots were drawn for? If so what is on
  // screen may be incomplete and has to be rebuilt.
  if (!drawnBox) return false;
  return view.x < drawnBox.x0 || view.y < drawnBox.y0 ||
         view.x + view.w > drawnBox.x1 || view.y + view.h > drawnBox.y1;
}

function drawDots(rows){
  const svg = document.querySelector('.map svg');
  if (!svg || !P) return;
  lastRows = rows;
  const old = document.getElementById('dots');
  if (old) old.remove();
  const g = el('g');
  g.id = 'dots';
  const byScore = $('bycolor').value === 'score';
  const k = zoomFactor();
  const svgBox = svg.getBoundingClientRect();
  const unitsPerPx = view.w / (svgBox.width || 1);
  const fill = r => byScore ? scoreColor(r.eff_score)
                            : (TIER_COLOR[r.location_tier] || TIER_COLOR.UNKNOWN);
  let n = 0, clusters = 0;
  const allGroups = clusterOf(rows);
  const box = drawnBox = cullBox();
  // count from EVERY group, render only the ones in the box: the counter reports how
  // many listings are on the map, not how many happen to be under the window
  for (const b of allGroups){
    n += b.rows.length;
    if (b.rows.length > 1) clusters++;
  }
  const groups = allGroups.filter(b => b.x >= box.x0 && b.x <= box.x1 &&
                                       b.y >= box.y0 && b.y <= box.y1);
  const fans = activeFans(groups);
  const fanned = new Set();
  fans.forEach(f => f.rows.forEach(r => fanned.add(r.dedup_key)));
  // a group tighter than one cluster cell at MAX zoom can never be split by zooming
  const tightSpan = CLUSTER_PX * ((WORLD.w / 12) / (svgBox.width || 1));
  for (const b of groups){
    if (b.rows.every(r => fanned.has(r.dedup_key))) continue;             // drawn below
    if (b.rows.length === 1){
      addDot(g, b.rows[0], b.x, b.y, k, fill(b.rows[0]), unitsPerPx);
      continue;
    }
    // one badge for the group, coloured by its best listing so a cluster hiding a
    // strong match doesn't read as background noise
    const best = b.rows.reduce((a, r) => (r.eff_score > a.eff_score ? r : a), b.rows[0]);
    const grp = el('g');
    grp.setAttribute('class', 'cl' + (b.sameSpot ? ' stack' : ''));
    // the rows themselves, not a joined list of keys — a dedup_key is phone|address
    // and an address may contain a comma, which a split(',') would tear in half
    grp.__rows = b.rows;
    grp.__sameSpot = b.sameSpot;
    grp.__x = b.x; grp.__y = b.y;
    grp.__tight = spanOf(b.rows) <= tightSpan;
    const c = el('circle');
    c.setAttribute('cx', b.x.toFixed(1));
    c.setAttribute('cy', b.y.toFixed(1));
    c.setAttribute('r', (10 / k).toFixed(2));
    c.setAttribute('fill', fill(best));
    c.setAttribute('fill-opacity', '0.9');
    c.setAttribute('stroke', '#fff');
    c.setAttribute('stroke-width', (2 / k).toFixed(2));
    const t = el('text');
    t.setAttribute('x', b.x.toFixed(1));
    t.setAttribute('y', b.y.toFixed(1));
    t.setAttribute('font-size', (10 / k).toFixed(2));
    t.setAttribute('text-anchor', 'middle');
    t.setAttribute('dominant-baseline', 'central');
    t.setAttribute('fill', '#fff');
    t.setAttribute('font-weight', 'bold');
    t.textContent = b.rows.length;
    const ttl = el('title');
    // Say WHICH kind of pile this is, because the two behave differently and the
    // difference is not the map's fault: a stack is what the posts didn't tell us.
    ttl.textContent = b.sameSpot
      ? b.rows.length + ' דירות באותה נקודה בדיוק — הפוסטים לא נתנו מספר בית. ' +
        'לחיצה פורשת אותן'
      : b.rows.length + ' דירות באזור הזה — לחיצה מתקרבת';
    // A badge is the control that opens 97% of the map, and it was 4 px across with no
    // hit padding at all while every single dot got 14–22 px of it. Same fix as mkHit,
    // its own class so the dot handlers keep ignoring it.
    const h = el('circle');
    h.setAttribute('cx', b.x.toFixed(1));
    h.setAttribute('cy', b.y.toFixed(1));
    h.setAttribute('r', Math.max(10 / k, HIT_RADIUS_PX * unitsPerPx).toFixed(2));
    h.setAttribute('class', 'clhit');
    grp.append(h, c, t, ttl);
    // A BADGE MUST CARRY THE HALO TOO, or the feature helps almost never: measured at
    // 1x, 253 of 312 listings sit inside a badge, so a ⭐ flat would be invisible in
    // 81% of cases — exactly the ones you cannot find by eye. Gold therefore means
    // "your shortlist is here", whether that is one flat or one of nine.
    if (b.rows.some(r => r.saved)){
      const halo = el('circle');
      halo.setAttribute('cx', b.x.toFixed(1));
      halo.setAttribute('cy', b.y.toFixed(1));
      halo.setAttribute('r', (CL_HALO_PX * unitsPerPx).toFixed(2));
      halo.setAttribute('class', 'halo cl-halo');
      grp.insertBefore(halo, c);
    }
    g.appendChild(grp);
  }
  for (const f of fans) drawSpider(g, f, k, fill, unitsPerPx);
  svg.appendChild(g);
  /* NAME THE OMISSION. A listing with no coordinate simply vanished from the map, and
     the two counters ("N / M דירות" and "N על המפה") disagreed with no explanation —
     84 of 394 right now, 21%, and precisely the set 🎯 pinning exists to fix. Silently
     dropping a fifth of the data is the one thing this project keeps refusing to do. */
  const lost = rows.length - n;
  $('mapcount').innerHTML = esc(n + ' מתוך ' + rows.length + ' על המפה') +
    (clusters ? esc(' · ' + clusters + ' קבוצות') : '') +
    (lost ? ' · <button id="nolocbtn" class="linkish">' +
            esc(lost + ' ללא מיקום') + '</button>' : '');
  const nb = $('nolocbtn');
  if (nb) nb.onclick = () => showNoLoc(rows.filter(
      r => r.lat === null || r.lat === undefined));
}

/* ---------- the flats with no coordinate ----------
   They are not on the map and never can be until someone says where they are, so the
   honest move is to list them where the count is, not to pretend the map is complete.
   A bottom sheet on a phone, the same shape the card already uses under 700px. */
function showNoLoc(rows){
  const box = $('noloc');
  const canPin = !SNAP && window.__LIVE__;
  // 🎯 can only walk the ones that HAVE an address — govmap has nothing to answer for a
  // post that never named a place. Say the number on the button rather than let it
  // silently cover fewer flats than the heading promises; the rest are still listed,
  // still openable, and still placeable by hand from the card.
  const pinnable = rows.filter(r => (r.address || '').trim()).length;
  box.innerHTML = '<div class="nlhead"><b>' + esc(rows.length + ' דירות ללא מיקום') +
    '</b>' + (canPin && pinnable
                ? ' <button id="nlpin">' + esc('🎯 מקם אותן (' + pinnable + ')') + '</button>'
                : '') +
    ' <button id="nlclose" aria-label="סגור">✕</button></div>' +
    '<div class="nlbody">' + rows.map(r =>
      '<button class="nlrow" data-key="' + esc(r.dedup_key) + '">' +
      '<span class="nladdr">' + esc(r.address || '— ללא כתובת בפוסט') + '</span>' +
      '<span class="muted">' + esc('⭐' + fmt(r.eff_score) +
        (r.price_per_room ? ' · ' + r.price_per_room + '₪' : '')) + '</span></button>').join('') +
    '</div>';
  box.style.display = '';
  $('nlclose').onclick = () => { box.style.display = 'none'; };
  box.querySelectorAll('.nlrow').forEach(b => b.onclick = () => {
    box.style.display = 'none';
    showCard(rowByKey(b.dataset.key));      // no dot to hover — the list IS the way in
  });
  // The pin flow is a WRITE path, so it exists only on the live page; the snapshot
  // still lists them, because knowing which flats have no location is useful offline.
  if (canPin) $('nlpin').onclick = () => { box.style.display = 'none';
                                           startPinFlow(true); };
}

/* ---------- fan a stack open ----------
   Zoom cannot separate dots that share a coordinate, so the only honest way to reach
   them is to move them off it deliberately, on leader lines that show where they
   really came from. */
function drawSpider(g, spider, k, fill, unitsPerPx){
  const R = 34 / k;                                   // constant on screen
  const rows = spider.rows;
  const grp = el('g');
  grp.setAttribute('class', 'spider');
  const hub = el('circle');
  hub.setAttribute('cx', spider.x.toFixed(1));
  hub.setAttribute('cy', spider.y.toFixed(1));
  hub.setAttribute('r', (2.5 / k).toFixed(2));
  hub.setAttribute('class', 'spider-hub');
  grp.appendChild(hub);
  rows.forEach((r, i) => {
    // a second ring past 12 so the dots never overlap each other either
    const ring = i < 12 ? 0 : 1;
    const inRing = ring ? rows.length - 12 : Math.min(rows.length, 12);
    const idx = ring ? i - 12 : i;
    const rad = R * (1 + ring * 0.85);
    const a = (idx / inRing) * Math.PI * 2 - Math.PI / 2;
    const x = spider.x + Math.cos(a) * rad, y = spider.y + Math.sin(a) * rad;
    const leg = el('line');
    leg.setAttribute('x1', spider.x.toFixed(1));
    leg.setAttribute('y1', spider.y.toFixed(1));
    leg.setAttribute('x2', x.toFixed(1));
    leg.setAttribute('y2', y.toFixed(1));
    leg.setAttribute('class', 'spider-leg');
    grp.appendChild(leg);
    addDot(grp, r, x, y, k, fill(r), unitsPerPx);
  });
  g.appendChild(grp);
}

/* How far apart are a group's dots, in world units? A group tighter than one cluster
   cell at maximum zoom can never be pulled apart by zooming. */
function spanOf(rows){
  const pts = rows.map(r => project(r.lat, r.lon));
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  return Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
}

function inView(x, y){
  return x >= view.x && x <= view.x + view.w && y >= view.y && y <= view.y + view.h;
}

/* Fans to draw: the ones you opened by hand, plus — once zoomed in — every stack in
   view. Limited to the viewport on purpose: 38 stacks of up to 19 dots would otherwise
   all be built on every redraw, and drawDots has no culling. */
function activeFans(groups){
  const out = manualFans.slice();
  if (zoomFactor() < AUTO_FAN_ZOOM) return out;
  for (const b of groups){
    if (!b.sameSpot || b.rows.length < 2 || !inView(b.x, b.y)) continue;
    if (out.some(f => Math.abs(f.x - b.x) < 1e-6 && Math.abs(f.y - b.y) < 1e-6)) continue;
    out.push({rows: b.rows, x: b.x, y: b.y});
  }
  return out;
}

function openFan(fan){
  if (!manualFans.some(f => Math.abs(f.x - fan.x) < 1e-6 && Math.abs(f.y - fan.y) < 1e-6))
    manualFans.push(fan);
  drawDots(lastRows);
}

function closeFans(){
  manualFans = [];
  drawDots(lastRows);
}

/* ---------- fit the view to what is showing ---------- */
function fitTo(rows, animateFrom){
  const pts = rows.filter(r => r.lat !== null && r.lat !== undefined)
                  .map(r => project(r.lat, r.lon));
  if (!pts.length) return;
  let x0 = Math.min(...pts.map(p => p[0])), x1 = Math.max(...pts.map(p => p[0]));
  let y0 = Math.min(...pts.map(p => p[1])), y1 = Math.max(...pts.map(p => p[1]));
  const padx = Math.max((x1 - x0) * 0.12, 25), pady = Math.max((y1 - y0) * 0.12, 25);
  x0 -= padx; x1 += padx; y0 -= pady; y1 += pady;
  // honour the aspect ratio, and never zoom past the pan/zoom clamp — three
  // neighbouring flats should not slam the map to 12x
  let w = Math.max(x1 - x0, (y1 - y0) * (WORLD.w / WORLD.h));
  w = Math.min(WORLD.w, Math.max(WORLD.w / 8, w));
  view.w = w;
  view.h = w * (WORLD.h / WORLD.w);
  view.x = (x0 + x1) / 2 - view.w / 2;
  view.y = (y0 + y1) / 2 - view.h / 2;
  applyView();
}

/* ---------- box select ----------
   The one filter a map can express that the toolbar cannot: "these streets, here".
   Shift+drag so it never fights drag-to-pan, plus a button for touch. */
let boxSel = null;                       // {x0,y0,x1,y1} in world coords, or null
function inBox(r){
  if (!boxSel || r.lat === null || r.lat === undefined) return !boxSel;
  const [x, y] = project(r.lat, r.lon);
  return x >= boxSel.x0 && x <= boxSel.x1 && y >= boxSel.y0 && y <= boxSel.y1;
}
function setBox(b){
  boxSel = b;
  const chip = $('boxchip');
  chip.style.display = b ? '' : 'none';
  $('boxbtn').classList.toggle('on', !!boxPending);
  const rect = document.getElementById('boxrect');
  if (rect) rect.remove();
  render();
}
let boxPending = false;                  // touch: next drag draws a box, not a pan

function markCursor(){
  const rows = document.querySelectorAll('tr.row');
  rows.forEach((tr, i) => tr.classList.toggle('cursor', i === cursor));
}

function render(){
  const rows = visible();
  $('tb').innerHTML = rows.map(rowHtml).join('');
  $('n').textContent = rows.length + ' / ' + DATA.length + ' דירות';
  $('listsum').textContent = 'רשימה (' + rows.length + ')';
  const nnew = DATA.filter(isNew).length;
  $('newcount').textContent = nnew
    ? nnew + ' חדשות מאז הביקור הקודם · ' : '';
  drawDots(rows);
  drawWorklist();
  if (cursor >= rows.length) cursor = Math.max(0, rows.length - 1);
  markCursor();
}

/* Which street is worth pinning next?

   A 📍 on a numbered address does not just fix that flat — the server turns it into a
   geocoding anchor, which then places every other number on the same street. So the
   effort is worth wildly different amounts depending on where it lands: one pin on a
   street with nine approximate flats is nine fixes, one pin on a street with a single
   flat is one. This ranks them so the next pin is the expensive one. */
function drawWorklist(){
  const box = $('worklist');
  if (!box) return;
  if (!$('approx').checked){ box.style.display = 'none'; return; }
  const by = new Map();
  DATA.filter(isApprox).forEach(r => {
    if (!r.street) return;                 // canonical, resolved server-side
    by.set(r.street, (by.get(r.street) || 0) + 1);
  });
  const top = [...by.entries()].filter(e => e[1] > 1)
                .sort((a, b) => b[1] - a[1]).slice(0, 8);
  if (!top.length){ box.style.display = 'none'; return; }
  box.style.display = '';
  box.innerHTML = 'פין אחד ברחוב יתקן כמה דירות בבת אחת: ' +
    top.map(([st, n]) =>
      '<button data-street="' + esc(st) + '">' + esc(st) + ' · ' + n + '</button>')
      .join(' ');
}

/* An index, not a scan. rowByKey is called on every hover, every dot click, every table
   row and once per stop in drawRoute; a linear DATA.find over 400 rows inside a
   pointermove handler is the kind of thing that only shows up on a phone. Rebuilt
   whenever DATA is replaced (see setData). */
let _byKey = null;
const rowByKey = k => (_byKey = _byKey ||
    new Map(DATA.map(r => [r.dedup_key, r]))).get(k);

function setData(rows){
  DATA = rows;
  _byKey = null;
}

async function post(path, body){
  if (!window.__LIVE__){
    alert('זמין רק כשהשרת רץ: python serve_dashboard.py');
    return null;
  }
  try {
    const res = await fetch(path + '?token=' + encodeURIComponent(TOKEN), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)});
    return res.ok ? await res.json() : null;
  } catch (e){ return null; }
}

/* Narrowing the filters should take you to what is left — otherwise "3 דירות" sits
   somewhere off the current view and you have to hunt for it. Colour is not a filter,
   so it does not move the map. Switchable off in the legend panel. */
function afterFilter(){
  render();
  const cb = document.querySelector('#legend input[data-layer="autofit"]');
  if (cb && cb.checked) fitTo(visible());
}

/* Typing is the one filter that fires per KEYSTROKE, and each one re-runs passes() over
   every row, rebuilds the table, rebuilds every dot and — with autofit on — animates the
   map. Debounced so a word costs one pass instead of one per letter; the toggles and
   selects are single events and stay immediate. */
const FILTER_DEBOUNCE_MS = 150;
let filterTimer = null;
function afterFilterSoon(){
  clearTimeout(filterTimer);
  filterTimer = setTimeout(afterFilter, FILTER_DEBOUNCE_MS);
}

['q', 'st', 'tier', 'maxp', 'minr', 'stale', 'nobroker', 'saved', 'onlynew', 'approx']
  .forEach(id => {
    const node = $(id);
    if (!node) return;
    const typed = !(node.type === 'checkbox' || node.tagName === 'SELECT');
    node.addEventListener(typed ? 'input' : 'change',
                          typed ? afterFilterSoon : afterFilter);
  });
$('bycolor').addEventListener('change', render);

$('worklist').addEventListener('click', ev => {
  const b = ev.target.closest('button[data-street]');
  if (!b) return;
  $('q').value = b.dataset.street;
  afterFilter();
});

if (!SNAP && window.__LIVE__){
  $('pinstart').style.display = '';
  $('pinstart').onclick = () => startPinFlow(false);
  $('pinaccept').onclick = acceptPin;
  $('pinskip').onclick = () => { pinAt++; showPinStep(); };
  $('pinstop').onclick = endPinFlow;
  $('pinmanual').onclick = () => {
    const item = pinQueue[pinAt];
    if (item) setPlacing(item.key);        // today's tap-to-place, same commit path
  };
}

document.querySelectorAll('#t thead th[data-k]').forEach(th =>
  th.addEventListener('click', () => {
    const k = th.dataset.k;
    if (!k) return;
    sortDir = (k === sortCol) ? -sortDir : -1;
    sortCol = k;
    render();
  }));

$('tb').addEventListener('click', async ev => {
  const btn = ev.target.closest('button, input[type=checkbox]');
  const tr = ev.target.closest('tr');
  if (!tr || !btn) return;
  const key = tr.dataset.key;
  const r = rowByKey(key);
  if (!r) return;
  const act = btn.dataset.act;
  if (act === 'exp'){
    const nxt = tr.nextElementSibling;
    if (nxt && nxt.classList.contains('exp')) nxt.remove();
    else tr.insertAdjacentHTML('afterend', expandHtml(r));
    return;
  }
  if (act === 'cmp'){
    if (btn.checked) compare.add(key); else compare.delete(key);
    drawCompare();
    return;
  }
  if (act === 'savenote'){
    const ta = tr.querySelector('.note');
    const out = await post('/api/note', {key: key, text: ta.value});
    tr.querySelector('[data-note-status]').textContent = out ? 'נשמר' : 'לא נשמר';
    if (out) r.note = out.text;
    return;
  }
  if (act === 'save' || act === 'dismiss' || act === 'contacted'){
    const mark = act === 'save' ? 'saved' : act === 'dismiss' ? 'dismissed' : 'contacted';
    const out = await post('/api/mark', {key: key, mark: mark});
    if (out){
      if (mark === 'saved') r.saved = true;
      if (mark === 'contacted') r.contacted = true;
      if (out.score) r.eff_score = out.score;
      render();
    }
  }
});

$('tb').addEventListener('mouseover', ev => {
  const tr = ev.target.closest('tr.row');
  if (!tr) return;
  document.querySelectorAll('.dot').forEach(d =>
    d.classList.toggle('hi', d.dataset.key === tr.dataset.key));
  const r = rowByKey(tr.dataset.key);
  if (r && r.lat) showCard(r);          // the list drives the map too
});

/* ---------- the apartment card ----------
   Hover on a pointer device, tap on touch. matchMedia('(hover: hover)') is the right
   signal — a narrow window on a laptop still has a mouse. Clicking a dot used to open
   Google Maps, which would have fired on the tap-to-expand gesture; directions are a
   button inside the card now. */
const CAN_HOVER = window.matchMedia('(hover: hover)').matches;
let cardKey = null, hideTimer = null;

/* One 🚶 per transport item, so "18 דק׳ to the 669" can be checked rather than
   trusted. Live-only: it's a real OSRM call. */
function amenityWalks(r){
  if (!window.__LIVE__ || !r.amenity_points || !r.amenity_points.length) return '';
  const bits = r.amenity_points.map((a, i) =>
    '<button data-card="amwalk" data-i="' + i + '" title="מסלול הליכה אל ' +
    esc(a.name) + '">' + a.icon + '🚶</button>').join('');
  return '<div class="btns amwalks">' + bits + '</div>';
}

function cardHtml(r){
  /* Only what decides a click. The note and the full metric list live in the row
     expander — repeating them here is what made the card cover the map. */
  const flags = [r.saved ? '⭐' : '', r.contacted ? '📵' : '', r.stale ? '🕒' : '',
                 r.broker >= 4 ? '⚠️' : '', isNew(r) ? 'חדש' : '']
                .filter(Boolean).join(' ');
  const bits = [
    r.price_per_room ? r.price_per_room + '₪' : 'ללא מחיר',
    (r.available_rooms != null ? r.available_rooms + ' חד׳' : ''),
    (r.walk_minutes != null ? Math.round(r.walk_minutes) + ' דק׳' : ''),
    (r.lease_start || '')
  ].filter(Boolean).join(' · ');
  const photo = (!SNAP && r.photos && r.photos.length)
    ? '<img loading="lazy" src="/img/' + r.photos[0] + '?token=' + TOKEN +
      '" onerror="this.style.display=\'none\'">' : '';
  const btn = (href, label, title) => href
    ? '<a href="' + esc(href) + '" target="_blank" rel="noreferrer" title="' + title +
      '">' + label + '</a>' : '';
  const dirs = r.lat
    ? btn('https://www.google.com/maps/dir/?api=1&destination=' + r.lat + ',' + r.lon,
          '🧭', 'ניווט') : '';
  // the real OSRM route needs the server; in a shared snapshot there's nothing to ask
  const walk = (window.__LIVE__ && r.lat)
    ? '<button data-card="walk" title="מסלול הליכה לשער">🚶</button>' : '';
  // correcting a location writes to the DB, so it needs the server too
  const place = window.__LIVE__
    ? '<button data-card="place" title="' +
      (r.manual_location ? 'מיקום ידני — הקישו לתקן שוב' : 'תקנו את המיקום על המפה') +
      '">' + (r.manual_location ? '📌' : '📍') + '</button>' +
      (r.manual_location
        ? '<button data-card="unplace" title="בטלו את המיקום הידני">↩</button>' : '')
    : '';
  const votes = window.__LIVE__
    ? '<button data-card="save" title="שמור">⭐</button>' +
      '<button data-card="dismiss" title="הסתר">🗑</button>' +
      '<button data-card="contacted" title="יצרתי קשר">📵</button>' : '';
  // How much to trust the dot you just tapped. Silent on an exact hit — the point is
  // to flag the 41% that are a street or neighbourhood centroid, not to nag.
  const PRECISION = {street: '📍 מרכז הרחוב — הפוסט לא נתן מספר בית',
                     area: '📍 מרכז השכונה — מיקום מקורב',
                     none: '📍 לא זוהה מיקום מדויק'};
  const precision = PRECISION[r.geo_confidence]
    ? '<div class="am">' + esc(PRECISION[r.geo_confidence]) + '</div>' : '';
  return '<div class="hd">' + photo + '<div><div class="ttl">' + esc(r.address || '—') +
      ' <span class="pill ' + esc(r.location_tier || 'UNKNOWN') + '">' +
      esc(r.location_tier || '?') + '</span></div>' +
    '<div class="kv">⭐' + fmt(r.eff_score) + ' · ' + esc(bits) +
      (flags ? ' · ' + esc(flags) : '') + '</div></div></div>' +
    precision +
    (r.amenity_text ? '<div class="am">' + esc(r.amenity_text) + '</div>' : '') +
    amenityWalks(r) +
    '<div class="btns">' + btn(r.wa, '💬', 'וואטסאפ') + btn(r.source_url, '🔗', 'הפוסט') +
      dirs + walk + place + votes + '</div>';
}

/* ---------- what we actually know about an imprecise listing ----------
   A street- or area-level listing has no building. Drawing it as a dot still says
   "here", so opening one also lights up the street it names: "somewhere along this
   line" is both true and more useful than a point pretending to be a location. */
function drawStreetHint(r){
  const old = document.getElementById('sthint');
  if (old) old.remove();
  if (!r || !isApprox(r) || !r.street) return;
  const pts = (window.__STREETS__ || {})[r.street];
  if (!pts || pts.length < 2) return;
  const svg = document.querySelector('.map svg');
  const d = pts.map((c, i) => (i ? 'L' : 'M') +
                    project(c[0], c[1]).map(v => v.toFixed(1)).join(' ')).join(' ');
  const g = el('g');
  g.id = 'sthint';
  const p = el('path');
  p.setAttribute('d', d);
  p.setAttribute('class', 'sthint');
  g.appendChild(p);
  // under the dots, so the flat you are pointing at stays on top
  svg.insertBefore(g, document.getElementById('dots'));
}

function showCard(r, dot){
  clearTimeout(hideTimer);
  cardKey = r.dedup_key;
  $('cardbody').innerHTML = cardHtml(r);
  drawMyAmenities(r);                     // point at THIS flat's stops and gym
  drawStreetHint(r);                      // …and the street, when that is all we know
  const card = $('card');
  card.classList.add('on');
  /* Below 700px the CSS turns the card into a bottom sheet; positioning it beside
     the dot would fight that with inline left/top. A touchscreen laptop narrowed to
     phone width reports hover, so the width is the honest test, not CAN_HOVER. */
  const SHEET = window.matchMedia('(max-width:700px)').matches;
  if (SHEET){ card.style.left = ''; card.style.top = ''; }
  if (CAN_HOVER && dot && !SHEET){
    /* Sit beside the dot on whichever side has room, and never ON it — the thing
       you're pointing at has to stay visible. Try right, then left, then below,
       then above; clamp into the map either way. */
    const map = document.querySelector('.map').getBoundingClientRect();
    const d = dot.getBoundingClientRect();
    const w = card.offsetWidth || 200, h = card.offsetHeight || 110;
    const GAP = 12;
    const dx = d.left - map.left, dy = d.top - map.top;
    let x, y = Math.max(6, Math.min(map.height - h - 6, dy + d.height / 2 - h / 2));
    if (dx + d.width + GAP + w <= map.width - 6) x = dx + d.width + GAP;      // right
    else if (dx - GAP - w >= 6) x = dx - GAP - w;                             // left
    else {                                                                     // stack
      x = Math.max(6, Math.min(map.width - w - 6, dx + d.width / 2 - w / 2));
      y = (dy + d.height + GAP + h <= map.height - 6) ? dy + d.height + GAP
                                                      : Math.max(6, dy - GAP - h);
    }
    card.style.left = x + 'px';
    card.style.top = y + 'px';
  } else {
    // bottom-sheet mode: drop any anchor coordinates a previous hover left behind,
    // or the sheet sits off-flush after a resize
    card.style.left = card.style.top = '';
  }
}

function hideCard(){
  $('card').classList.remove('on');
  cardKey = null;
  const am = document.getElementById('myamen');
  if (am) am.remove();
  const st = document.getElementById('sthint');
  if (st) st.remove();
}

/* ---------- the real walking route ----------
   The walk column is a number; this draws the line it came from. Straight-line would
   be a lie here — the railway and Soroka are both in the way — so this is OSRM or
   nothing, and "nothing" says so rather than falling back to a fiction. */
async function drawWalk(key, btn, dest){
  const svg = document.querySelector('.map svg');
  const old = document.getElementById('walkroute');
  const note = $('walknote');
  const tag = key + '|' + (dest ? dest.label : '');
  if (old){
    const same = old.dataset.key === tag;
    old.remove();
    note.style.display = 'none';
    if (same) return;                             // clicking the same 🚶 again clears it
  }
  const label = btn ? btn.textContent : '';
  if (btn) btn.textContent = '…';
  const out = await post('/api/walk', dest ? {key: key, dest: dest} : {key: key});
  if (btn) btn.textContent = label || '🚶';
  if (!out || !out.ok){
    // Name the actual failure. One message for everything meant a dead Docker
    // container and a listing with no coordinates looked identical.
    const WHY = {no_location: 'לדירה הזו אין מיקום על המפה',
                 bad_destination: 'יעד לא תקין',
                 osrm_down: 'שרת המסלולים (OSRM) לא זמין — הפעילו את Docker'};
    note.textContent = (out && WHY[out.reason]) ||
      'לא התקבל מסלול — בדקו ש-serve_dashboard ו-OSRM רצים';
    note.style.display = '';
    return;
  }
  const g = el('g');
  g.id = 'walkroute';
  g.dataset.key = tag;                    // so the same button toggles it off
  const d = out.coords.map((c, i) => (i ? 'L' : 'M') + project(c[0], c[1])
                                       .map(v => v.toFixed(1)).join(' ')).join(' ');
  for (const cls of ['wr-cas', 'wr']){            // casing + line, so it reads over streets
    const p = el('path');
    p.setAttribute('d', d);
    p.setAttribute('class', cls);
    g.appendChild(p);
  }
  svg.appendChild(g);
  note.textContent = '🚶 ' + out.minutes + ' דק׳ · ' + out.metres + ' מ׳ אל ' +
                     (out.gate || (dest && dest.label) || 'היעד') + ' — לחצו שוב לניקוי';
  note.style.display = '';
  fitTo(out.coords.map(c => ({lat: c[0], lon: c[1]})));
}

/* ---------- the open listing's own transport ----------
   The global layer can only show fixed landmarks (the 669 stops, the gym). The stop
   that matters is the one THIS flat would use, and that is per-listing — so it is
   drawn when its card opens, with a hairline back to the flat. */
function drawMyAmenities(r){
  const svg = document.querySelector('.map svg');
  const old = document.getElementById('myamen');
  if (old) old.remove();
  if (!svg || !r || !r.amenity_points || !r.amenity_points.length || !r.lat) return;
  const k = zoomFactor();
  const g = el('g');
  g.id = 'myamen';
  const home = project(r.lat, r.lon);
  for (const a of r.amenity_points){
    const p = project(a.lat, a.lon);
    const leg = el('line');
    leg.setAttribute('x1', home[0].toFixed(1)); leg.setAttribute('y1', home[1].toFixed(1));
    leg.setAttribute('x2', p[0].toFixed(1));    leg.setAttribute('y2', p[1].toFixed(1));
    leg.setAttribute('class', 'myamen-leg');
    g.appendChild(leg);
    const bg = el('circle');
    bg.setAttribute('cx', p[0].toFixed(1)); bg.setAttribute('cy', p[1].toFixed(1));
    bg.setAttribute('r', (11 / k).toFixed(2));
    bg.setAttribute('class', 'myamen-bg');
    const t = el('text');
    t.setAttribute('x', p[0].toFixed(1)); t.setAttribute('y', p[1].toFixed(1));
    t.setAttribute('font-size', (15 / k).toFixed(2));
    t.setAttribute('text-anchor', 'middle');
    t.setAttribute('dominant-baseline', 'central');
    t.textContent = a.icon;
    const ttl = el('title');
    ttl.textContent = a.icon + ' ' + a.name +
      (a.minutes != null ? ' · ' + Math.round(a.minutes) + ' דק׳ הליכה' : '');
    t.appendChild(ttl);
    g.append(bg, t);
  }
  svg.appendChild(g);
}

/* ---------- place-mode: tap the map to correct a location ----------
   Chosen over dragging the dot: a 5px dot is a coin-flip to grab on a phone, and an
   accidental drag would silently move a flat. Place-mode makes the intent explicit —
   you arm it, you tap, you confirm, and you see what it did to the grade. */
let placing = null;                       // the dedup_key being placed, or null

function setPlacing(key){
  placing = key;
  const svg = document.querySelector('.map svg');
  svg.classList.toggle('placing', !!key);
  const bar = $('placebar');
  bar.style.display = key ? '' : 'none';
  // both bars live in the same slot, and the guided flow deliberately falls through to
  // place-mode — so stack instead of overlapping. Measured, because the pin bar wraps.
  const flow = $('pinflow');
  bar.style.top = (key && flow && flow.style.display !== 'none')
      ? (flow.offsetHeight + 12) + 'px' : '';
  if (key){
    const r = rowByKey(key);
    $('placewhat').textContent = (r && r.address) || '';
    hideCard();
  }
}

async function commitPlace(key, lat, lon, scope){
  const out = await post('/api/locate', {key: key, lat: lat, lon: lon, scope: scope});
  if (!out || !out.ok){
    const why = 'לא נשמר' + (out && out.reason ? ' (' + out.reason + ')' : '');
    $('placemsg').textContent = why;
    if (inPinFlow(key)) $('pinmsg').textContent = why;
    return;
  }
  const r = rowByKey(key);
  if (r){
    r.lat = lat; r.lon = lon;
    r.manual_location = true;
    r.geocode_source = 'manual';
    r.geo_confidence = 'exact';
    if (out.regraded){
      r.location_tier = out.after.location_tier;
      r.walk_minutes = out.after.walk_minutes;
      r.eff_score = out.after.eff_score;
      r.status = out.after.status;
    }
  }
  setPlacing(null);
  manualFans = [];                 // the flat just moved; its old fan is meaningless
  render();
  // Say what the move DID. A corrected location re-grades the flat — it can move
  // RED->GREEN — and that consequence should never be silent.
  const b = out.before, a = out.after;
  const chg = out.regraded && (b.location_tier !== a.location_tier ||
                               Math.round(b.eff_score) !== Math.round(a.eff_score))
    ? ' · ' + b.location_tier + '→' + a.location_tier +
      ' · ⭐' + fmt(b.eff_score) + '→' + fmt(a.eff_score)
    : (out.regraded ? ' · הדירוג לא השתנה' : ' · הדירוג לא חושב מחדש (הפוסט לא נשמר)');
  // A numbered pin also teaches the geocoder where that number is, which places every
  // other number on the street. Worth saying — it is why pinning is worth the trouble.
  $('walknote').textContent = '📍 המיקום עודכן' +
    (out.pinned_address ? ' לכל הכתובת "' + out.pinned_address + '"' : '') +
    (out.taught_street ? ' · נלמד "' + out.taught_street + '" לכל הרחוב' : '') + chg;
  $('walknote').style.display = '';
  // The queue advances HERE, not in acceptPin, so correcting by hand moves on exactly
  // like accepting does — otherwise the manual path left the flow stuck on a flat that
  // was already placed.
  if (inPinFlow(key)){ pinAt++; showPinStep(); }
}

function initPlacing(){
  const svg = document.querySelector('.map svg');
  if (!svg) return;
  svg.addEventListener('click', ev => {
    if (!placing) return;
    ev.stopPropagation();
    const b = svg.getBoundingClientRect();
    const wx = view.x + ((ev.clientX - b.left) / b.width) * view.w;
    const wy = view.y + ((ev.clientY - b.top) / b.height) * view.h;
    const ll = unproject(wx, wy);
    const key = placing;
    const r = rowByKey(key);
    const addr = (r && r.address) || '';
    // Two scopes, because the two failures are different: one flat in the wrong spot
    // vs an ADDRESS that always resolves wrongly (אוניברסיטת בן גוריון, שכונה ד).
    const both = addr && confirm('לתקן את המיקום של כל הדירות בכתובת "' + addr +
      '"?\n\nאישור = כל הכתובת (גם דירות עתידיות)\nביטול = רק הדירה הזו');
    commitPlace(key, ll[0], ll[1], both ? 'address' : 'listing');
  }, true);                                // capture: beat the cluster/spider handler
  $('placecancel').addEventListener('click', () => setPlacing(null));
  document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape' && placing) setPlacing(null);
  });
}

function initCard(){
  const svg = document.querySelector('.map svg');
  if (!svg) return;
  /* CLICK IS ALWAYS BOUND. It used to live in the `else` of this branch, so on any
     device that reports hover — every desktop, and plenty of touch laptops and mobile
     browsers — clicking a dot did nothing at all. Hover is an ADDITION for pointer
     devices, never the only way in. */
  svg.addEventListener('click', ev => {
    if (placing) return;                       // place-mode owns the next tap
    const dot = ev.target.closest('.dot, .hit');
    if (!dot){ if (!ev.target.closest('.cl')) hideCard(); return; }
    const r = rowByKey(dot.dataset.key);
    if (r) showCard(r, dot);
  });
  if (CAN_HOVER){
    svg.addEventListener('mouseover', ev => {
      const dot = ev.target.closest('.dot, .hit');
      if (!dot) return;
      const r = rowByKey(dot.dataset.key);
      if (r) showCard(r, dot);
    });
    // a grace delay so the pointer can travel from the dot onto the buttons
    svg.addEventListener('mouseout', ev => {
      if (ev.target.closest('.dot')) hideTimer = setTimeout(hideCard, 260);
    });
    $('card').addEventListener('mouseenter', () => clearTimeout(hideTimer));
    $('card').addEventListener('mouseleave', hideCard);
  }
  $('card').querySelector('.x').addEventListener('click', hideCard);
  $('card').addEventListener('click', async ev => {
    const b = ev.target.closest('[data-card]');
    if (!b || !cardKey) return;
    const act = b.dataset.card;
    if (act === 'walk'){ await drawWalk(cardKey, b); return; }
    if (act === 'amwalk'){
      const r = rowByKey(cardKey);
      const a = r && r.amenity_points[+b.dataset.i];
      if (a) await drawWalk(cardKey, b, {lat: a.lat, lon: a.lon, label: a.name});
      return;
    }
    if (act === 'place'){ setPlacing(cardKey); return; }
    if (act === 'unplace'){
      const out = await post('/api/locate', {key: cardKey, clear: true});
      if (out && out.ok){
        const r = rowByKey(cardKey);
        if (r){
          r.manual_location = false;
          if (out.regraded){
            // including lat/lon: the dot has to go back to wherever the geocoder
            // says, not sit at the point we just cleared
            r.lat = out.after.lat; r.lon = out.after.lon;
            r.geocode_source = out.after.geocode_source;
            r.geo_confidence = out.after.geo_confidence;
            r.location_tier = out.after.location_tier;
            r.walk_minutes = out.after.walk_minutes;
            r.eff_score = out.after.eff_score;
          }
        }
        hideCard();
        render();
      }
      return;
    }
    const mark = act === 'save' ? 'saved' : act === 'dismiss' ? 'dismissed' : 'contacted';
    const out = await post('/api/mark', {key: cardKey, mark: mark});
    if (out){
      const r = rowByKey(cardKey);
      if (r){
        if (mark === 'saved') r.saved = true;
        if (mark === 'contacted') r.contacted = true;
        if (out.score) r.eff_score = out.score;
        const k = cardKey;
        render();
        showCard(rowByKey(k));
      }
    }
  });
}

document.addEventListener('click', ev => {
  const img = ev.target.closest('.thumb');
  if (img){ $('lbimg').src = img.src; $('lb').classList.add('on'); }
  else if (ev.target.id === 'lb' || ev.target.id === 'lbimg') $('lb').classList.remove('on');
});

document.addEventListener('keydown', ev => {
  /* Only TEXT entry should swallow the shortcuts. A checkbox is an INPUT too, so a
     blanket tag check meant that ticking a compare box silently killed j/k/s/x. */
  const a = document.activeElement, t = (a.type || '').toLowerCase();
  const typing = a.tagName === 'TEXTAREA' || a.tagName === 'SELECT' ||
    (a.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'submit'].includes(t));
  if (typing) return;
  const rows = document.querySelectorAll('tr.row');
  if (!rows.length) return;
  const move = d => { cursor = Math.max(0, Math.min(cursor + d, rows.length - 1));
                      markCursor(); rows[cursor].scrollIntoView({block: 'nearest'}); };
  if (ev.key === 'j') move(1);
  else if (ev.key === 'k') move(-1);
  else if (ev.key === 's'){ const b = rows[cursor].querySelector('[data-act=save]'); if (b) b.click(); }
  else if (ev.key === 'x'){ const b = rows[cursor].querySelector('[data-act=dismiss]'); if (b) b.click(); }
  else if (ev.key === 'Enter'){ const a = rows[cursor].querySelector('td.addr a'); if (a) a.click(); }
  else if (ev.key === '?') alert('j/k תנועה · s שמור · x בטל · Enter פתח פוסט');
});

function drawCompare(){
  const p = $('cmp');
  if (compare.size < 2){ p.classList.remove('on'); return; }
  const picked = [...compare].map(rowByKey).filter(Boolean).slice(0, 4);
  const F = [['ציון', 'eff_score'], ['מחיר', 'price_per_room'],
             ['פנויים', 'available_rooms'], ['שותפים', 'total_roommates'],
             ['הליכה', 'walk_minutes'], ['כניסה', 'lease_start'],
             ['קומה', 'floor'], ['תחבורה', 'amenity_text']];
  p.innerHTML = '<table><tr><th></th>' +
    picked.map(r => '<th>' + esc(r.address || '—') + '</th>').join('') + '</tr>' +
    F.map(f => '<tr><td class="muted">' + f[0] + '</td>' +
      picked.map(r => '<td>' + esc(fmt(r[f[1]])) + '</td>').join('') + '</tr>').join('') +
    '</table>' + (SNAP ? '' : '<button id="route">תכנן מסלול צפייה</button> ' +
    '<span id="routeout" class="live"></span>');
  p.classList.add('on');
  if (SNAP) return;
  $('route').onclick = async () => {
    const btn = $('route'), label = btn.textContent;
    btn.textContent = '…';
    const out = await post('/api/route', {keys: picked.map(r => r.dedup_key)});
    btn.textContent = label;
    drawRoute(out);
  };
}

/* ---------- guided pinning ----------
   The worklist told you WHICH streets were worth a pin; finding each building was still
   yours to do. Now govmap proposes and you judge — accept, correct, or skip. It is never
   automatic: govmap substitutes silently (`בני אור 999` -> `בני אור 13`), so a person has
   to see the answer before it becomes an anchor that moves a whole street. */
let pinQueue = [], pinAt = 0, pinCand = null;

/* true when `key` is the flat the flow is currently asking about — the one condition
   under which a save should advance the queue */
const inPinFlow = key => !!(pinQueue[pinAt] && pinQueue[pinAt].key === key);

async function startPinFlow(unplacedOnly){
  const out = await post('/api/worklist', {unplaced_only: !!unplacedOnly});
  pinQueue = (out && out.items) || [];
  pinAt = 0;
  if (!pinQueue.length){ alert('אין דירות שדורשות מיקום — הכל ממוקם'); return; }
  $('pinflow').style.display = '';
  showPinStep();
}

function endPinFlow(){
  pinQueue = []; pinCand = null;
  $('pinflow').style.display = 'none';
  const c = document.getElementById('pincand');
  if (c) c.remove();
  drawStreetHint(null);
}

async function showPinStep(){
  const c = document.getElementById('pincand');
  if (c) c.remove();
  pinCand = null;
  if (pinAt >= pinQueue.length){
    $('pinmsg').textContent = 'סיימנו — כל הרשימה עברה';
    $('pinaccept').disabled = true;
    return;
  }
  const item = pinQueue[pinAt];
  const r = rowByKey(item.key);
  $('pinwhat').textContent = item.address;
  $('pincount').textContent = (pinAt + 1) + '/' + pinQueue.length +
      (item.fixes > 1 ? ' · פין כאן יתקן ' + item.fixes + ' דירות' : '');
  $('pinmsg').textContent = 'שואל את govmap…';
  $('pinaccept').disabled = true;
  if (r) drawStreetHint(r);
  const out = await post('/api/propose', {key: item.key});
  if (!out || !out.ok){
    const WHY = {govmap_no_answer: 'govmap לא מצא — סמנו ידנית על המפה',
                 no_housing: 'govmap החזיר נקודה בתוך ' + ((out && out.where) || 'מוסד') +
                             ' — לא מקום מגורים, סמנו ידנית',
                 no_address: 'אין כתובת בפוסט'};
    $('pinmsg').textContent = (out && WHY[out.reason]) || 'אין הצעה — סמנו ידנית';
    setPlacing(item.key);                 // fall straight through to tap-to-place
    return;
  }
  pinCand = out;
  // show govmap's OWN wording, not the address we asked about — reading the two side by
  // side is the only way a person can catch a substitution
  $('pinmsg').textContent = 'govmap: ' + (out.found || out.address || '') + ' — מאשרים?';
  $('pinaccept').disabled = false;
  // move the view FIRST: the ring counter-scales off the current zoom, so drawing it
  // before fitTo would leave it the wrong size the moment the view lands.
  fitTo([{lat: out.lat, lon: out.lon}]);
  drawCand(out.lat, out.lon);
}

/* the proposed point: a white halo under a dashed purple ring, sized in screen terms
   so it stays a ring and never becomes a blob when you zoom in to look at it */
function drawCand(lat, lon){
  const svg = document.querySelector('.map svg');
  const old = document.getElementById('pincand');
  if (old) old.remove();
  const g = el('g');
  g.id = 'pincand';
  const [x, y] = project(lat, lon);
  const r = (12 / zoomFactor()).toFixed(2);
  for (const cls of ['pincand halo', 'pincand']){
    const c = el('circle');
    c.setAttribute('cx', x.toFixed(1)); c.setAttribute('cy', y.toFixed(1));
    c.setAttribute('r', r);
    c.setAttribute('class', cls);
    g.appendChild(c);
  }
  svg.appendChild(g);
}

async function acceptPin(){
  if (!pinCand) return;
  const item = pinQueue[pinAt], cand = pinCand;
  $('pinmsg').textContent = 'שומר…';
  $('pinaccept').disabled = true;
  // scope "address" when govmap answered with a house number: that point is the whole
  // address, and relocate() then teaches it as an anchor for the entire street.
  await commitPlace(item.key, cand.lat, cand.lon, cand.number ? 'address' : 'listing');
}

/* ---------- an afternoon of viewings ----------
   The planner used to print an order and nothing else. What you need before setting out
   is how long it takes and which way you actually walk, so each leg carries its own
   minutes and its real OSRM path. When the router is down we say the order is an
   estimate and draw NOTHING — a straight line here would cross the railway, which is
   the same lie drawWalk already refuses to tell. */
function drawRoute(out){
  const old = document.getElementById('viewroute');
  if (old) old.remove();
  const note = $('routeout');
  if (!out || !out.order || !out.order.length){ note.textContent = 'לא זמין'; return; }
  const name = k => (rowByKey(k) || {}).address || k;
  const legs = out.legs || [];
  note.innerHTML = out.order.map((k, i) => {
    const leg = legs[i - 1];
    const gap = leg && leg.minutes ? ' <span class="muted">' + leg.minutes + '′</span> → ' : (i ? ' → ' : '');
    return gap + '<b>' + (i + 1) + '.</b> ' + esc(name(k));
  }).join('') +
    (out.total_minutes ? ' <b>· סה״כ ' + out.total_minutes + ' דק׳ הליכה</b>' : '') +
    (out.estimated ? ' <span class="muted">(סדר משוער — שרת המסלולים כבוי, אין מסלול לצייר)</span>' : '');
  if (!out.coords || !out.coords.length) return;
  const svg = document.querySelector('.map svg');
  const g = el('g');
  g.id = 'viewroute';
  const d = out.coords.map((c, i) => (i ? 'L' : 'M') +
                  project(c[0], c[1]).map(v => v.toFixed(1)).join(' ')).join(' ');
  for (const cls of ['wr-cas', 'wr']){
    const p = el('path');
    p.setAttribute('d', d);
    p.setAttribute('class', cls);
    g.appendChild(p);
  }
  svg.appendChild(g);
  fitTo(out.coords.map(c => ({lat: c[0], lon: c[1]})));
}

async function poll(){
  try {
    const v = await (await fetch('/api/version?token=' + encodeURIComponent(TOKEN))).json();
    // Surface a dead router before it's needed, rather than after a failed tap.
    $('osrmstate').textContent = v.osrm === false
      ? ' · ⚠️ שרת המסלולים כבוי (Docker) — 🚶 לא יעבוד' : '';
    const sig = JSON.stringify(v);
    if (sig !== window.__VER__){
      window.__VER__ = sig;
      const d = await (await fetch('/api/listings.json?token=' + encodeURIComponent(TOKEN))).json();
      setData(d.listings);
      render();
    }
  } catch (e){ /* server stopped or PC asleep — keep showing what we have */ }
}

/* the list stays closed by default — the map is the view — but remember a choice */
const list = $('list');
if (localStorage.getItem('bgu_list_open') === '1') list.open = true;
list.addEventListener('toggle', () =>
  localStorage.setItem('bgu_list_open', list.open ? '1' : '0'));

render();
initMapGestures();
initLayers();
initCard();
initPlacing();
const focusKey = new URLSearchParams(location.search).get('key');
if (focusKey){
  const r = rowByKey(focusKey);
  if (r){ $('q').value = r.address || ''; render(); }
}
if (window.__LIVE__){ window.__VER__ = null; poll(); setInterval(poll, window.__POLL__ * 1000); }
"""

_HEAD = [("", ""), ("ציון", "eff_score"), ("סטטוס", "status"), ("אזור", "location_tier"),
         ("מחיר", "price_per_room"), ("פנויים", "available_rooms"),
         ("שותפים", "total_roommates"), ("כתובת", "address"), ("הליכה", "walk_minutes"),
         ("כניסה", "lease_start"), ("תחבורה ושירותים", "amenity_text"),
         ("קשר", "contact"), ("", "")]


def plan_route(keys) -> dict:
    """Order a shortlist into an efficient walking route.

    Nearest-neighbour over real foot-routing times from osrm.table_minutes (one call per
    stop; a shortlist is a handful of flats, so that's a handful of calls). With OSRM
    down it falls back to straight-line distance and says so, rather than refusing."""
    import osrm
    from zones import _haversine_m
    rows = {r["dedup_key"]: r for r in _rows()}
    pts = [(k, rows[k]["lat"], rows[k]["lon"]) for k in keys
           if k in rows and rows[k]["lat"] is not None]
    if len(pts) < 2:
        return {"order": [p[0] for p in pts], "estimated": True}

    estimated = False
    cost = {}
    for key, la, lo in pts:
        mins = osrm.table_minutes(la, lo, [(b, c) for _k, b, c in pts])
        if not mins:
            estimated = True
            mins = [_haversine_m(la, lo, b, c) / 80.0 for _k, b, c in pts]
        cost[key] = {pts[i][0]: (m if m is not None else 1e6) for i, m in enumerate(mins)}

    remaining = [p[0] for p in pts]
    order = [remaining.pop(0)]
    while remaining:
        last = order[-1]
        nxt = min(remaining, key=lambda k: cost[last].get(k, 1e6))
        remaining.remove(nxt)
        order.append(nxt)

    # The order alone was all this returned, printed as a line of text. An afternoon of
    # viewings needs to know how long it takes and which way you actually walk, so each
    # leg now carries its own minutes and its real path. One geometry call per leg —
    # a shortlist is capped at 8, and the alternative is a straight line across the
    # railway, which is the lie `drawWalk` already refuses to tell.
    at = {k: (rows[k]["lat"], rows[k]["lon"]) for k in order}
    legs, coords, total = [], [], 0.0
    for a, b in zip(order, order[1:]):
        mins = cost[a].get(b)
        leg = {"from": a, "to": b,
               "minutes": round(mins, 1) if mins and mins < 1e5 else None}
        if not estimated:
            got = osrm.foot_geometry(at[a][0], at[a][1],
                                     {"lat": at[b][0], "lon": at[b][1]})
            if got:
                leg["minutes"] = round(got["minutes"], 1)
                leg["metres"] = round(got["metres"])
                coords.extend(got["coords"])
        if leg["minutes"]:
            total += leg["minutes"]
        legs.append(leg)
    return {"order": order, "estimated": estimated, "legs": legs,
            "total_minutes": round(total, 1) if total else None, "coords": coords}


def _json_for_script(obj) -> str:
    """JSON safe to embed inside a <script> block.

    json.dumps escapes quotes and backslashes but NOT '<', so an address or post body
    containing "</script>" would close the tag and everything after it would be parsed
    as HTML — a script-injection hole, and this data comes straight from Facebook posts.
    Escaping < > & as \\uXXXX is invisible to JSON.parse and closes it."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def render(live: bool, snapshot: bool = False) -> str:
    """The dashboard page. `live=True` fetches from the server and can vote/note;
    `live=False` inlines the data so the file works offline with no server at all.
    `snapshot=True` is the copy you send someone: same page, write controls removed,
    dated so a three-day-old file is never mistaken for current."""
    rows = _rows()
    placed = [(r["lat"], r["lon"]) for r in rows if r["lat"] is not None]
    base_svg, projection = map_listings.build_base_svg(
        [(la, lo, None, None, None, None, None, None) for la, lo in placed])
    payload = [] if live else rows_for_api()
    matches = sum(1 for r in rows if r["status"] == "MATCH")
    mode = ("מתעדכן אוטומטית" if live
            else "קובץ סטטי — הרץ serve_dashboard.py לעדכון חי ולגישה מהנייד")
    stamp = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    # A sent file has no way to tell you a fresher copy exists. Put the live URL in the
    # banner so whoever is holding a three-day-old download can get today's.
    live_url = ""
    try:
        import publish
        live_url = publish.pages_url()
    except Exception:
        pass
    link = (f' <a href="{html.escape(live_url)}">לגרסה המתעדכנת ←</a>' if live_url else "")
    banner = (f'<p class="snap">📸 צילום מצב מ־{stamp} — הנתונים לא מתעדכנים בקובץ הזה. '
              f'דירה שנמצאה אחרי המועד הזה לא תופיע כאן.{link}</p>' if snapshot else "")

    head = "".join(f'<th data-k="{k}">{html.escape(h)}</th>' for h, k in _HEAD)
    return f"""<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">
<title>BGU housing dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
{_pwa_head(snapshot)}
<style>{_CSS}{map_listings.STREET_CSS}</style>
<div class="wrap">
<h1>לוח דירות — BGU</h1>
{banner}
<p class="sub">{len(rows)} רשומות · {matches} התאמות · {len(placed)} על המפה ·
  <span id="newcount"></span><span class="live">{mode}</span><span id="osrmstate"
  class="live"></span></p>
<div class="bar">
  <input id="q" type="search" placeholder="חיפוש — כולל טקסט הפוסט המקורי…">
  <label>סטטוס <select id="st"><option value="">הכל</option>
    <option>MATCH</option><option>NEEDS_DATA</option></select></label>
  <label>אזור <select id="tier"><option value="">הכל</option>
    <option>GREEN</option><option>AMBER</option><option>RED</option>
    <option>UNKNOWN</option></select></label>
  <label>מחיר עד <input id="maxp" type="number" style="width:86px"></label>
  <label>חדרים מ־ <input id="minr" type="number" style="width:60px"></label>
  <label><input id="stale" type="checkbox"> ללא ישנות</label>
  <label><input id="nobroker" type="checkbox"> ללא מתווכים</label>
  <label><input id="saved" type="checkbox"> ⭐ בלבד</label>
  <label><input id="onlynew" type="checkbox"> חדשות בלבד</label>
  <label><input id="approx" type="checkbox"> מיקום משוער בלבד</label>
  <label>צבע <select id="bycolor"><option value="tier">אזור</option>
    <option value="score">ציון</option></select></label>
</div>
<div class="count"><span id="n"></span> · <span id="mapcount"></span></div>
<div id="worklist" style="display:none"></div>
<div class="panel" id="cmp"></div>
<div class="map">{base_svg}</svg>
  <div class="mapbtns">
    <button id="fitbtn" title="התאם את התצוגה לדירות המסוננות">התאם</button>
    <button id="boxbtn" title="סימון אזור: Shift+גרירה, או לחצו כאן ואז גררו">▭ אזור</button>
    <button id="reset" title="איפוס תצוגה">איפוס</button>
    <button id="pinstart" style="display:none"
            title="עוברים על הדירות חסרות המיקום — govmap מציע, אתם מאשרים">🎯 מיקומים</button>
  </div>
  <div id="boxchip" style="display:none">אזור מסומן
    <button id="boxclear" title="בטל את סימון האזור">✕</button></div>
  <div id="noloc" style="display:none"></div>
  <div id="walknote" style="display:none"></div>
  <div id="placebar" style="display:none">📍 הקישו על המיקום הנכון של
    <b id="placewhat"></b> <span id="placemsg" class="live"></span>
    <button id="placecancel">ביטול</button></div>
  <div id="pinflow" style="display:none">🎯 <b id="pinwhat"></b>
    <span id="pincount" class="live"></span> · <span id="pinmsg" class="live"></span>
    <button id="pinaccept">אישור</button>
    <button id="pinmanual">סימון ידני</button>
    <button id="pinskip">דילוג</button>
    <button id="pinstop">סיום</button></div>
  {_legend_html()}
  <div class="maphint">Ctrl+גלגלת או צביטה לזום · גרירה/אצבע להזזה ·
    ריחוף/נגיעה בנקודה לפרטים</div>
  <div id="card"><button class="x" aria-label="סגור">✕</button><div id="cardbody"></div></div>
</div>
<details class="list" id="list"><summary id="listsum">רשימה</summary>
<div class="scroll"><table id="t"><thead><tr>{head}</tr></thead><tbody id="tb"></tbody></table></div>
</details>
<p class="sub">⭐ שמור · 📵 יצרתי קשר · 🕒 ישן מ־{config.LISTING_STALE_DAYS} ימים ·
  ⚠️ מתווך · ▾ פירוט ניקוד והפוסט המקורי · j/k/s/x מקלדת · לחיצה על נקודה פותחת ניווט.</p>
</div>
<div id="lb"><img id="lbimg" alt=""></div>
<script>
window.__BOOT__ = {_json_for_script(payload)};
window.__STREETS__ = {_json_for_script(street_shapes(rows))};
window.__PROJ__ = {json.dumps(projection)};
window.__LIVE__ = {str(bool(live)).lower()};
window.__SNAPSHOT__ = {str(bool(snapshot)).lower()};
window.__POLL__ = {config.DASHBOARD_POLL_SECONDS};
</script>
<script>{_JS}</script>
</html>"""


def walk_route(key: str, dest: dict | None = None) -> dict:
    """The real walking path from one listing to a campus gate — or, with `dest`, to
    its bus stop or the gym.

    The walk column is a number; this is the line it came from — whether you cross
    the railway, go round Soroka, or walk straight down רגר. OSRM only: there is no
    honest straight-line version of "which way do I actually go", so with the router
    down this returns {ok: False} and the page says so."""
    row = next((r for r in _rows() if r["dedup_key"] == key), None)
    if not row or row["lat"] is None:
        return {"ok": False, "reason": "no_location"}

    if dest:
        try:
            target = {"lat": float(dest["lat"]), "lon": float(dest["lon"]),
                      "name": str(dest.get("label") or "")}
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "reason": "bad_destination"}
    else:
        # Ask which gate is nearest with ONE table call, then fetch only that path.
        # Four sequential route calls meant four 15-second timeouts to discover the
        # router was down — long past when someone on a phone gives up.
        gates = list(config.GATES.values())
        mins = osrm.table_minutes(row["lat"], row["lon"],
                                  [(g["lat"], g["lon"]) for g in gates])
        pairs = [(m, g) for m, g in zip(mins or [], gates) if m is not None]
        if not pairs:
            return {"ok": False, "reason": "osrm_down"}
        target = min(pairs, key=lambda p: p[0])[1]

    got = osrm.foot_geometry(row["lat"], row["lon"], target)
    if not got:
        return {"ok": False, "reason": "osrm_down"}
    return {"ok": True, "gate": target.get("name", ""), "coords": got["coords"],
            "minutes": round(got["minutes"], 1), "metres": round(got["metres"])}


def relocate(key: str, lat: float, lon: float, scope: str = "listing") -> dict:
    """Place one listing by hand and re-grade it.

    Moving a dot changes the truth, not just the picture: the zone tier, the walk to a
    gate and therefore the fit score all follow from where the flat is, so a listing
    can legitimately move RED→GREEN here. The response carries before→after so the
    page can show that rather than changing it silently.

    scope='address' also pins the ADDRESS TEXT (geocode.add_pin), which fixes every
    listing at that address and every future one — the right scope for the two flats
    whose address is literally `אוניברסיטת בן גוריון`, or for a bare `שכונה ד`.

    Either way, a pin on a NUMBERED address also becomes a geocoding anchor
    (`geocode.add_anchor`): saying "number 140 is here" is exactly what an anchor is, and
    it then places every other number on that street. On the 18 streets OSM has no
    addresses for at all this is the only mechanism that can ever work — the numbering
    origin of a street is not derivable from free data. `add_anchor` refuses a point more
    than 200 m from the street it claims, so a mis-tap cannot poison the street; the
    listing's own manual location is stored separately and stands regardless.

    The re-grade goes through pipeline._classify, the same function the live pipeline
    and replay.py use, so a corrected listing can never be graded by different rules
    than everything else."""
    if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
        return {"ok": False, "reason": "bad_coordinates"}
    before = next((r for r in _rows() if r["dedup_key"] == key), None)
    if not before:
        return {"ok": False, "reason": "unknown_key"}

    storage.set_manual_location(key, lat, lon)
    pinned_address = None
    if scope == "address" and (before["address"] or "").strip():
        pinned_address = geocode.add_pin(before["address"].strip(), lat, lon)
        geocode.uncache(before["address"].strip())     # drop the wrong cached hit
    taught = _teach_anchor(before.get("address"), lat, lon)

    after = _regrade(key)
    out = {"ok": True, "lat": lat, "lon": lon, "pinned_address": pinned_address,
           "taught_street": taught,
           "before": {k: before.get(k) for k in ("location_tier", "walk_minutes",
                                                 "eff_score", "status")}}
    out["after"] = after or out["before"]
    out["regraded"] = after is not None
    return out


def propose_location(key: str) -> dict:
    """Ask govmap where this listing's address is, so the pinning flow can offer an
    answer instead of asking the user to hunt for the building.

    Deliberately only a PROPOSAL. govmap substitutes silently — `בני אור 999` comes back
    as `בני אור 13`, and a nonsense street comes back in רמלה — so nothing here writes
    anything. The user accepts, corrects or skips, which is the whole point of a confirm
    step: a person sees the substitution."""
    row = next((r for r in _rows() if r["dedup_key"] == key), None)
    if not row:
        return {"ok": False, "reason": "unknown_key"}
    addr = (row.get("address") or "").strip()
    if not addr:
        return {"ok": False, "reason": "no_address"}
    import govmap
    hn = geocode._house_number(addr)
    street = row.get("street")
    found, pt = None, None
    if street and hn:
        found, pt = govmap.address_detail(
            street, hn, verify=lambda head: streets.canonical(head)[0] == street)
    if pt is None:                       # no usable street/number — try the whole string
        for kind, text, p in govmap.search(f"{addr} באר שבע"):
            if kind == "address" and any(c in text for c in ("באר שבע", "באר-שבע")):
                if not hn or govmap._has_number(text, hn):
                    found, pt = text, p
                    break
    if pt is None:
        return {"ok": False, "reason": "govmap_no_answer", "address": addr}
    import zones
    where = zones.no_housing_here(pt[0], pt[1])
    if where:
        return {"ok": False, "reason": "no_housing", "where": where, "address": addr}
    # `found` is govmap's OWN wording, which is the thing the user is confirming — the
    # post said `ביאליק 122, שכונה ב`, govmap answers `ביאליק 122, באר שבע`, and the
    # difference between those two is exactly what a confirm step is for.
    return {"ok": True, "lat": pt[0], "lon": pt[1], "address": addr, "found": found,
            "street": street, "number": hn}


def pin_worklist(unplaced_only: bool = False) -> list:
    """Imprecise listings worth pinning, best first.

    Ordered by how many OTHER listings the same pin would fix, because a 📍 on a numbered
    address becomes a street anchor and places every other number on that street.

    `unplaced_only` narrows it to the listings with NO coordinate at all — the ones that
    are missing from the map rather than merely imprecise on it. That is the queue the
    "ללא מיקום" sheet starts, because those are the flats you cannot even see."""
    rows = _rows()
    per_street: dict = {}
    for r in rows:
        if r.get("geo_confidence") in ("exact", "high") or r.get("manual_location"):
            continue
        if r.get("street"):
            per_street[r["street"]] = per_street.get(r["street"], 0) + 1
    out = []
    for r in rows:
        if r.get("geo_confidence") in ("exact", "high") or r.get("manual_location"):
            continue
        # NARROW THE QUEUE, NOT THE ARITHMETIC: `fixes` is counted over every imprecise
        # listing above, because a pin really does fix all of them. Filtering the rows
        # before that would have made each pin look worth less than it is.
        if unplaced_only and r.get("lat") is not None:
            continue
        if not (r.get("address") or "").strip():
            continue                       # nothing to propose and nothing to pin
        out.append({"key": r["dedup_key"], "address": r["address"],
                    "street": r.get("street") or "",
                    "fixes": per_street.get(r.get("street"), 1),
                    "numbered": bool(geocode._house_number(r["address"]))})
    # a numbered address on a busy street first: that is where one tap pays the most
    out.sort(key=lambda x: (-x["fixes"], not x["numbered"], x["address"]))
    return out


def _teach_anchor(address, lat: float, lon: float):
    """Turn a hand-placed listing into a street anchor. Returns 'street number' when the
    geocoder accepted it, else None (no house number, unknown street, or too far off)."""
    text = (address or "").strip()
    hn = geocode._house_number(text)
    if not text or not hn:
        return None
    for cand in geocode._candidate_tokens(text)[:2]:
        real, _how = streets.canonical(cand)
        if real and geocode.add_anchor(real, hn, lat, lon):
            return f"{real} {hn}"
    return None


def unrelocate(key: str) -> dict:
    """Drop a hand-placed location and let the geocoder decide again."""
    existed = storage.clear_manual_location(key)
    after = _regrade(key)
    return {"ok": True, "existed": existed, "after": after, "regraded": after is not None}


def _regrade(key: str):
    """Re-run the real classifier on one stored listing and persist the new verdict.

    Returns the new {tier, walk, score, status}, or None when the archived post isn't
    available to re-classify from (an old listing archived before post capture) — in
    which case the coordinates still stand, the grade is simply left alone rather
    than guessed at."""
    post = storage.post_for(key)
    if not post or not post.get("parsed_json"):
        return None
    try:
        import pipeline
        from models import ListingExtract
        e = ListingExtract.model_validate_json(post["parsed_json"])
        e = pipeline._postprocess_extract(e, post.get("raw_text") or "",
                                          post.get("comments") or "")
        images = json.loads(post["images"]) if post.get("images") else []
        # commit=False: persist and re-sort here, but never re-alert — the user is
        # looking at the listing, they don't need a Telegram message about it.
        res = pipeline._classify(e, post.get("raw_text") or "", post.get("source_url"),
                                 post.get("group"), images, None, commit=False)
        storage.save_listing(res)
    except Exception as exc:
        print(f"[dashboard] regrade of {key!r} failed: {exc}")
        return None
    # lat/lon travel back too: after an undo the page has to move the dot to wherever
    # the geocoder now says, or the map keeps showing the cleared manual point until
    # something else happens to refresh it.
    return {"location_tier": res.location_tier, "walk_minutes": res.walk_minutes,
            "eff_score": storage.effective_score(key, res.score or 0),
            "status": getattr(res.status, "value", str(res.status)),
            "lat": res.lat, "lon": res.lon, "geocode_source": res.geo_source,
            "geo_confidence": geocode.confidence(res.geo_source)}


def _legend_html() -> str:
    """What every symbol means, plus switches to turn the busier layers off.

    Written server-side rather than in JS because it is static text, and because a
    partner opening the shared file cold is exactly who needs it — it has to be in
    the markup even if the script never runs."""
    tc = map_listings._TIER_COLOR

    def sw(color: str, label: str) -> str:
        return (f'<div class="li"><span class="sw" style="background:{color}"></span>'
                f'{html.escape(label)}</div>')

    def ln(css: str, label: str) -> str:
        return (f'<div class="li"><span class="ln" style="{css}"></span>'
                f'{html.escape(label)}</div>')

    def lay(lid: str, label: str, on: bool) -> str:
        return (f'<label class="lay"><input type="checkbox" data-layer="{lid}"'
                f'{" checked" if on else ""}> {html.escape(label)}</label>')

    return f"""<details id="legend"><summary>מקרא ושכבות ▾</summary><div class="body">
<h4>נקודה = דירה</h4>
{sw(tc["GREEN"], "GREEN — בתוך האזור הירוק")}
{sw(tc["AMBER"], f'AMBER — עד {config.MAX_WALK_MINUTES} דק׳ הליכה לשער')}
{sw(tc["RED"], "RED — מחוץ לטווח")}
{sw(tc["UNKNOWN"], "לא זוהה מיקום")}
<div class="li muted">בבחירת צבע לפי ציון: אדום=0 → ירוק=100</div>
<div class="li"><span class="sw" style="background:{tc["GREEN"]}"></span>
  עיגול מלא — מספר בית מדויק</div>
<div class="li"><span class="sw" style="background:transparent;border:2px solid
  {tc["GREEN"]}"></span> עיגול חלול — מיקום משוער (מרכז רחוב/שכונה)</div>
<div class="li"><span class="sw" style="background:{tc["GREEN"]};box-shadow:0 0 0 2.4px
  #f5a623"></span> טבעת זהב — ⭐ שמור (גם על קבוצה שיש בה שמור)</div>
<div class="li"><span class="sw" style="background:{tc["GREEN"]};opacity:.4"></span>
  דהוי — 📵 כבר יצרתי קשר</div>
<div class="li muted">מספר בעיגול = כמה דירות שם. באותה נקודה בדיוק — לחיצה פורשת
  אותן, כי זום לא יפריד ביניהן.</div>
<h4>רקע</h4>
{ln("border-top-width:3px;border-color:#1b5e20", "האזור הירוק (מצויר ביד)")}
{ln("border-top-width:1.6px;border-color:#1a237e;border-top-style:dashed", "שכונות ב/ג/ד — הקבילות")}
{ln("border-top-width:1.2px;border-color:#8a94a6;border-top-style:dashed", "שכונות אחרות — לאוריינטציה בלבד")}
{ln("border-top-width:2.4px;border-color:#c8ccd2", "עורק ראשי · קו דק = רחוב פנימי")}
{sw("#3949ab", "אוניברסיטת בן גוריון")}
{sw("#ad1457", "סורוקה")}
<div class="li">★ שערי הקמפוס · 🚌 קו 669 · 🚆 לרכבת · 🏋️ חדר כושר</div>
<h4>שכבות</h4>
{lay("streets", "רחובות ושמותיהם", True)}
{lay("nbhd", "גבולות שכונות", True)}
{lay("amen", "סימוני תחבורה וחדר כושר", True)}
{lay("rings", f"טבעות הליכה 5/10/15/{config.MAX_WALK_MINUTES} דק׳ מהשערים", False)}
<h4>התנהגות</h4>
{lay("autofit", "התאם תצוגה לסינון", True)}
</div></details>"""


def render_live() -> str:
    """Served by serve_dashboard.py — data comes from /api/listings.json."""
    return render(live=True)


def build() -> str:
    """Write the offline copy. Same page, data inlined, no server required."""
    page = render(live=False)
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT}  ({page.count('dedup_key')} listing references)")
    return page


# ---------- installable, and readable with no signal ----------
# The published snapshot is now the everyday phone page (Tailscale was dropped), so it has
# to behave like an app: open full-screen from the home screen, and still show the last
# data in a lift or on a bus. The manifest and the worker are only emitted for the
# SNAPSHOT — the live page is a localhost/LAN tool and caching it would be the 22-hour
# stale-server trap all over again, but silent and on your phone.
PWA_ICON = ("data:image/svg+xml;utf8,"
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
            "<rect width='64' height='64' rx='12' fill='%230b6bcb'/>"
            "<circle cx='32' cy='26' r='9' fill='white'/>"
            "<path d='M32 58c-9-13-16-20-16-28a16 16 0 1 1 32 0c0 8-7 15-16 28z' "
            "fill='none' stroke='white' stroke-width='4'/></svg>")


def _manifest(stamp: str) -> str:
    return json.dumps({
        "name": "לוח דירות — BGU", "short_name": "דירות BGU",
        "start_url": "./", "scope": "./", "display": "standalone", "dir": "rtl",
        "lang": "he", "background_color": "#0f1115", "theme_color": "#0b6bcb",
        "icons": [{"src": PWA_ICON, "sizes": "any", "type": "image/svg+xml"}],
        "version": stamp,
    }, ensure_ascii=False)


def _service_worker(stamp: str) -> str:
    """Cache the shell so the page opens offline — but NEVER serve a stale page when the
    network is available. A PWA that pins itself to an old build is the same failure as
    the dashboard server that ran 22 hours on yesterday's code, except you cannot see it.
    So: network first, cache only as the fallback, and a versioned cache name so a new
    publish evicts the old one."""
    return f"""const CACHE = 'bgu-{stamp}';
self.addEventListener('install', e => {{ self.skipWaiting(); }});
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(
    ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {{
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request).then(r => {{
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy));
      return r;
    }}).catch(() => caches.match(e.request, {{ignoreSearch: true}}))
  );
}});
"""


def _pwa_head(snapshot: bool) -> str:
    if not snapshot:
        return ""          # the live page must never be cached — see _service_worker
    return """<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#0b6bcb">
<meta name="mobile-web-app-capable" content="yes">
<link rel="apple-touch-icon" href="icon.svg">
<script>
if ('serviceWorker' in navigator) addEventListener('load', () =>
  navigator.serviceWorker.register('sw.js').catch(() => {}));
</script>"""


def build_share():
    """A dated copy to send to the people flat-hunting with you.

    One self-contained file: no server, no account, no install — it opens on a phone
    from a Telegram chat. Contacts and WhatsApp links stay (the user's decision: the
    partners need to call the landlord too), so treat it like the phone numbers it
    contains and send it only to that group."""
    path = config.DATA_DIR / f"dashboard-{dt.date.today().isoformat()}.html"
    page = render(live=False, snapshot=True)
    path.write_text(page, encoding="utf-8")
    # The PWA companions ride alongside, stamped so a new publish evicts the old cache.
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    (config.DATA_DIR / "manifest.webmanifest").write_text(_manifest(stamp),
                                                          encoding="utf-8")
    (config.DATA_DIR / "sw.js").write_text(_service_worker(stamp), encoding="utf-8")
    (config.DATA_DIR / "icon.svg").write_text(
        PWA_ICON.split(",", 1)[1].replace("%23", "#"), encoding="utf-8")
    print(f"wrote {path}  ({len(page) // 1024} KB, "
          f"{page.count('dedup_key')} listing references) + manifest/sw/icon")
    return path


if __name__ == "__main__":
    if "--share" in sys.argv:
        shared = build_share()
        if "--publish" in sys.argv:
            import publish
            publish.publish(shared)
        if "--send" in sys.argv:
            import notifier
            import publish
            # Carry the LIVE URL in the caption, not just the file. The file is a
            # snapshot of one moment; the URL refreshes hourly and works from any
            # device with this PC off — and without this nothing ever told anyone it
            # exists.
            url = publish.pages_url()
            caption = f"לוח הדירות — צילום מצב {dt.date.today().isoformat()}"
            if url:
                caption += f"\n🔗 גרסה מתעדכנת: {url}"
            ok = notifier.send_document(shared, caption=caption)
            print("sent to Telegram" if ok else "NOT sent (check the token/chat id)")
        if "--open" in sys.argv:
            webbrowser.open(shared.as_uri())
    else:
        build()
        if "--open" in sys.argv:
            webbrowser.open(OUT.as_uri())
