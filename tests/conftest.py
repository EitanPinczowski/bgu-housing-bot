"""Shared test fixtures. Adds the project root to sys.path so tests can import
the flat modules (fit, storage, zones, …) and points storage at a throwaway DB."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


@pytest.fixture(autouse=True)
def _never_touch_the_real_budget_file(tmp_path, monkeypatch):
    """NO TEST MAY READ OR WRITE data/llm_budget.json.

    It happened twice in one session. The damage is quiet and it outlives the test run:
    a test recording a fake refusal left `refused_at` and a synthetic error string in the
    operational file, so `doctor` then advised a budget change based on a value no
    provider ever sent. It was invisible while LLM_DAILY_BUDGET was 900 — the live
    counter sat under it — and only surfaced when the budget was lowered to the real
    limit and 14 unrelated tests began failing.

    Autouse and in conftest rather than in one test module, because the module that
    needs it is not always the module that writes it."""
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "llm_budget.json")
    # `_model_rung` is process state: a test that exhausts the first model would leave
    # every later test running on the second one.
    monkeypatch.setattr(llm, "_model_rung", 0)


@pytest.fixture(autouse=True)
def _no_test_may_launch_docker_desktop(monkeypatch):
    """NO TEST MAY START DOCKER DESKTOP.

    `doctor.try_fix()` gained a second repair layer: when the Docker ENGINE is unreachable
    it launches Docker Desktop and then waits up to ~2 minutes for the engine to answer.
    `test_fix_starts_osrm_when_down` stubs `subprocess.run` but not `subprocess.Popen`, so
    the moment that layer existed the test launched the real application and then slept out
    the full retry loop — the suite went from 59s to 162s, and the 100s was the giveaway.

    Autouse, and here rather than in test_doctor.py, for the same reason as the budget
    fixture above: the module that needs the guard is not always the module that trips it.
    A test that wants the real thing can monkeypatch it back explicitly."""
    import doctor
    monkeypatch.setattr(
        doctor, "_start_docker_desktop",
        lambda: (False, "blocked by the test suite — see conftest"))


@pytest.fixture(autouse=True)
def _no_test_may_poison_the_median_gap():
    """Confine `geocode._median_gap` to the test that computed it.

    It is a module-level cache with no invalidation: the first call to
    `_median_metres_per_number()` measures every anchored street and keeps the answer for
    the life of the process. Tests fake `_street_axis` and `_load_anchors` in six places
    across two files, so whichever test happens to trigger that first call under a fake
    caches a garbage value FOR EVERY TEST AFTER IT.

    With `pytest-randomly` reshuffling the order each run, the symptom is a failure in an
    unrelated file that passes when run alone — on 2026-08-11,
    `test_a_landmark_mentioned_in_passing_never_hijacks_a_real_address` failed that way,
    and the test that poisoned it was in `test_seed_anchors.py`.

    Resetting rather than pinning: a pinned constant would silently drift from the real
    data, and the value is only ever a fallback for a street with a single anchor.

    Autouse, and here rather than beside the tests that fake the axis, for the same reason
    as the two fixtures above — the module that needs the guard is not the module that
    trips it.
    """
    import geocode
    geocode._median_gap = None
    yield
    geocode._median_gap = None


@pytest.fixture(autouse=True)
def _no_test_may_reach_the_network(monkeypatch, request):
    """NO TEST MAY CALL OUT. "Offline always" was already the house rule; nothing enforced
    it, and two tests in `test_geocode.py` were quietly geocoding against live Overpass and
    Nominatim.

    That is not a slow test, it is a WRONG one. The mirrors disagree with each other — the
    project already learned this when the accuracy harness turned out to be measuring a
    coin flip — so an assertion resting on their answer passes or fails according to which
    mirror replied. Measured 2026-08-12 over `pytest-randomly` seeds 1-7: 3 of 7 failed,
    and the same seed could differ between runs.

    A blocked call surfaces as an ordinary exception, and `geocode`'s network tiers already
    swallow those (`except Exception: continue`) and fall through to the local tiers. So
    the effect is exactly `geo_dump --local-only`, which CLAUDE.md names as THE way to
    measure here and which is verified deterministic. A test that genuinely needs a
    response still stubs `requests` itself; monkeypatch applies its patch after this one
    and undoes it first, so the stub simply wins.

    Autouse and in conftest for the reason the two fixtures above give: the module that
    needs the guard is not always the module that trips it."""
    import requests
    def _blocked(*a, **k):
        raise RuntimeError(
            f"offline suite: {request.node.nodeid} tried to reach {(list(a) + [''])[0]} — "
            f"stub `requests` in the test (see _overpass_on in tests/test_geocode.py)")
    for verb in ("get", "post", "put", "delete", "head", "patch", "request"):
        monkeypatch.setattr(requests, verb, _blocked)


@pytest.fixture(autouse=True)
def _no_test_may_touch_the_real_geocode_cache(tmp_path, monkeypatch):
    """NO TEST MAY READ OR WRITE data/geocode_cache.json — same rule, and the same reasons,
    as the budget file above.

    WRITING it is the operational hazard: `geocode_detailed` caches every answer it gets,
    so an unstubbed test writes whatever a mirror happened to say into the file the live
    pipeline and every map dot then read. `_save_cache` only refuses to SHRINK the cache,
    so additions land silently in a full production file.

    READING it is what made the flake permanent rather than intermittent. On 2026-08-12 the
    suite cached `אברהם אבינו, שכונה ד` from overpass and the bare `אברהם אבינו` from
    nominatim — 711 m apart, against a test asserting they agree within 300 m. Once both
    entries existed, a test that had been failing on ~40% of orderings failed on every run,
    and deleting the file was the only way back to green.

    `_cache` is reset alongside the path because it is module-level and loaded once per
    process: repointing the path alone would leave whatever the first test loaded in memory
    for every test after it."""
    import geocode
    monkeypatch.setattr(geocode, "_CACHE_PATH", tmp_path / "geocode_cache.json")
    monkeypatch.setattr(geocode, "_cache", None)


@pytest.fixture(autouse=True)
def _no_test_may_leave_a_mirror_marked_dead():
    """Confine `geocode._dead_mirrors` to the test that filled it.

    It is a per-process circuit breaker with no invalidation: once a mirror raises, it is
    never called again for the life of the interpreter. `test_transient_overpass_failure_is
    _not_cached` asserts an exact call count (`2 * len(config.OVERPASS_URLS)`), so any
    earlier test that killed one mirror made that count come out 7 instead of 8 — a failure
    in a test that passes when run alone.

    Same shape as the `_median_gap` fixture above, and the reason it is a fixture rather
    than a fix in `geocode`: the breaker is correct in production, where a process is one
    scrape. It is only wrong across tests, which pretend to be many processes."""
    import geocode
    geocode._dead_mirrors.clear()
    yield
    geocode._dead_mirrors.clear()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point storage at a fresh empty SQLite file for the duration of one test.
    storage reads config.DB_PATH on every call, so patching the attribute is enough."""
    db = tmp_path / "test_listings.sqlite"
    monkeypatch.setattr(config, "DB_PATH", db)
    return db
