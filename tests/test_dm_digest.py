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


# --- doctor's failures ride the digest (a check nobody reads is not a check) --------

def test_health_section_lists_each_failure_with_its_remedy(monkeypatch):
    import doctor
    monkeypatch.setattr(doctor, "checks", lambda: [
        ("osrm", doctor.FAIL, "connection refused", "start the container"),
        ("db", doctor.PASS, "346 listings", ""),
    ])
    out = "\n".join(d._health_section())
    assert "osrm" in out and "connection refused" in out
    assert "start the container" in out          # doctor's fix line is what makes it useful
    assert "db" not in out                       # passing rows say nothing


def test_health_section_is_silent_when_everything_passes(monkeypatch):
    import doctor
    monkeypatch.setattr(doctor, "checks", lambda: [("db", doctor.PASS, "ok", "")])
    assert d._health_section() == [], "silence must mean healthy, not a daily cry"


def test_only_hard_failures_are_reported(monkeypatch):
    """A digest that complains every day is one you stop opening — which is the failure
    this exists to fix, not repeat. WARN stays in `doctor.py`."""
    import doctor
    monkeypatch.setattr(doctor, "checks", lambda: [("sheets", doctor.WARN, "partial", "fix it")])
    assert d._health_section() == []


def test_a_broken_doctor_cannot_suppress_the_rest_of_the_digest(monkeypatch):
    """The unmapped-locations report has always been this digest's job; a health check
    that throws must not take it down with it."""
    import doctor
    monkeypatch.setattr(doctor, "checks", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = "\n".join(d._health_section())
    assert "boom" in out and out.startswith("⚠")


def test_a_failure_alone_is_enough_to_send(monkeypatch):
    """Nothing unmapped and nothing low-confidence used to mean `build` returned None —
    so a wedged scraper would have been reported by nobody."""
    import doctor
    import storage
    monkeypatch.setattr(storage, "unknown_locations", lambda *a, **k: [])
    monkeypatch.setattr(d, "_low_confidence_section", list)
    monkeypatch.setattr(doctor, "checks", lambda: [("osrm", doctor.FAIL, "down", "")])
    text = d.build(days=1, suggest=False)
    assert text and "osrm" in text
