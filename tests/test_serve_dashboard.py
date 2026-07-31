"""serve_dashboard — the security-critical file.

This server is reachable from outside the machine and it lists OTHER PEOPLE'S phone
numbers and home addresses, so the tests that matter here are the ones about who can
reach what: the token on every route, and the image proxy refusing to fetch anything
that isn't already in the database.

No sockets are bound — the handler is driven directly with a fake request.
"""
import json
from io import BytesIO

import pytest

import serve_dashboard as srv


class _FakeHandler(srv.Handler):
    """Drive Handler without a network: capture what it would have written."""

    def __init__(self, path, method="GET", body=None, token_header=None):
        self.path = path
        self.command = method
        self.sent = {}
        self._body = json.dumps(body).encode() if body is not None else b""
        hdrs = {"Content-Length": str(len(self._body))}
        if token_header:
            hdrs["X-Dashboard-Token"] = token_header
        self.headers = hdrs
        self.rfile = BytesIO(self._body)
        self.wfile = BytesIO()

    # -- swallow the real socket plumbing
    def send_response(self, code, *a):
        self.sent["code"] = code

    def send_header(self, k, v):
        self.sent.setdefault("headers", {})[k] = v

    def end_headers(self):
        pass

    def log_message(self, *a):
        pass

    def run(self):
        (self.do_GET if self.command == "GET" else self.do_POST)()
        return self.sent.get("code"), self.wfile.getvalue()


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret")
    return "s3cret"


def _get(path, **kw):
    return _FakeHandler(path, **kw).run()


def _post(path, body, **kw):
    return _FakeHandler(path, "POST", body=body, **kw).run()


# --- the token, on every route ----------------------------------------------------
@pytest.mark.parametrize("path", ["/", "/api/version", "/api/listings.json",
                                  "/img/" + "a" * 40])
def test_every_get_route_requires_the_token(path):
    assert _get(path)[0] == 403                                  # none supplied
    assert _get(path + "?token=wrong")[0] == 403                 # wrong one


@pytest.mark.parametrize("path", ["/api/mark", "/api/note", "/api/route",
                                  "/api/walk", "/api/locate"])
def test_every_write_route_requires_the_token(path, temp_db):
    code, _ = _post(path, {"key": "k", "mark": "saved"})
    assert code == 403
    code, _ = _post(path + "?token=nope", {"key": "k", "mark": "saved"})
    assert code == 403


def test_a_correct_token_is_accepted_in_query_or_header(temp_db):
    assert _get("/api/version?token=s3cret")[0] == 200
    assert _get("/api/version", token_header="s3cret")[0] == 200


def test_token_comparison_is_not_a_prefix_match():
    assert _get("/api/version?token=s3")[0] == 403
    assert _get("/api/version?token=s3cretXXL")[0] == 403


# --- the image proxy: an allow-list, not a fetcher --------------------------------
def test_image_proxy_refuses_a_hash_we_never_stored(monkeypatch):
    monkeypatch.setattr(srv, "_image_urls", lambda: {})
    monkeypatch.setattr(srv, "_img_map", {})
    code, _ = _get("/img/" + "b" * 40 + "?token=s3cret")
    assert code == 404


def test_image_proxy_never_takes_a_url_from_the_request(monkeypatch):
    """The whole point of hashing: if a URL in the query string could reach the
    fetcher, this would be an open relay for probing internal addresses."""
    fetched = []
    monkeypatch.setattr(srv, "_image_urls", lambda: {})
    monkeypatch.setattr(srv, "_img_map", {})

    def boom(*a, **k):
        fetched.append(a)
        raise AssertionError("must not fetch")

    monkeypatch.setattr(srv, "_cached_image", lambda d: None)
    for bad in ("/img/http://169.254.169.254/latest?token=s3cret",
                "/img/..%2f..%2fconfig.py?token=s3cret",
                "/img/short?token=s3cret"):
        assert _get(bad)[0] == 404
    assert fetched == []


def test_unknown_routes_and_traversal_are_404(temp_db):
    for p in ("/../config.py", "/data/listings.sqlite", "/nope"):
        assert _get(p + "?token=s3cret")[0] == 404


# --- writes go through storage, and only the allowed ones -------------------------
def test_mark_rejects_an_invented_mark(temp_db):
    code, body = _post("/api/mark?token=s3cret", {"key": "k1", "mark": "delete_everything"})
    assert code == 400


def test_mark_records_through_storage(temp_db):
    import storage
    code, body = _post("/api/mark?token=s3cret", {"key": "k1", "mark": "saved"})
    assert code == 200 and json.loads(body)["ok"] is True
    assert storage.get_user_mark("k1", "dashboard") == "saved"
    # the one-vote-per-user rule still applies — the ledger is the same one Telegram uses
    _post("/api/mark?token=s3cret", {"key": "k1", "mark": "dismissed"})
    assert storage.get_user_mark("k1", "dashboard") == "saved"


def test_note_round_trip(temp_db):
    import storage
    code, body = _post("/api/note?token=s3cret",
                       {"key": "k2", "text": "התקשרתי, אין מענה"})
    assert code == 200
    assert storage.get_note("k2") == "התקשרתי, אין מענה"
    assert json.loads(body)["text"] == "התקשרתי, אין מענה"


def test_a_write_without_a_key_is_rejected(temp_db):
    assert _post("/api/mark?token=s3cret", {"mark": "saved"})[0] == 400
    assert _post("/api/note?token=s3cret", {"text": "x"})[0] == 400


def test_malformed_json_does_not_crash_the_server(temp_db):
    h = _FakeHandler("/api/mark?token=s3cret", "POST")
    h.rfile = BytesIO(b"{not json")
    h.headers = {"Content-Length": "9"}
    assert h.run()[0] == 400


# --- payload -----------------------------------------------------------------------
def test_listings_endpoint_matches_the_dashboard_rows(temp_db, monkeypatch):
    monkeypatch.setattr(srv.dashboard, "rows_for_api", lambda: [{"dedup_key": "a"}])
    code, body = _get("/api/listings.json?token=s3cret")
    payload = json.loads(body)
    assert code == 200 and payload["listings"] == [{"dedup_key": "a"}]
    assert "version" in payload


def test_version_is_cheap_and_changes_with_the_data(temp_db):
    import storage
    from models import ListingExtract, PipelineResult, Status
    before = storage.dashboard_version()
    storage.save_listing(PipelineResult(status=Status.MATCH, dedup_key="v1", score=70,
                                        extract=ListingExtract(is_apartment_ad=True)))
    assert storage.dashboard_version() != before


def test_private_data_is_not_cacheable(temp_db):
    h = _FakeHandler("/api/version?token=s3cret")
    h.run()
    assert "no-store" in h.sent["headers"]["Cache-Control"]


# --- the walking route -------------------------------------------------------------
def test_walk_route_is_a_post_so_the_phone_never_enters_a_url(temp_db):
    """A dedup_key is phone|address. GET /api/walk/<key> would put a landlord's phone
    number in the request line, the access log and the browser history; the body
    keeps it out of all three."""
    assert _get("/api/walk?token=s3cret")[0] == 404          # no GET route at all
    code, body = _post("/api/walk?token=s3cret", {"key": "nope"})
    assert code == 200
    assert json.loads(body)["ok"] is False                   # unknown key, not a crash


def test_walk_route_says_so_when_osrm_is_down(temp_db, monkeypatch):
    """There is no honest straight-line version of 'which way do I actually walk',
    so with the router down the answer is 'no route', never a guessed one."""
    import dashboard
    monkeypatch.setattr(dashboard, "_rows",
                        lambda: [{"dedup_key": "k1", "lat": 31.26, "lon": 34.80}])
    monkeypatch.setattr(dashboard.osrm, "foot_geometry", lambda *a, **k: None)
    code, body = _post("/api/walk?token=s3cret", {"key": "k1"})
    out = json.loads(body)
    assert code == 200 and out == {"ok": False, "reason": "osrm_down"}


def test_walk_route_asks_which_gate_once_then_fetches_only_that_path(temp_db,
                                                                     monkeypatch):
    """It used to fetch a full geometry from every gate and keep the shortest: four
    sequential calls, so a sick router took four 15-second timeouts to admit it. One
    table call picks the gate, one route call draws it."""
    import config
    import dashboard
    monkeypatch.setattr(dashboard, "_rows",
                        lambda: [{"dedup_key": "k1", "lat": 31.26, "lon": 34.80}])
    gates = list(config.GATES.values())
    table_calls, geom_calls = [], []

    def fake_table(lat, lon, dests):
        table_calls.append(dests)
        return [30.0, 4.0] + [30.0] * (len(dests) - 2)      # the 2nd gate is nearest

    def fake_geom(lat, lon, target):
        geom_calls.append(target)
        return {"minutes": 4.0, "metres": 300,
                "coords": [[lat, lon], [target["lat"], target["lon"]]]}

    monkeypatch.setattr(dashboard.osrm, "table_minutes", fake_table)
    monkeypatch.setattr(dashboard.osrm, "foot_geometry", fake_geom)
    out = json.loads(_post("/api/walk?token=s3cret", {"key": "k1"})[1])
    assert len(table_calls) == 1 and len(geom_calls) == 1    # 2 calls, not len(GATES)
    assert geom_calls[0] == gates[1]                         # …and the nearest one
    assert out["ok"] and out["minutes"] == 4.0 and out["gate"] == gates[1].get("name")
    # lat,lon for the projector — OSRM's own lon,lat order would put the line in Egypt
    assert out["coords"][0] == [31.26, 34.80]


def test_walk_route_can_target_an_amenity_instead_of_a_gate(temp_db, monkeypatch):
    """"18 דק׳ to the 669" is worth being able to check rather than trust. With a dest
    the gate lookup is skipped entirely — no table call at all."""
    import dashboard
    monkeypatch.setattr(dashboard, "_rows",
                        lambda: [{"dedup_key": "k1", "lat": 31.26, "lon": 34.80}])
    called = []
    monkeypatch.setattr(dashboard.osrm, "table_minutes",
                        lambda *a, **k: called.append("table") or [1.0])
    monkeypatch.setattr(dashboard.osrm, "foot_geometry",
                        lambda lat, lon, t: {"minutes": 7.5, "metres": 610,
                                             "coords": [[lat, lon], [t["lat"], t["lon"]]]})
    out = json.loads(_post("/api/walk?token=s3cret",
                           {"key": "k1", "dest": {"lat": 31.271, "lon": 34.798,
                                                  "label": "מרכז אורן"}})[1])
    assert out["ok"] and out["gate"] == "מרכז אורן" and out["minutes"] == 7.5
    assert called == []                                      # no gate lookup
    assert out["coords"][-1] == [31.271, 34.798]

    bad = json.loads(_post("/api/walk?token=s3cret",
                           {"key": "k1", "dest": {"lat": "here"}})[1])
    assert bad == {"ok": False, "reason": "bad_destination"}


# --- correcting a location ----------------------------------------------------------
def test_locate_is_a_post_and_validates_its_coordinates(temp_db):
    assert _get("/api/locate?token=s3cret")[0] == 404             # no GET route
    assert _post("/api/locate?token=s3cret", {"key": "k1"})[0] == 400          # no lat/lon
    assert _post("/api/locate?token=s3cret",
                 {"key": "k1", "lat": "north", "lon": 34.8})[0] == 400
    code, body = _post("/api/locate?token=s3cret",
                       {"key": "k1", "lat": 999, "lon": 34.8})
    assert code == 200 and json.loads(body) == {"ok": False, "reason": "bad_coordinates"}


def test_locate_refuses_a_key_it_does_not_know(temp_db):
    """Better a plain no than writing a manual_locations row for a listing that
    doesn't exist, which nothing would ever read or clean up."""
    code, body = _post("/api/locate?token=s3cret",
                       {"key": "never-seen", "lat": 31.26, "lon": 34.80})
    assert code == 200 and json.loads(body)["reason"] == "unknown_key"


def test_locate_clear_takes_the_undo_path(temp_db, monkeypatch):
    import dashboard
    seen = {}

    def fake_unrelocate(k):
        seen["key"] = k
        return {"ok": True, "existed": True, "after": None, "regraded": False}

    monkeypatch.setattr(dashboard, "unrelocate", fake_unrelocate)
    code, body = _post("/api/locate?token=s3cret", {"key": "k1", "clear": True})
    assert code == 200 and json.loads(body)["ok"] is True
    assert seen["key"] == "k1"
