"""osrm._foot_minutes: a transient error retries with backoff (so a blip doesn't
silently drop the walk time); a real 'no route' answer returns immediately."""
import osrm


class _R:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


_GATE = {"lat": 31.26, "lon": 34.80}


def test_transient_error_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(osrm.time, "sleep", lambda *a: None)
    calls = {"n": 0}

    def flaky_get(url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("blip")
        return _R({"code": "Ok", "routes": [{"duration": 600}]})   # 600s = 10 min

    monkeypatch.setattr(osrm.requests, "get", flaky_get)
    assert osrm._foot_minutes(31.25, 34.79, _GATE) == 10.0
    assert calls["n"] == 3                         # failed twice, succeeded on the third


def test_all_retries_fail_returns_none(monkeypatch):
    monkeypatch.setattr(osrm.time, "sleep", lambda *a: None)
    calls = {"n": 0}

    def always_fail(url, **kw):
        calls["n"] += 1
        raise ConnectionError("down")

    monkeypatch.setattr(osrm.requests, "get", always_fail)
    assert osrm._foot_minutes(31.25, 34.79, _GATE, tries=3) is None
    assert calls["n"] == 3


def test_no_route_answer_does_not_retry(monkeypatch):
    calls = {"n": 0}

    def no_route(url, **kw):
        calls["n"] += 1
        return _R({"code": "NoRoute", "routes": []})

    monkeypatch.setattr(osrm.requests, "get", no_route)
    assert osrm._foot_minutes(31.25, 34.79, _GATE) is None
    assert calls["n"] == 1                          # a real 'no route' is not retried


def test_table_walk_picks_nearest_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(osrm, "_alive", True)
    monkeypatch.setattr(osrm, "_walk_cache", None)
    monkeypatch.setattr(osrm, "_WALK_CACHE_PATH", tmp_path / "walk.json")
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        # source 0 to itself (0) then to each gate in config.GATES order, seconds
        durs = [0] + [900, 300, 600, 480][:len(osrm.config.GATES)]
        return _R({"code": "Ok", "durations": [durs]})

    monkeypatch.setattr(osrm.requests, "get", fake_get)
    minutes, gate = osrm.walk_to_nearest(31.26, 34.80)
    assert minutes == 5.0                          # 300s = 5 min, the nearest gate
    assert calls["n"] == 1                          # ONE /table call, not one per gate
    # second lookup of the same rounded coord is served from cache — no HTTP
    assert osrm.walk_to_nearest(31.26, 34.80) == (5.0, gate)
    assert calls["n"] == 1


def test_circuit_breaker_skips_when_down(monkeypatch):
    # OSRM down: probe fails once, then walk_to_nearest short-circuits (no per-gate calls)
    monkeypatch.setattr(osrm, "_alive", None)
    calls = {"n": 0}

    def down(url, **kw):
        calls["n"] += 1
        raise ConnectionError("down")

    monkeypatch.setattr(osrm.requests, "get", down)
    assert osrm.walk_to_nearest(31.25, 34.79) == (None, None)
    assert osrm.walk_to_nearest(31.26, 34.80) == (None, None)
    assert calls["n"] == 1                          # a single probe, cached — not 4 gates × 2
    assert osrm.osrm_down is True
