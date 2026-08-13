"""The conftest guards that keep this suite offline and reproducible.

A guard that can only ever say PASS is not a guard, so each of the three is exercised
against the thing it is supposed to catch. They exist because `tests/test_geocode.py` was
geocoding against live Overpass and Nominatim: 3 of 7 `pytest-randomly` seeds failed on
2026-08-12, and once two mirrors' disagreeing answers reached `data/geocode_cache.json`
the failure became permanent.
"""
import config
import geocode
import pytest


def test_the_network_guard_actually_blocks_a_call():
    """Proved against a real call rather than assumed. The message names the test, because
    the failure it reports is "someone wrote a test that reaches the internet" and the
    useful part is which one."""
    import requests
    with pytest.raises(RuntimeError, match="offline suite"):
        requests.get("https://overpass-api.de/api/interpreter")
    with pytest.raises(RuntimeError, match="offline suite"):
        requests.post("https://nominatim.openstreetmap.org/search")


def test_a_test_may_still_stub_requests_for_itself(monkeypatch):
    """The guard must not break the many tests that legitimately fake a response.
    monkeypatch applies the test's patch after the fixture's and undoes it first, so the
    stub wins for the duration and the block is back in place afterwards."""
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: "stubbed")
    assert requests.post("https://example.invalid") == "stubbed"


def test_the_geocode_cache_path_is_not_the_real_one():
    """The operational hazard: `geocode_detailed` caches every answer, and `_save_cache`
    only refuses to SHRINK a cache — additions land silently in the production file that
    the live pipeline and every map dot then read."""
    assert geocode._CACHE_PATH != config.DATA_DIR / "geocode_cache.json"
    assert not geocode._CACHE_PATH.exists(), "each test starts from an empty cache"


def test_writing_the_cache_cannot_reach_the_real_file():
    """Prove it by actually writing one."""
    real = config.DATA_DIR / "geocode_cache.json"
    before = real.read_bytes() if real.exists() else None
    geocode._load_cache()["מקום בדיקה"] = {"c": [31.25, 34.80], "s": "test"}
    geocode._save_cache()
    assert geocode._CACHE_PATH.exists(), "it wrote somewhere"
    after = real.read_bytes() if real.exists() else None
    assert after == before, "the real cache changed"


# These two are a PAIR and the order is the point: the killer runs first in file order, so
# the checker below can only pass because the fixture cleaned up between them. Run them the
# other way round and the checker proves nothing — it would pass in a fresh process anyway.

def test_a_mirror_killed_here_must_not_leak_to_the_next_test(monkeypatch):
    """Kill every mirror. The assertion here is only that the breaker really trips, which
    is what stops the checker below from being vacuous."""
    monkeypatch.setattr(geocode.config, "USE_OVERPASS_FALLBACK", True)
    monkeypatch.setattr(geocode.time, "sleep", lambda *a: None)
    geocode._overpass_query("רחוב כלשהו", None)      # every mirror raises -> marked dead
    assert geocode._dead_mirrors, "the breaker did not trip, so the checker proves nothing"


def test_the_dead_mirror_breaker_starts_clear():
    """`_dead_mirrors` is a per-process circuit breaker ({url: retry-after}) — correct for
    a scrape, wrong across tests, which pretend to be many processes. A single mirror left
    marked dead by an earlier test turned an exact call-count assertion from 8 into 7, in
    `test_transient_overpass_failure_is_not_cached`, which passes when run alone.

    The test immediately above marked every mirror dead. Reaching here with a clean set is
    the fixture working."""
    assert geocode._dead_mirrors == {}
