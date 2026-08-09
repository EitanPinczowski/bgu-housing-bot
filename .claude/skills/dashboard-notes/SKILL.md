---
name: dashboard-notes
description: >
  Reference notes on the browse-by-hand view: the SVG map, projection, clustering and
  fan-out, zoom/pan performance, layers and dark theme, tokens and the image proxy, the
  PWA snapshot and publishing. Load before editing dashboard.py, serve_dashboard.py,
  map_listings.py, area_map.py, or publish.py.
---

# Dashboard Notes

How the map and the table are rendered and served, and the many things that looked right on a desktop and were broken on a phone.

> Moved verbatim from `CLAUDE.md`. Do not reword in place — see the
> `write-a-note` skill.

- `dashboard.py` / `serve_dashboard.py` — the browse-by-hand view. The table and the
  map dots are rendered IN THE BROWSER from one JSON payload, so filtering moves the
  dots too; the backdrop (zone/gates/amenity pins) comes from `map_listings.build_base_svg`
  and both sides project through the same `xy_from` params. `dashboard.py` writes a
  self-contained offline `data/dashboard.html`; `serve_dashboard.py` serves the same page
  live from SQLite, which is the only way to poll, vote, or open it on a phone.
  - **A token is required on every route** — the page shows landlords' phone numbers and
    addresses. `DASHBOARD_TOKEN` in `.env`, else generated into
    `data/dashboard_token.txt`. Don't add an unauthenticated mode.
  - **The live server is a HOME tool; the published snapshot is the phone.** Tailscale was
    removed at the user's request (2026-08-02). Live = this PC or the same Wi-Fi, and it
    is where writes happen (votes, notes, 📍 pinning) because they need the DB. Away from
    home there is deliberately no live route — no tunnel, no VPN. The published page is
    the answer and it is a **PWA**: `dashboard.build_share` emits `manifest.webmanifest`,
    `sw.js` and `icon.svg` beside the snapshot and `publish.py` ships them, so it installs
    to a home screen and opens with no signal. The worker is **network-first** with a
    per-publish cache stamp — a cache-first PWA would be the 22-hour stale-server trap
    again, silently and on a phone. **The live page emits neither** (`_pwa_head(snapshot)`
    returns "" when live) for exactly that reason.
  - The image proxy serves **only URLs already in the DB**, keyed by hash — never a URL
    from the request, or it becomes an open relay. It caches to `data/images/` because
    Facebook URLs expire (only 8 of 350 listings have permanent Telegram file_ids).
  - Untrusted post text rides inside a `<script>` block, so it goes through
    `dashboard._json_for_script` (escapes `<`); `json.dumps` alone would let
    `</script>` in an address break out.
  - Viewers use `geocode.geocode_cached` — never the network. Going through the full
    geocoder took **211 s** to render 350 rows.
  - **The map is the page**, the table is behind a `<details>`. Zoom/pan animate the
    `viewBox`; strokes are non-scaling and dots/labels counter-scale, so only the
    geography grows. Zoom is **Ctrl/⌘+wheel only** — a plain wheel must scroll the
    page, or you can't reach the list under a 78vh map. Dots within ~19 px collapse
    into a counted badge (`clusterOf`) rebuilt on zoom change, not on pan.
  - **`python dashboard.py --share`** writes a dated `data/dashboard-YYYY-MM-DD.html`
    for the people flat-hunting with the user. `window.__SNAPSHOT__` **removes** every
    write control (a disabled button that alerts "unavailable" is worse) and the
    `/img` tags, and adds a dated banner. Contacts and WhatsApp links **stay** — the
    partners need to call the landlord (user's decision) — which is why
    `notifier.send_document` defaults to the **group**, never `all`. `--send` posts it;
    `BGU Dashboard Share` runs it daily at 21:00.
  - `POST /api/walk` (not `GET /api/walk/<key>`): a `dedup_key` is `phone|address`, so
    a GET would put a landlord's phone number in the URL and the access log. Same for
    `POST /api/locate`. Draws the real OSRM path (`osrm.foot_geometry`, GeoJSON — no
    polyline decoding). **No straight-line fallback**: there is no honest estimate of
    *which way* you walk. One `table_minutes` call picks the gate, then ONE geometry
    call — it used to be one geometry call per gate, i.e. 4× 15 s of timeouts to
    discover the router was down. Optional `dest` routes to an amenity instead.
  - **Touch: one finger pans, two pinch.** `touch-action` must be on `.map svg`, not
    just `.map` — it does NOT inherit, and the pointer handlers live on the svg. With
    `pan-y` there the browser scrolled the page *while* we panned the map: the
    "glitchy on phone" bug. Card buttons are 40px under `@media (pointer:coarse)`.
  - **`click` must always be bound.** It lived in the `else` of `if (CAN_HOVER)`, so on
    every desktop and many touch laptops clicking a dot did nothing — the "I can't press
    on a listing" report. Hover is an addition for pointer devices, never the only way
    in. Each dot also carries an invisible `.hit` circle sized in **real screen pixels**
    (`HIT_RADIUS_PX * unitsPerPx`): a dot is 4px, and sizing the target in viewBox units
    looked right but measured 8px, because the map renders ~0.36px per unit.
  - **A badge must be as easy to hit as a dot, and stacks open themselves.** 223 of 229
    listings sat inside a badge and the badges had **zero** hit targets between them
    (4.1 px circles) while every single dot got `HIT_RADIUS_PX` of padding — that is
    "clusters aren't clickable". Pressing one also started a pan and captured the
    pointer, which retargets the following `click` to the `<svg>` where `closest('.cl')`
    is null, so `.cl` now joins `.dot` in the pointerdown exclusion. Past
    `AUTO_FAN_ZOOM` (6×) every `sameSpot` stack **in the viewport** fans open by itself —
    viewport-limited because 38 stacks × 19 dots would otherwise be rebuilt on every
    redraw and `drawDots` has no culling. A group too tight for max zoom to split
    (`__tight`) fans instead of zooming, because `fitTo` clamps at 8× while `applyView`
    allows 12× and the badge could otherwise be clicked forever doing nothing. Measured
    in-viewport: at 4× 80% of visible listings are still badged, at **6× it is 3%**, at
    8× zero.
  - **PANNING MUST NOT DO ZOOM WORK.** `applyView` ran on every pointermove and
    re-queried + rewrote every `.dot` radius and all 264 `.slabel` font-sizes — all of
    which divide by the zoom factor, which a pan does not change. It now sits behind the
    existing `lastZoom` guard in `rescaleForZoom`, and the label NodeList is cached
    (static backdrop markup). Measured: pan **0.24 → 0.005 ms** at 1×, **0.38 → 0.093 ms**
    at 8×. `drawDots` also culls to the viewport + 0.6 screens, redrawing only when the
    window leaves the box it was drawn for: **260 → 108** nodes at 8×, and panning the
    full width in 28 steps costs 9 redraws with 0 groups left unrendered.
    - **Culling must never change what the page CLAIMS, only what it draws.** Counting in
      both the total loop and the render loop made the counter read "556 על המפה" against
      456 listings. One accumulator, over `allGroups`; there is a test.
    - **A map laid out at ZERO width cannot be clustered** — every px size divides by it
      and the `|| 1` fallbacks make one cluster cell 19,000 world units, collapsing all
      312 listings into a single badge that reads like data loss. `drawDots` keeps the
      last good drawing and waits. Nothing listened for `resize` at all before, so cluster
      cells, hit targets, halos and the scale bar were stale after a phone rotation.
  - **The dot carries two extra channels and only two** — ⭐ saved as a gold ring,
    📵 contacted at 40% opacity. It already encodes tier as fill and precision as
    hollow/solid; at ~4px on a phone, more is mud. Broker/stale/notes/photos have filters
    and card lines and stay there.
    - **A BADGE carries the ring too.** At 1×, **253 of 312** listings sit inside a badge,
      so a ring on lone dots alone helps in 19% of cases and is missing from exactly the
      flats you cannot pick out by eye.
    - **Sized in SCREEN px, not viewBox units** — the `mkHit` trap again. In world units
      the ring measured 7px around a 4px dot at 375px wide while looking fine on a
      desktop. Now 14px (dot) / 20px (badge) on every screen.
    - `.dot.hi` must not set `stroke:#111`: it repainted an approx dot's tier-coloured
      outline and destroyed the precision signal at the moment you were looking closely.
  - **The map NAMES what it is not showing.** A listing with no coordinate used to just
    vanish, and the two counters disagreed silently — 84 of 396, 21%. The counter now
    reads `N מתוך M על המפה · K ללא מיקום`, and K opens a sheet listing them, each row
    opening its card. 🎯 there starts the pin flow via `pin_worklist(unplaced_only=True)`.
    - `fixes` is still counted over EVERY imprecise listing: a pin on בזל fixes the
      placed-but-vague ones too, so narrowing the rows before counting would understate
      each pin. The queue narrows; the arithmetic does not.
    - The button says **61**, not 83 — the other 22 have no address for govmap to answer.
  - **GREEN|AMBER|RED is drawn as a field** (`map_listings.tier_field_svg`), sampled from
    `zones.classify_effective` itself. NOT an analytic mask of gate circles minus green
    minus no-amber: `classify_effective` also reds out anything outside ב/ג/ד, so a mask
    would paint AMBER where a listing is classified RED. Sampling the classifier cannot
    disagree with it; a test asserts pixel identity over every sample cell.
    - **Run-length merging is what let it ship on by default**: 2,643 rects per-cell →
      **159** merged (−94%), 186 rects / 12.4 KB / 0.9% of the snapshot. The gate was
      ≤600 nodes / 40 KB. Cells are uniform and axis-aligned so the merge is exact.
    - It goes **above the background and below the streets** — it is a wash. The green
      polygon's own 10% fill drops to 0 while it is on (two washes are mud).
    - `latlon_from` (the Python twin of the page's `unproject`) exists because the field
      must cover the WHOLE canvas: `_bounds_of` excludes the pad and `scale = min(...)`
      leaves slack on one axis, so a grid over those bounds leaves an unpainted frame.
    - **The band is the straight-line estimate; a dot's own tier uses OSRM.** Near the
      edge they can disagree, and the dot is the more accurate one. Said in the legend.
  - **The שכונה ד' carve-out is outlined** (`.noamber`, from `zones._no_amber_polys`). It
    is a RULE, not a walk-time consequence, and was drawn nowhere in either map — so a
    flat 7 minutes from a gate could be RED with nothing on screen to explain it. `קדש 26`
    flipped MATCH→DROP for exactly this reason in the 2026-08-02 replay.
  - **The map has a dark theme** (`prefers-color-scheme` in `STREET_CSS`). It had none at
    all while the page has had one since D4, so at night it was a white slab in a dark
    page. The background is a CLASS so CSS beats the presentation attribute; tier hues are
    **dimmed, not inverted** — at .19 opacity the light ones glow on dark, so dark lifts
    them to .26.
  - **Gates are a layer** with a name revealed at 2.6×. They were a bare ★ with no class:
    the only untoggleable thing on the map and the only landmark with no name, despite
    every AMBER verdict being measured to one.
  - **A scale bar** (`drawScale`), because no zoom level said whether two dots were 100 m
    or 1 km apart. Reuses the page's own projection so it cannot drift from what is drawn,
    and rides the zoom guard — a bar is a function of scale, and a pan does not change
    scale. Measured against haversine: within **0.5%** at 1×/4×/8×. `units_per_metre` is
    shared with `walk_rings_svg`, which had been recomputing it inside the radius loop.
  - **📍 אני is client-only.** `watchPosition` → a marker plus an accuracy ring in REAL
    METRES (a 40 m fix draws 40 m), and on the first fix the list orders by distance from
    you, restoring the previous sort on the way out. The position is never sent, never
    stored — which is also why it works on the published snapshot. **Geolocation needs a
    secure context**: the https page and 127.0.0.1 qualify, the plain-http LAN URL does
    not, and the error says so.
  - **The view is remembered and shareable** (`bgu.view` + a `#v=x,y,w` hash). Layers have
    persisted since F4; the view never did, so the PWA reopened on the whole city. The
    hash wins over localStorage — it is what somebody deliberately sent you.
  - **A dot says how much to trust it.** `geocode_source` → `geocode.confidence()`;
    `street`/`none` draw HOLLOW. Only 45% of listings have a house number; 41% are a
    street/neighbourhood centroid. That is why 282 mapped listings sit on **105
    distinct coordinates** — so a badge over a *stack* (one shared point, up to 19
    flats) **fans out** rather than zooming, because zoom can never separate identical
    coordinates. A badge over a genuine spread still zooms. Don't "simplify" these
    back into one behaviour.
  - **📍 place-mode corrects a location, and it is authoritative.**
    `storage.manual_locations` (keyed by dedup_key) is preferred by
    `pipeline._classify` — the same funnel `replay.py` re-runs — so a correction
    survives `replay --apply`. `"manual"` is registered in `geocode._PRECISE_SOURCES`
    as `exact`. Saving re-grades the listing through the real classifier and reports
    tier/walk/score before→after: moving a dot changes the verdict, and that must not
    be silent. Scope "this address" instead calls the existing `geocode.add_pin` +
    `uncache`, which fixes every current and future listing there.
  - **🎯 guided pinning: govmap PROPOSES, a person decides.** `pin_worklist()` orders the
    imprecise listings by how many flats one pin would fix (a numbered pin becomes a
    street anchor); `POST /api/propose` → `propose_location()` asks govmap and returns a
    candidate, writing NOTHING. The candidate draws as a dashed ring, never a dot, so an
    unconfirmed guess cannot be mistaken for a saved location. Accept commits through the
    existing `relocate()`; "סימון ידני" falls through to tap-to-place; skip moves on.
    - **Never auto-accept, and show govmap's OWN wording** (`govmap.address_detail`
      returns the matched text with the point). govmap substitutes silently — `בני אור 999`
      comes back as `בני אור 13` — and a person can only judge an answer they can read.
      The measured substitutions are rejected before display, and a candidate inside the
      no-housing mask is refused outright.
    - **The queue advances in `commitPlace`, not in `acceptPin`**, so correcting by hand
      moves on exactly like accepting does. Wired the other way, the manual path left the
      flow stuck on a flat it had just placed.
  - **The viewing-route planner** is the UI for `POST /api/route`, which had none for
    weeks: tick flats in compare → order, per-leg minutes, total, and the real OSRM path.
    Router down = "the order is an estimate" and **nothing drawn**; a straight line here
    crosses the railway, the same lie `drawWalk` already refuses to tell.
  - **Amenity pins are PER-LISTING.** The map-wide layer can only carry fixed
    landmarks; "a stop with a bus to the train station" matches 428 stops, so it is
    skipped by the `_MAX_PINS_PER_TARGET` guard and has no useful map-wide form.
    Opening a card pins that flat's own stops via `amenities.locate`, which resolves
    coordinates by (name, direction_id) out of `amenities.json` — matching on name
    alone put both directions of a junction on one pin. Still display-only.
- `publish.py` — pushes the dated snapshot to **GitHub Pages** so a URL exists when the
  PC is off (Tailscale and tunnels all need it awake; the scraper does too, so the
  hosted copy is a SNAPSHOT, never live). `SITE_REPO_URL` in `.env`, read at call time
  because `.env` is loaded per entry point. **It refuses to publish into the code repo**
  — that repo is public and git history is permanent, so ~350 landlords' phone numbers
  could never be taken back. One always-amended commit, force-pushed: a ~1 MB generated
  page four times a day would otherwise add ~365 MB/year of unread history. The page is
  public and unauthenticated by the user's explicit decision (2026-07-31);
  `PUBLISH_NOINDEX=1` only hides it from search. Unset `SITE_REPO_URL` = silent no-op.
- `map_listings.py` — the shared map renderer (dashboard + `area_map.py`): projection
  (`_projector`/`xy_from`), street geometry as **4 combined `<path>`s** not ~2,800
  polylines, zoom-revealed street labels (`data-minzoom`), landmarks, display-only
  neighborhood outlines, amenity pins, and `walk_rings_svg` (5/10/15/`MAX_WALK_MINUTES`
  from each gate, using the same arithmetic as `zones.est_walk_to_gate_min`).
  Layer switches are one class on the `<svg>`; each layer needs its **own** marker
  class (`st-l`, `nbhd`/`nbhd-abc`, `amen`, `lmk`) — a `:not()` chain once hid the campus label.
  - **The surveyed landmarks are DRAWN, at their measured extent** (`geocode.landmarks()`,
    a second source alongside `area_features.json`'s campus/Soroka). They steered geocoding
    while being invisible, so a dot inside `הבלוק` looked placed from nowhere; a 90 m
    outline and a 299 m one don't look alike, so the shape itself says how precisely a
    listing described that way is known. A missing `area_features.json` must not take them
    with it — different file, and there's a test.
  - **A `.no-X` CLASS IS ONLY HALF A LAYER — it needs a checkbox driving it.** `lmk`
    shipped with the switch class, the dark rule and a test asserting the CSS, and was
    still permanently on and unexplained, because nothing emitted `data-layer="lmk"` in
    `dashboard._legend_html`. That is the untoggleable-gates problem the marker-class rule
    exists to prevent, reintroduced by the rule's own wording. A test now walks every
    `.no-X` in `STREET_CSS` and demands a switch.
  - **ONE dark media query, not one per layer.** The `.lmk` rules opened a second
    `@media (prefers-color-scheme:dark)` earlier in `STREET_CSS`; the dark-theme test
    splits on the first occurrence, so it read the two-line landmark block as the whole
    theme and reported `.mapbg` unthemed. Dark overrides go in the existing block.
  - **A STREET'S PROMINENCE IS THE TOTAL POLYLINE IT DRAWS**, not the distance between one
    segment's ends, and not its longest segment. Both were wrong and each hid different
    streets: measuring end-to-end understates a curved road (`חיבת ציון`, 13.8 px against
    84.5 px of real geometry, so never named at any zoom — 22 drawn streets had ≥90 px of
    line but <90 px end to end), and judging by the longest *piece* loses a roundabout
    (`כיכר האבות` is six segments, none over 4.9 px, ~20 px in total — and it is the
    densest listing cluster on the map). Where the name is PLACED still comes from the
    longest single piece. Green/amber streets ever named **81 → 96 of 98**, at 1× **31 →
    49**. The 2 left (`אלפרד רוסי`, `נרבוני`) are genuinely under the 12 px floor.
  - **In-zone streets outrank `_LABEL_CAP`** — sorting on length alone spends the budget
    on long roads out in the desert that no flat is on. `_LABEL_CAP` itself (450) is
    headroom, not the fix: on the dashboard's viewport only 197 labels are emitted so it
    does not bind there at all; it binds on a wider one (~412 streets drawn).
  - `_MAX_LABEL_ZOOM` (8.0) **cannot currently fire** — the 12 px floor caps the ratio at
    90/12 = 7.5. It stays as the coupling between the label rule and the map's 12× zoom
    ceiling (`dashboard.applyView`), so a later change to the floor or the target cannot
    silently emit a label nobody can ever zoom to. A test holds the two ends together.
- `load_map_neighborhoods.py` / `map_neighborhoods.json` — **display-only** neighborhood
  outlines (א–יא, רמות, …). Deliberately NOT `neighborhoods.json`:
  `zones.in_allowed_neighborhood` passes a point inside **any** polygon in that file, so
  adding שכונה ו there to label the map would silently widen the ב/ג/ד gate. A
  `test_zones.py` guard proves it doesn't.
