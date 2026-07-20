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
