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

    def fake_run(provider, text):
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

    def fake_run(provider, text):
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
