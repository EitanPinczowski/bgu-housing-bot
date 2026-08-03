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
