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
import storage

OUT = config.DATA_DIR / "dashboard.html"

_SQL = """
    SELECT l.dedup_key, l.status, l.location_tier, l.score, l.price_per_room,
           l.available_rooms, l.total_roommates, l.address, l.walk_minutes,
           l.lease_start, l.contact, l.source_url, l."group", l.floor, l.furnished,
           l.balcony, l.elevator, l.images, l.amenities, l.first_seen, l.summary,
           l.price_from_comment
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
            r["amenity_text"] = " · ".join(amenities.describe(json.loads(r["amenities"] or "{}")))
        except Exception:
            r["amenity_text"] = ""
        # coordinates for the map dot — geocode is cached, so this is a dict lookup
        coords = geocode.geocode_cached(r["address"])
        r["lat"], r["lon"] = (coords if coords else (None, None))
        r["photos"] = _photo_hashes(r["images"])
        r["breakdown"] = _breakdown_for(r)
    rows.sort(key=lambda r: r["eff_score"], reverse=True)
    return rows


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
            "broker", "note", "post_text", "lat", "lon", "photos", "breakdown")}
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
     position:relative;height:min(78vh,900px);background:#f6f7f9;touch-action:pan-y}
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
#boxchip{position:absolute;inset-inline-start:8px;top:8px;z-index:15;font-size:12px;
       background:var(--accent);color:#fff;border-radius:99px;padding:2px 6px 2px 10px}
#boxchip button{background:none;border:0;color:#fff;cursor:pointer;font-size:12px;
       padding:0 3px}
#boxrect{fill:#3367d6;fill-opacity:.13;stroke:#3367d6;stroke-width:1.4;
       stroke-dasharray:5,4;vector-effect:non-scaling-stroke;pointer-events:none}
.dot{cursor:pointer}.dot.hi{stroke:#111;stroke-width:2.5}
.cl{cursor:zoom-in}.cl text{pointer-events:none;user-select:none}
.muted{color:var(--mut)}a{color:var(--accent)}
.count{color:var(--mut);font-size:12px;margin:6px 2px}
.panel{border:1px solid var(--line);border-radius:8px;padding:10px;margin:10px 0;
       background:var(--card);display:none}
.panel.on{display:block}
.panel table{min-width:0}
#lb{position:fixed;inset:0;background:#000c;display:none;align-items:center;
    justify-content:center;z-index:50}
#lb.on{display:flex}#lb img{max-width:94vw;max-height:90vh;border-radius:6px}
.live{font-size:11px;color:var(--mut)}
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
const TIER_COLOR = {GREEN: '#2e7d32', AMBER: '#e08e0b', RED: '#c0392b', UNKNOWN: '#7f8c8d'};
function scoreColor(s){
  const t = Math.max(0, Math.min(100, s || 0)) / 100;
  return 'hsl(' + Math.round(t * 130) + ' 65% ' + (42 + (1 - t) * 8) + '%)';
}
function esc(s){ const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

function passes(r){
  const text = $('q').value.trim().toLowerCase();
  if (text){
    const hay = [r.address, r.contact, r.summary, r.amenity_text, r.note, r.post_text]
      .join(' ').toLowerCase();
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
  const thumb = (r.photos && r.photos.length)
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
    '<td class="act"><button data-act="save">⭐</button>' +
      '<button data-act="dismiss">🗑</button>' +
      '<button data-act="contacted">📵</button>' +
      '<button data-act="exp">▾</button>' +
      '<input type="checkbox" data-act="cmp" title="השווה"></td></tr>';
}

function expandHtml(r){
  const chips = (r.breakdown || []).map(p =>
    '<span class="chip ' + (p[1] >= 0 ? 'pos' : 'neg') + '">' + esc(p[0]) + ' ' +
    (p[1] > 0 ? '+' : '') + p[1] + '</span>').join('');
  const gallery = (r.photos || []).map(h =>
    '<img class="thumb" loading="lazy" src="/img/' + h + '?token=' + TOKEN +
    '" onerror="this.style.display=\'none\'">').join(' ');
  return '<tr class="exp" data-key="' + esc(r.dedup_key) + '"><td colspan="13">' +
    '<div class="chips">' + chips + '</div>' +
    (gallery ? '<div style="margin:6px 0">' + gallery + '</div>' : '') +
    (r.post_text ? '<div class="raw">' + esc(r.post_text) + '</div>'
                 : '<div class="muted">הפוסט המקורי לא נשמר</div>') +
    '<textarea class="note">' + esc(r.note || '') + '</textarea>' +
    '<div><button data-act="savenote">שמור הערה</button> ' +
    '<span class="live" data-note-status></span></div></td></tr>';
}

/* ---------- zoom & pan: animate the viewBox, no library ----------
   The map is the main view now, so it has to zoom: אברהם אבינו alone holds 21
   listings, an unreadable blob at full extent. Strokes are non-scaling (CSS), and
   dots and labels are counter-scaled here so only the geography grows. */
const WORLD = {x: 0, y: 0, w: (P ? P.w : 1000), h: (P ? P.h : 820)};
let view = {...WORLD};
const zoomFactor = () => WORLD.w / view.w;
let lastRows = [], lastZoom = 0;

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
  svg.querySelectorAll('.dot').forEach(d => d.setAttribute('r', (5 / k).toFixed(2)));
  svg.querySelectorAll('.slabel').forEach(t => {
    if (!t.dataset.fs) t.dataset.fs = t.getAttribute('font-size') || '11';
    t.setAttribute('font-size', (+t.dataset.fs / k).toFixed(2));
    // reveal names as they become legible: arteries at 1x, side streets once you're
    // zoomed in among them (see map_listings.street_labels_svg)
    const mz = +t.dataset.minzoom || 1;
    t.style.display = (k >= mz) ? '' : 'none';
  });
  // clusters are a function of zoom, so they have to be rebuilt when it changes —
  // but not while panning, which would redraw 350 nodes on every pointermove
  if (Math.abs(Math.log(k / (lastZoom || k))) > 0.01 || !lastZoom){
    lastZoom = k;
    drawDots(lastRows);
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
     first time this was tried. Same reason `touch-action: pan-y` in the CSS: one
     finger scrolls the page, two fingers work the map. */
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
    if (pts.size === 1 && !ev.target.closest('.dot')){
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
  // a cluster badge zooms to the flats hiding inside it
  svg.addEventListener('click', ev => {
    const grp = ev.target.closest('.cl');
    if (grp && grp.__rows) fitTo(grp.__rows);
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
  for (const b of bins.values()){ b.x /= b.rows.length; b.y /= b.rows.length; }
  return [...bins.values()];
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
  const fill = r => byScore ? scoreColor(r.eff_score)
                            : (TIER_COLOR[r.location_tier] || TIER_COLOR.UNKNOWN);
  let n = 0, clusters = 0;
  for (const b of clusterOf(rows)){
    n += b.rows.length;
    if (b.rows.length === 1){
      const r = b.rows[0];
      const c = el('circle');
      c.setAttribute('cx', b.x.toFixed(1));
      c.setAttribute('cy', b.y.toFixed(1));
      c.setAttribute('r', (5 / k).toFixed(2));       // constant on screen under zoom
      c.setAttribute('class', 'dot');
      c.setAttribute('fill', fill(r));
      c.setAttribute('fill-opacity', '0.85');
      c.setAttribute('stroke', '#fff');
      c.setAttribute('stroke-width', '1');
      c.dataset.key = r.dedup_key;
      const t = el('title');
      t.textContent = (r.address || '—') + ' | ' + r.location_tier + ' | ⭐' + r.eff_score +
                      (r.price_per_room ? ' | ' + r.price_per_room + '₪' : '');
      c.appendChild(t);
      g.appendChild(c);
      continue;
    }
    clusters++;
    // one badge for the group, coloured by its best listing so a cluster hiding a
    // strong match doesn't read as background noise
    const best = b.rows.reduce((a, r) => (r.eff_score > a.eff_score ? r : a), b.rows[0]);
    const grp = el('g');
    grp.setAttribute('class', 'cl');
    // the rows themselves, not a joined list of keys — a dedup_key is phone|address
    // and an address may contain a comma, which a split(',') would tear in half
    grp.__rows = b.rows;
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
    ttl.textContent = b.rows.length + ' דירות כאן — לחיצה מתקרבת';
    grp.append(c, t, ttl);
    g.appendChild(grp);
  }
  svg.appendChild(g);
  $('mapcount').textContent = n + ' על המפה' +
    (clusters ? ' · ' + clusters + ' קבוצות' : '');
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
  if (cursor >= rows.length) cursor = Math.max(0, rows.length - 1);
  markCursor();
}

const rowByKey = k => DATA.find(r => r.dedup_key === k);

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

['q', 'st', 'tier', 'maxp', 'minr', 'stale', 'nobroker', 'saved', 'onlynew']
  .forEach(id => {
    const node = $(id);
    if (!node) return;
    const ev = (node.type === 'checkbox' || node.tagName === 'SELECT') ? 'change' : 'input';
    node.addEventListener(ev, afterFilter);
  });
$('bycolor').addEventListener('change', render);

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
  const photo = (r.photos && r.photos.length)
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
  const votes = window.__LIVE__
    ? '<button data-card="save" title="שמור">⭐</button>' +
      '<button data-card="dismiss" title="הסתר">🗑</button>' +
      '<button data-card="contacted" title="יצרתי קשר">📵</button>' : '';
  return '<div class="hd">' + photo + '<div><div class="ttl">' + esc(r.address || '—') +
      ' <span class="pill ' + esc(r.location_tier || 'UNKNOWN') + '">' +
      esc(r.location_tier || '?') + '</span></div>' +
    '<div class="kv">⭐' + fmt(r.eff_score) + ' · ' + esc(bits) +
      (flags ? ' · ' + esc(flags) : '') + '</div></div></div>' +
    (r.amenity_text ? '<div class="am">' + esc(r.amenity_text) + '</div>' : '') +
    '<div class="btns">' + btn(r.wa, '💬', 'וואטסאפ') + btn(r.source_url, '🔗', 'הפוסט') +
      dirs + walk + votes + '</div>';
}

function showCard(r, dot){
  clearTimeout(hideTimer);
  cardKey = r.dedup_key;
  $('cardbody').innerHTML = cardHtml(r);
  const card = $('card');
  card.classList.add('on');
  if (CAN_HOVER && dot){
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

function hideCard(){ $('card').classList.remove('on'); cardKey = null; }

function initCard(){
  const svg = document.querySelector('.map svg');
  if (!svg) return;
  if (CAN_HOVER){
    svg.addEventListener('mouseover', ev => {
      const dot = ev.target.closest('.dot');
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
  } else {
    svg.addEventListener('click', ev => {
      const dot = ev.target.closest('.dot');
      if (!dot){ hideCard(); return; }
      const r = rowByKey(dot.dataset.key);
      if (r) showCard(r, dot);
    });
  }
  $('card').querySelector('.x').addEventListener('click', hideCard);
  $('card').addEventListener('click', async ev => {
    const b = ev.target.closest('[data-card]');
    if (!b || !cardKey) return;
    const act = b.dataset.card;
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
    '</table><button id="route">תכנן מסלול צפייה</button> ' +
    '<span id="routeout" class="live"></span>';
  p.classList.add('on');
  $('route').onclick = async () => {
    const out = await post('/api/route', {keys: picked.map(r => r.dedup_key)});
    $('routeout').textContent = out
      ? out.order.map((k, i) => (i + 1) + '. ' + ((rowByKey(k) || {}).address || k)).join('  →  ') +
        (out.estimated ? '  (הערכה — OSRM כבוי)' : '')
      : 'לא זמין';
  };
}

async function poll(){
  try {
    const v = await (await fetch('/api/version?token=' + encodeURIComponent(TOKEN))).json();
    const sig = JSON.stringify(v);
    if (sig !== window.__VER__){
      window.__VER__ = sig;
      const d = await (await fetch('/api/listings.json?token=' + encodeURIComponent(TOKEN))).json();
      DATA = d.listings;
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
    return {"order": order, "estimated": estimated}


def _json_for_script(obj) -> str:
    """JSON safe to embed inside a <script> block.

    json.dumps escapes quotes and backslashes but NOT '<', so an address or post body
    containing "</script>" would close the tag and everything after it would be parsed
    as HTML — a script-injection hole, and this data comes straight from Facebook posts.
    Escaping < > & as \\uXXXX is invisible to JSON.parse and closes it."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def render(live: bool) -> str:
    """The dashboard page. `live=True` fetches from the server and can vote/note;
    `live=False` inlines the data so the file works offline with no server at all."""
    rows = _rows()
    placed = [(r["lat"], r["lon"]) for r in rows if r["lat"] is not None]
    base_svg, projection = map_listings.build_base_svg(
        [(la, lo, None, None, None, None, None, None) for la, lo in placed])
    payload = [] if live else rows_for_api()
    matches = sum(1 for r in rows if r["status"] == "MATCH")
    mode = ("מתעדכן אוטומטית" if live
            else "קובץ סטטי — הרץ serve_dashboard.py לעדכון חי ולגישה מהנייד")

    head = "".join(f'<th data-k="{k}">{html.escape(h)}</th>' for h, k in _HEAD)
    return f"""<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">
<title>BGU housing dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_CSS}{map_listings.STREET_CSS}</style>
<div class="wrap">
<h1>לוח דירות — BGU</h1>
<p class="sub">{len(rows)} רשומות · {matches} התאמות · {len(placed)} על המפה ·
  <span id="newcount"></span><span class="live">{mode}</span></p>
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
  <label>צבע <select id="bycolor"><option value="tier">אזור</option>
    <option value="score">ציון</option></select></label>
</div>
<div class="count"><span id="n"></span> · <span id="mapcount"></span></div>
<div class="panel" id="cmp"></div>
<div class="map">{base_svg}</svg>
  <div class="mapbtns">
    <button id="fitbtn" title="התאם את התצוגה לדירות המסוננות">התאם</button>
    <button id="boxbtn" title="סימון אזור: Shift+גרירה, או לחצו כאן ואז גררו">▭ אזור</button>
    <button id="reset" title="איפוס תצוגה">איפוס</button>
  </div>
  <div id="boxchip" style="display:none">אזור מסומן
    <button id="boxclear" title="בטל את סימון האזור">✕</button></div>
  {_legend_html()}
  <div class="maphint">Ctrl+גלגלת או צביטה לזום · גרירה להזזה · ריחוף/נגיעה בנקודה לפרטים</div>
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
window.__PROJ__ = {json.dumps(projection)};
window.__LIVE__ = {str(bool(live)).lower()};
window.__POLL__ = {config.DASHBOARD_POLL_SECONDS};
</script>
<script>{_JS}</script>
</html>"""


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


if __name__ == "__main__":
    build()
    if "--open" in sys.argv:
        webbrowser.open(OUT.as_uri())
