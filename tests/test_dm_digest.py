"""dm_digest._core_token: pick the distinctive Hebrew word to geocode a messy
location by, and _suggest's cache/format wiring (network stubbed)."""
import dm_digest as d


def test_core_token_drops_generic_and_takes_longest():
    assert d._core_token("רחוב סיני") == "סיני"
    assert d._core_token("רחוב מגידו, שכונה ט") == "מגידו"        # generics + 1-letter dropped
    assert d._core_token("הנרי קנדל 14, שכונת רמב\"ם") == "הנרי"   # longest of הנרי/קנדל (tie→first max)
    assert d._core_token("באר שבע") is None                        # nothing distinctive left
    assert d._core_token("13/6") is None                           # digits/punct only


def test_suggest_caches_and_formats(monkeypatch):
    calls = {"n": 0}

    def fake_post(ep, data=None, timeout=None, headers=None):
        calls["n"] += 1

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"elements": [{"tags": {"name": "טור סיני"},
                                      "center": {"lat": 31.27032, "lon": 34.80668}}]}
        return R()

    import time
    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda *_: None)   # _suggest's `import time` is this module
    d._sugg_cache.clear()

    assert d._suggest("רחוב סיני") == ("טור סיני", 31.27032, 34.80668)
    d._suggest("רחוב סיני")                     # same token -> served from cache
    assert calls["n"] == 1
