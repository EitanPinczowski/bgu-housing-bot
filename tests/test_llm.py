"""llm.extract fallback ladder: quota latches immediately, transient errors are
served by the fallback and only abandon the primary after a threshold."""
import config
import llm


def _setup(monkeypatch, fail_with):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "LLM_MAX_CONSECUTIVE_ERRORS", 3)
    monkeypatch.setattr(llm, "_primary_exhausted", False)
    monkeypatch.setattr(llm, "_consecutive_errors", 0)
    monkeypatch.setattr(llm, "fallback_used", 0)
    calls = []

    def fake_run(provider, text, images=None):
        calls.append(provider)
        if provider == "gemini":
            raise RuntimeError(fail_with)
        return "FALLBACK_OK"

    monkeypatch.setattr(llm, "_run", fake_run)
    return calls


def test_transient_errors_fall_back_then_latch(monkeypatch):
    calls = _setup(monkeypatch, "500 transient server error")
    for _ in range(4):
        assert llm.extract("post") == "FALLBACK_OK"   # every post still served
    assert calls.count("gemini") == 3                  # stops retrying after threshold
    assert llm._primary_exhausted is True


def test_quota_error_latches_immediately(monkeypatch):
    calls = _setup(monkeypatch, "429 RESOURCE_EXHAUSTED")
    llm.extract("post")
    llm.extract("post")
    assert calls.count("gemini") == 1                  # latched on the first quota hit
    assert llm._primary_exhausted is True


def test_the_local_fallback_is_capped_per_run(monkeypatch):
    """A quota-less run used to grind 186 posts at ~63s each, hold the scraper lock
    for 5h12m, and cost the day's other two runs (2026-08-03)."""
    _setup(monkeypatch, "429 RESOURCE_EXHAUSTED")
    monkeypatch.setattr(config, "LOCAL_FALLBACK_MAX_POSTS_PER_RUN", 3)
    assert llm.fallback_budget_spent() is False
    for _ in range(3):
        llm.extract("post")
    assert llm.fallback_budget_spent() is True


def test_the_cap_is_a_question_not_an_exception(monkeypatch):
    """manual.py and replay.py --use-llm hold no lock and have no next run to
    protect, so extract() must keep answering past the cap. Only the scraper loop
    has a reason to stop, so only the scraper loop asks."""
    _setup(monkeypatch, "429 RESOURCE_EXHAUSTED")
    monkeypatch.setattr(config, "LOCAL_FALLBACK_MAX_POSTS_PER_RUN", 2)
    for _ in range(5):
        assert llm.extract("post") == "FALLBACK_OK"     # never raises, never returns None
    assert llm.fallback_used == 5


def test_a_zero_cap_cannot_be_configured():
    """0 would abandon a run the instant Gemini ran out, losing posts the local
    model could still have read."""
    import config as cfg
    real = cfg.LOCAL_FALLBACK_MAX_POSTS_PER_RUN
    try:
        cfg.LOCAL_FALLBACK_MAX_POSTS_PER_RUN = 0
        try:
            cfg.validate()
        except SystemExit as exc:
            assert "LOCAL_FALLBACK_MAX_POSTS_PER_RUN" in str(exc)
        else:
            raise AssertionError("validate() accepted a zero cap")
    finally:
        cfg.LOCAL_FALLBACK_MAX_POSTS_PER_RUN = real


def test_success_resets_error_counter(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "LLM_MAX_CONSECUTIVE_ERRORS", 3)
    monkeypatch.setattr(llm, "_primary_exhausted", False)
    monkeypatch.setattr(llm, "_consecutive_errors", 0)
    seq = iter(["boom", None, "boom"])   # error, success, error

    def fake_run(provider, text, images=None):
        if provider == "gemini":
            v = next(seq)
            if v:
                raise RuntimeError(v)
            return "GEMINI_OK"
        return "FALLBACK_OK"

    monkeypatch.setattr(llm, "_run", fake_run)
    assert llm.extract("p") == "FALLBACK_OK"   # error 1 -> fallback
    assert llm.extract("p") == "GEMINI_OK"     # success resets counter
    assert llm.extract("p") == "FALLBACK_OK"   # error again, counter was reset
    assert llm._primary_exhausted is False     # never reached 3 in a row


def test_ocr_image_capped_per_run(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", None)
    monkeypatch.setattr(config, "SCRAPER_MAX_OCR_PER_RUN", 2)
    monkeypatch.setattr(llm, "_primary_exhausted", False)
    monkeypatch.setattr(llm, "ocr_used", 0)
    seen = []

    def fake_run(provider, text, images=None):
        seen.append(images)
        return "OK"

    monkeypatch.setattr(llm, "_run", fake_run)
    for _ in range(4):
        llm.extract("p", images=["http://img"])
    assert seen == [["http://img"], ["http://img"], None, None]   # capped at 2
    assert llm.ocr_used == 2


def test_ocr_not_spent_on_text_only_posts(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", None)
    monkeypatch.setattr(config, "SCRAPER_MAX_OCR_PER_RUN", 5)
    monkeypatch.setattr(llm, "_primary_exhausted", False)
    monkeypatch.setattr(llm, "ocr_used", 0)
    seen = []
    monkeypatch.setattr(llm, "_run", lambda p, t, images=None: seen.append(images) or "OK")
    llm.extract("a normal text post")
    assert seen == [None] and llm.ocr_used == 0


# --- batched extraction (llm.extract_many) ----------------------------------------

def _batch_setup(monkeypatch):
    """Gemini live, local fallback available, nothing exhausted."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", "openai_compatible")
    monkeypatch.setattr(llm, "_primary_exhausted", False)
    monkeypatch.setattr(llm, "_consecutive_errors", 0)
    monkeypatch.setattr(llm, "fallback_used", 0)


def _mk(addr):
    from models import ListingExtract
    return ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=addr)


def _posts(n):
    return [(f"post {i}", None) for i in range(n)]


def test_a_batch_is_one_request_and_keeps_post_order(monkeypatch):
    """The whole point: 5 posts, 1 request. Order must survive or every listing
    lands on the wrong post."""
    _batch_setup(monkeypatch)
    seen, singles = [], []
    monkeypatch.setattr(llm, "_extract_gemini_many",
                        lambda texts: seen.append(texts) or [_mk(t) for t in texts])
    monkeypatch.setattr(llm, "extract", lambda *a, **k: singles.append(a) or _mk("SINGLE"))
    out = llm.extract_many(_posts(5))
    assert len(seen) == 1 and len(seen[0]) == 5          # ONE request for five posts
    assert not singles                                    # and no per-post calls
    assert [o.street_address_or_neighborhood for o in out] == [f"post {i}" for i in range(5)]


def test_a_short_or_reordered_answer_is_refused():
    """A model that answers 4 objects for 5 posts, or repeats an index, would shift
    listings onto the wrong posts — wrong phone, wrong address — undetectably."""
    import pytest
    from models import ListingExtract

    def items(indices):
        return [llm._IndexedExtract(index=i,
                                    listing=ListingExtract(is_apartment_ad=True))
                for i in indices]

    for bad in ([0, 1, 2, 3],            # too few
                [0, 1, 2, 3, 3],         # duplicate index
                [0, 1, 2, 3, 9],         # index out of range
                [0, 1, 2, 3, 4, 5]):     # too many
        with pytest.raises(ValueError):
            llm._validate_batch(items(bad), 5)

    # …and a correct answer is accepted, in post order, however it arrives
    ok = llm._validate_batch(items([4, 0, 3, 1, 2]), 5)
    assert len(ok) == 5


def test_any_batch_failure_redoes_the_posts_one_by_one(monkeypatch):
    """Never lose a post to a bad batch — quota is spent twice only on this path."""
    _batch_setup(monkeypatch)
    singles = []

    def boom(texts):
        raise ValueError("batch answered [0, 1] for 4 posts")

    monkeypatch.setattr(llm, "_extract_gemini_many", boom)
    monkeypatch.setattr(llm, "extract",
                        lambda t, comments=None, images=None: singles.append(t) or _mk(t))
    out = llm.extract_many(_posts(4))
    assert len(out) == 4 and len(singles) == 4           # every post still extracted
    assert [o.street_address_or_neighborhood for o in out] == [f"post {i}" for i in range(4)]


def test_a_quota_error_on_a_batch_latches_like_a_single_one(monkeypatch):
    """Otherwise the next batch pays Gemini's slow retry-backoff all over again."""
    _batch_setup(monkeypatch)
    monkeypatch.setattr(llm, "_extract_gemini_many",
                        lambda texts: (_ for _ in ()).throw(RuntimeError("429 RESOURCE_EXHAUSTED")))
    monkeypatch.setattr(llm, "extract", lambda *a, **k: _mk("x"))
    llm.extract_many(_posts(3))
    assert llm._primary_exhausted is True


def test_the_local_model_never_batches(monkeypatch):
    """Array structured-output is where small local models are least reliable, and a
    provider with no quota has nothing to gain."""
    _batch_setup(monkeypatch)
    monkeypatch.setattr(llm, "_primary_exhausted", True)     # Gemini gone -> local
    batched = []
    monkeypatch.setattr(llm, "_extract_gemini_many", lambda texts: batched.append(texts))
    monkeypatch.setattr(llm, "extract", lambda *a, **k: _mk("local"))
    llm.extract_many(_posts(5))
    assert batched == []                                      # never even attempted


def test_a_lone_post_does_not_take_the_batch_path(monkeypatch):
    """One post is already one request; batching it just adds a way to fail."""
    _batch_setup(monkeypatch)
    batched = []
    monkeypatch.setattr(llm, "_extract_gemini_many", lambda texts: batched.append(texts))
    monkeypatch.setattr(llm, "extract", lambda *a, **k: _mk("single"))
    assert len(llm.extract_many(_posts(1))) == 1
    assert batched == []


def test_batched_text_is_composed_exactly_like_single_text():
    """The prompt has a rule about the [תגובות למודעה] section; two copies of this
    composition would drift and the batched read would see a different string."""
    assert llm.with_comments("body", "c1") == "body\n\n[תגובות למודעה]:\nc1"
    assert llm.with_comments("body", None) == "body"
    assert llm.with_comments("body", "") == "body"


# --- the daily budget, keyed on the 10:00 quota window ----------------------------

def test_the_window_is_10am_israel_not_midnight():
    """THE WHOLE POINT. Google's free bucket resets at midnight US Pacific = 10:00
    here (measured 2026-08-03: the 08:00 run was EXHAUSTED, the 11:09 run was fine).
    A calendar-day counter would zero at midnight and hand the 08:00 run a budget it
    does not have — worse than no counter at all."""
    from datetime import datetime

    import dates
    assert dates.quota_window(datetime(2026, 8, 3, 9, 59)) == "2026-08-02"
    assert dates.quota_window(datetime(2026, 8, 3, 10, 0)) == "2026-08-03"
    assert dates.quota_window(datetime(2026, 8, 4, 9, 59)) == "2026-08-03"
    # …and midnight does NOT start a new one
    assert (dates.quota_window(datetime(2026, 8, 3, 23, 59))
            == dates.quota_window(datetime(2026, 8, 4, 0, 1)))


def test_the_budget_survives_a_process_restart(monkeypatch, tmp_path):
    """Each scheduled run is a new process; a counter in memory would never bind."""
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 3)
    for _ in range(3):
        llm._spend_budget()
    assert llm.budget_state()[1] == 3            # read back from disk
    assert llm.budget_spent() is True


def test_a_stale_window_reads_as_zero(monkeypatch, tmp_path):
    """No cleanup job: yesterday's entry is simply not this window."""
    import json
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"window": "1999-01-01", "calls": 9999}), encoding="utf-8")
    monkeypatch.setattr(llm, "_BUDGET_PATH", p)
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 10)
    assert llm.budget_state()[1] == 0
    assert llm.budget_spent() is False


def test_spending_the_budget_takes_the_same_path_as_a_429(monkeypatch, tmp_path):
    """It must latch the primary off and route to the fallback, exactly like a real
    quota error — that is what makes Part 1's run cap fire and end the run cleanly."""
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 2)
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", "openai_compatible")
    monkeypatch.setattr(llm, "_primary_exhausted", False)
    monkeypatch.setattr(llm, "fallback_used", 0)
    calls = []
    monkeypatch.setattr(llm, "_run",
                        lambda p, t, images=None: calls.append(p) or f"{p}_OK")
    assert llm.extract("a") == "gemini_OK"
    assert llm.extract("b") == "gemini_OK"
    assert llm.extract("c") == "openai_compatible_OK"   # budget spent -> fallback
    assert llm._primary_exhausted is True
    assert llm.fallback_used == 1


def test_a_zero_budget_disables_the_ceiling(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 0)
    for _ in range(50):
        llm._spend_budget()
    assert llm.budget_spent() is False
