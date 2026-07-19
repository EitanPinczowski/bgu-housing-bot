"""geocode — static table, Google fallback, bounds guard, and caching.
Google/Nominatim HTTP is mocked; no network is touched."""
import geocode


class _Resp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


def _gmap(lat, lon):
    return {"results": [{"geometry": {"location": {"lat": lat, "lng": lon}}}]}


def _fresh(monkeypatch, tmp_path):
    """Isolate the module cache and force Google on with a fake key."""
    monkeypatch.setattr(geocode, "_cache", {})
    monkeypatch.setattr(geocode, "_CACHE_PATH", tmp_path / "geo.json")
    monkeypatch.setattr(geocode.config, "USE_GOOGLE_GEOCODE", True)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake-key")


def test_static_table_wins_without_network(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # if Google were called it'd blow up (no real net) — static must short-circuit
    monkeypatch.setattr(geocode, "_google", lambda t: (_ for _ in ()).throw(AssertionError("net!")))
    assert geocode.geocode("גר בשכונה ג ליד האוני") == geocode.STATIC_TABLE["שכונה ג"]


def test_google_result_inside_box_is_used_and_cached(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    calls = {"n": 0}
    import requests
    def fake_get(url, **kw):
        calls["n"] += 1
        return _Resp(_gmap(31.255, 34.79))          # a point inside Be'er Sheva
    monkeypatch.setattr(requests, "get", fake_get)
    assert geocode.geocode("הבלוק") == (31.255, 34.79)
    # second lookup is served from cache — no extra HTTP call
    assert geocode.geocode("הבלוק") == (31.255, 34.79)
    assert calls["n"] == 1


def test_google_result_outside_box_is_rejected(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(geocode.config, "USE_NOMINATIM_FALLBACK", False)
    import requests
    monkeypatch.setattr(requests, "get", lambda url, **kw: _Resp(_gmap(32.08, 34.78)))  # Tel Aviv
    assert geocode.geocode("רחוב שלא קיים כאן") is None


def test_disabled_without_key(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    assert geocode._google_enabled() is False
