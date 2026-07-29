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
import html
import json
import sys
import webbrowser
from datetime import datetime

import amenities
import config
import map_listings
import storage

OUT = config.DATA_DIR / "dashboard.html"

_SQL = """
    SELECT l.dedup_key, l.status, l.location_tier, l.score, l.price_per_room,
           l.available_rooms, l.total_roommates, l.address, l.walk_minutes,
           l.lease_start, l.contact, l.source_url, l."group", l.floor, l.furnished,
           l.balcony, l.amenities, l.first_seen, l.summary
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
    for r in rows:
        key = r["dedup_key"]
        # the effective score is what the bot actually ranks by: quality + group votes
        r["eff_score"] = storage.effective_score(key, r["score"] or 0)
        r["saved"] = key in saved
        r["contacted"] = key in contacted
        r["stale"] = key in stale
        r["broker"] = storage.phone_listing_count(r["contact"])
        try:
            r["amenity_text"] = " · ".join(amenities.describe(json.loads(r["amenities"] or "{}")))
        except Exception:
            r["amenity_text"] = ""
    rows.sort(key=lambda r: r["eff_score"], reverse=True)
    return rows


def _wa_link(contact) -> str:
    """Reuse the notifier's rule so the dashboard's contact link behaves like the
    button in Telegram."""
    import notifier
    return notifier._contact_link(contact) or ""


_CSS = """
:root{--fg:#1c2024;--mut:#667;--line:#e3e6ea;--bg:#fff;--card:#fafbfc}
@media (prefers-color-scheme:dark){
  :root{--fg:#e6e8ea;--mut:#98a2ad;--line:#2a2f36;--bg:#14171a;--card:#1b1f24}}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 system-ui,Segoe UI,Arial;color:var(--fg);background:var(--bg)}
.wrap{max-width:1400px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mut);margin:0 0 14px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px;
     padding:10px;background:var(--card);border:1px solid var(--line);border-radius:8px}
.bar label{color:var(--mut);font-size:12px}
input,select{font:inherit;padding:5px 7px;border:1px solid var(--line);border-radius:6px;
             background:var(--bg);color:var(--fg)}
input[type=search]{min-width:190px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:1050px}
th,td{padding:7px 9px;text-align:right;border-bottom:1px solid var(--line);
      white-space:nowrap;vertical-align:top}
th{position:sticky;top:0;background:var(--card);cursor:pointer;user-select:none;font-size:12px}
th:hover{color:#3367d6}
td.addr{white-space:normal;min-width:190px}
td.am{white-space:normal;min-width:230px;color:var(--mut);font-size:12px}
tr:hover td{background:var(--card)}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;font-weight:600}
.GREEN{background:#e5f4e6;color:#1e6b23}.AMBER{background:#fdf1dc;color:#8a5a06}
.RED{background:#fbe6e4;color:#a3271c}.UNKNOWN{background:#eceff2;color:#5b666f}
@media (prefers-color-scheme:dark){
 .GREEN{background:#183a1c;color:#8fd694}.AMBER{background:#3d2f11;color:#e5bb6a}
 .RED{background:#3d1a17;color:#f0a79d}.UNKNOWN{background:#262b31;color:#9fb0bd}}
.map{margin:16px 0;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.map svg{display:block;width:100%;height:auto}
.muted{color:var(--mut)}
a{color:#3367d6}
.count{color:var(--mut);font-size:12px;margin:8px 2px}
"""

_JS = """
const rows=[...document.querySelectorAll('#t tbody tr')];
const q=document.getElementById('q'), st=document.getElementById('st'),
      tier=document.getElementById('tier'), maxp=document.getElementById('maxp'),
      minr=document.getElementById('minr'), hideStale=document.getElementById('stale'),
      hideBroker=document.getElementById('nobroker'), onlySaved=document.getElementById('saved');
function apply(){
  const text=q.value.trim().toLowerCase();
  let shown=0;
  for(const r of rows){
    const d=r.dataset;
    let ok=true;
    if(text && !r.textContent.toLowerCase().includes(text)) ok=false;
    if(ok && st.value && d.status!==st.value) ok=false;
    if(ok && tier.value && d.tier!==tier.value) ok=false;
    if(ok && maxp.value && (d.price==='' || +d.price>+maxp.value)) ok=false;
    if(ok && minr.value && (d.rooms==='' || +d.rooms<+minr.value)) ok=false;
    if(ok && hideStale.checked && d.stale==='1') ok=false;
    if(ok && hideBroker.checked && d.broker==='1') ok=false;
    if(ok && onlySaved.checked && d.saved!=='1') ok=false;
    r.style.display = ok?'':'none';
    if(ok) shown++;
  }
  document.getElementById('n').textContent = shown+' / '+rows.length+' listings';
}
[q,st,tier,maxp,minr,hideStale,hideBroker,onlySaved].forEach(el=>
  el.addEventListener(el.type==='checkbox'?'change':'input',apply));
// click a header to sort; click again to reverse
let lastCol=-1, dir=1;
document.querySelectorAll('#t th').forEach((th,i)=>th.addEventListener('click',()=>{
  dir = (i===lastCol) ? -dir : 1; lastCol=i;
  const num = th.dataset.num==='1';
  const body=document.querySelector('#t tbody');
  rows.sort((a,b)=>{
    const x=a.children[i].dataset.v ?? a.children[i].textContent;
    const y=b.children[i].dataset.v ?? b.children[i].textContent;
    if(num){ const nx=parseFloat(x)||-Infinity, ny=parseFloat(y)||-Infinity;
             return (nx-ny)*dir; }
    return x.localeCompare(y,'he')*dir;
  });
  rows.forEach(r=>body.appendChild(r));
}));
apply();
"""

_COLS = [
    ("ציון", "num"), ("סטטוס", "txt"), ("אזור", "txt"), ("מחיר", "num"),
    ("פנויים", "num"), ("שותפים", "num"), ("כתובת", "txt"), ("הליכה", "num"),
    ("כניסה", "txt"), ("קומה", "txt"), ("תחבורה ושירותים", "txt"),
    ("קשר", "txt"), ("מקור", "txt"),
]


def _cell(value, sort_value=None, cls="") -> str:
    v = "" if value is None else str(value)
    attr = f' data-v="{html.escape(str(sort_value))}"' if sort_value is not None else ""
    cl = f' class="{cls}"' if cls else ""
    return f"<td{cl}{attr}>{v}</td>"


def build() -> str:
    rows = _rows()
    svg, placed, unplaced = map_listings.build_svg()

    body = []
    for r in rows:
        tier = r["location_tier"] or "UNKNOWN"
        flags = []
        if r["saved"]:
            flags.append("⭐")
        if r["contacted"]:
            flags.append("📵")
        if r["stale"]:
            flags.append("🕒")
        if r["broker"] >= config.BROKER_MIN_LISTINGS:
            flags.append(f"⚠️{r['broker']}")
        addr = html.escape(r["address"] or "—")
        if r["source_url"]:
            addr = f'<a href="{html.escape(r["source_url"])}" target="_blank">{addr}</a>'
        if flags:
            addr += ' <span class="muted">' + " ".join(flags) + "</span>"
        wa = _wa_link(r["contact"])
        contact = html.escape(r["contact"] or "—")
        if wa:
            contact = f'<a href="{html.escape(wa)}" target="_blank">{contact}</a>'
        walk = "" if r["walk_minutes"] is None else f'{r["walk_minutes"]:.0f}'
        body.append(
            "<tr data-status='{s}' data-tier='{t}' data-price='{p}' data-rooms='{rm}' "
            "data-stale='{sl}' data-broker='{bk}' data-saved='{sv}'>".format(
                s=r["status"] or "", t=tier, p=r["price_per_room"] or "",
                rm=r["available_rooms"] or "", sl=int(bool(r["stale"])),
                bk=int(r["broker"] >= config.BROKER_MIN_LISTINGS), sv=int(bool(r["saved"])))
            + _cell(f'<b>{r["eff_score"]}</b>', r["eff_score"])
            + _cell(html.escape(r["status"] or ""))
            + _cell(f'<span class="pill {tier}">{tier}</span>', tier)
            # unknowns sort LAST ascending (a blank price is not "the cheapest"),
            # same convention as the walk column below
            + _cell(r["price_per_room"], r["price_per_room"] or 10 ** 6)
            + _cell(r["available_rooms"], r["available_rooms"] or 0)
            + _cell(r["total_roommates"], r["total_roommates"] or 0)
            + _cell(addr, r["address"] or "", cls="addr")
            + _cell(walk, r["walk_minutes"] if r["walk_minutes"] is not None else 999)
            + _cell(html.escape(r["lease_start"] or ""))
            + _cell(html.escape(str(r["floor"] or "")))
            + _cell(html.escape(r["amenity_text"]), cls="am")
            + _cell(contact)
            + _cell(html.escape((r["group"] or "").split("/")[-1][:14]))
            + "</tr>")

    head = "".join(f'<th data-num="{1 if k == "num" else 0}">{h}</th>' for h, k in _COLS)
    matches = sum(1 for r in rows if r["status"] == "MATCH")
    page = f"""<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">
<title>BGU housing dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_CSS}</style>
<div class="wrap">
<h1>לוח דירות — BGU</h1>
<p class="sub">{len(rows)} רשומות · {matches} התאמות · {len(placed)} על המפה ·
   {unplaced} ללא מיקום · עודכן {datetime.now():%Y-%m-%d %H:%M}</p>
<div class="bar">
  <input id="q" type="search" placeholder="חיפוש חופשי…">
  <label>סטטוס <select id="st"><option value="">הכל</option>
    <option>MATCH</option><option>NEEDS_DATA</option></select></label>
  <label>אזור <select id="tier"><option value="">הכל</option>
    <option>GREEN</option><option>AMBER</option><option>RED</option>
    <option>UNKNOWN</option></select></label>
  <label>מחיר עד <input id="maxp" type="number" style="width:88px" placeholder="₪"></label>
  <label>חדרים מ־ <input id="minr" type="number" style="width:64px"></label>
  <label><input id="stale" type="checkbox"> להסתיר ישנות</label>
  <label><input id="nobroker" type="checkbox"> בלי מתווכים</label>
  <label><input id="saved" type="checkbox"> ⭐ בלבד</label>
</div>
<div class="count" id="n"></div>
<div class="scroll"><table id="t"><thead><tr>{head}</tr></thead>
<tbody>{"".join(body)}</tbody></table></div>
<div class="map">{svg}</div>
<p class="sub">⭐ שמור · 📵 יצרתי קשר · 🕒 ישן מ־{config.LISTING_STALE_DAYS} ימים ·
   ⚠️ מתווך (מספר דירות). לחיצה על כותרת ממיינת.</p>
</div>
<script>{_JS}</script>
</html>"""
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT}  ({len(rows)} listings, {matches} matches)")
    return page


if __name__ == "__main__":
    build()
    if "--open" in sys.argv:
        webbrowser.open(OUT.as_uri())
