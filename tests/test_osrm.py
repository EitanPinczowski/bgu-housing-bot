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
