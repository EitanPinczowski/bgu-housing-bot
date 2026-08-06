"""llm.extract fallback ladder: quota latches immediately, transient errors are
served by the fallback and only abandon the primary after a threshold."""
import config
import llm

_slept: list = []          # every sleep the retry loop asked for, in order




def _setup(monkeypatch, fail_with, retries=0):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "LLM_MAX_CONSECUTIVE_ERRORS", 3)
    # Retries default OFF in these fixtures so each test states its own intent, and the
    # suite stays offline and instant — `time.sleep` is stubbed either way.
    monkeypatch.setattr(config, "GEMINI_RETRIES", retries)
    monkeypatch.setattr(llm.time, "sleep", lambda s: _slept.append(s))
    _slept.clear()
    monkeypatch.setattr(llm, "_primary_exhausted", False)
    monkeypatch.setattr(llm, "_consecutive_errors", 0)
    monkeypatch.setattr(llm, "fallback_used", 0)
    monkeypatch.setattr(llm, "retries_attempted", 0)
    monkeypatch.setattr(llm, "retries_succeeded", 0)
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


def test_quota_error_latches_once_its_retries_are_spent(monkeypatch):
    """With retries off this is the old contract: latch on the first quota hit and do
    not touch the primary again. The retry loop is tested separately below."""
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


def test_a_daily_quota_error_on_a_batch_latches_like_a_single_one(monkeypatch):
    """Otherwise the next batch pays Gemini's slow retry-backoff all over again."""
    _batch_setup(monkeypatch)
    monkeypatch.setattr(llm, "_extract_gemini_many",
                        lambda texts: (_ for _ in ()).throw(
                            RuntimeError("429 quota_id: GenerateRequestsPerDayPerProject")))
    monkeypatch.setattr(llm, "extract", lambda *a, **k: _mk("x"))
    llm.extract_many(_posts(3))
    assert llm._primary_exhausted is True


def test_a_retryable_quota_error_on_a_batch_does_not_latch(monkeypatch):
    """`one_by_one` re-runs every post through `extract`, which carries the retry loop.
    Latching here would skip those retries and hand the whole batch to the local model
    for what the usage dashboard says is a 2-7-a-day blip."""
    _batch_setup(monkeypatch)
    monkeypatch.setattr(llm, "_extract_gemini_many",
                        lambda texts: (_ for _ in ()).throw(RuntimeError("429 RESOURCE_EXHAUSTED")))
    monkeypatch.setattr(llm, "extract", lambda *a, **k: _mk("x"))
    llm.extract_many(_posts(3))
    assert llm._primary_exhausted is False


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
    # `_run` is stubbed, so the real counting point (`_pace_gemini`) never fires —
    # spend the budget explicitly, exactly as two real Gemini requests would.
    monkeypatch.setattr(llm, "_run",
                        lambda p, t, images=None: (calls.append(p),
                                                   p == "gemini" and llm._spend_budget(),
                                                   f"{p}_OK")[-1])
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


def test_every_gemini_request_is_counted_whoever_makes_it(monkeypatch, tmp_path):
    """The budget used to be spent in `extract()`, so anything calling `_extract_gemini`
    directly burned real quota invisibly. batch_ab.py does exactly that for its control:
    on 2026-08-04 doctor read 286/900 while Google was already returning 429."""
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 100)
    monkeypatch.setattr(config, "GEMINI_MIN_INTERVAL_SEC", 0)
    before = llm.budget_state()[1]
    llm._pace_gemini()                      # what every Gemini request goes through
    llm._pace_gemini()
    assert llm.budget_state()[1] == before + 2


def test_the_budget_is_not_double_counted(monkeypatch, tmp_path):
    """`extract()` must NOT add its own tally on top of the one in _pace_gemini."""
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 100)
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", None)
    monkeypatch.setattr(llm, "_primary_exhausted", False)
    # _run is stubbed, so _pace_gemini never fires — the only tally would be a stray
    # one left in extract(). There must be none.
    monkeypatch.setattr(llm, "_run", lambda p, t, images=None: "OK")
    before = llm.budget_state()[1]
    llm.extract("post")
    assert llm.budget_state()[1] == before


def test_the_local_model_does_not_spend_gemini_budget(monkeypatch, tmp_path):
    """Ollama has no quota; counting its calls would make the ceiling fire early."""
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 100)
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", "openai_compatible")
    monkeypatch.setattr(llm, "_primary_exhausted", True)     # straight to the fallback
    monkeypatch.setattr(llm, "_run", lambda p, t, images=None: "LOCAL")
    before = llm.budget_state()[1]
    llm.extract("post")
    assert llm.budget_state()[1] == before


# --- Part 5: the budget must be settable from a measurement, not a guess -----------

def test_a_real_refusal_records_where_the_provider_actually_stopped(tmp_path, monkeypatch):
    """`LLM_DAILY_BUDGET` (900) is a guess. The only thing that can replace it is the
    count standing when the first real 429 lands — and that was previously visible for a
    few seconds in the stdout of a run nobody is watching."""
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    llm._spend_budget(366)
    assert llm.quota_refusal() is None
    llm.record_quota_refusal()
    assert llm.quota_refusal() == 366
    assert llm.budget_state()[1] == 366, "recording must not lose the running count"


def test_only_the_first_refusal_in_a_window_is_kept(tmp_path, monkeypatch):
    """Where the refusals START is the ceiling; where the last one landed is noise."""
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    llm._spend_budget(366)
    llm.record_quota_refusal()
    llm._spend_budget(20)
    llm.record_quota_refusal()
    assert llm.quota_refusal() == 366


def test_our_own_ceiling_is_not_recorded_as_a_measurement(tmp_path, monkeypatch):
    """Our budget tripping says nothing about where Google's is — recording it would play
    the guess back as if it were evidence. Only `_is_quota_error` paths call this."""
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(llm.config, "LLM_DAILY_BUDGET", 10)
    llm._spend_budget(10)
    assert llm.budget_spent() is True
    assert llm.quota_refusal() is None, "the client-side ceiling is not a refusal"


def test_counting_more_calls_does_not_erase_the_refusal_point(tmp_path, monkeypatch):
    """`_spend_budget` wrote a bare {window, calls}, so the next run in the same window
    wiped the measurement before anyone could read `doctor`."""
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    llm._spend_budget(366)
    llm.record_quota_refusal()
    llm._spend_budget(5)                 # a later run in the same window
    assert llm.quota_refusal() == 366
    assert llm.budget_state()[1] == 371


def test_a_daily_refusal_and_a_per_minute_one_are_told_apart(tmp_path, monkeypatch):
    """`_is_quota_error` matches RESOURCE_EXHAUSTED / 429 / "quota" alike and threw the
    text away, so the first refusal ever recorded (252, 2026-08-05) cannot be diagnosed.
    Only a PerDay refusal may lower LLM_DAILY_BUDGET — treating a burst that was too fast
    as the daily ceiling would cut the allowance to a fraction of the real one."""
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    llm._spend_budget(250)
    llm.record_quota_refusal(
        "429 RESOURCE_EXHAUSTED quota_metric: generate_content_free_tier_requests, "
        "quota_id: GenerateRequestsPerDayPerProjectPerModel")
    assert llm.quota_refusal() == 250
    assert llm.quota_refusal_kind() == "day"


def test_a_per_minute_refusal_is_not_the_daily_ceiling(tmp_path, monkeypatch):
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    llm._spend_budget(30)
    llm.record_quota_refusal("429 quota_id: GenerateRequestsPerMinutePerProjectPerModel")
    assert llm.quota_refusal_kind() == "minute"


def test_an_undiagnosed_refusal_says_so_rather_than_guessing(tmp_path, monkeypatch):
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    llm._spend_budget(252)
    llm.record_quota_refusal()                      # no detail — the 2026-08-05 case
    assert llm.quota_refusal() == 252
    assert llm.quota_refusal_kind() == "unknown"


# --- the retry loop: a blip must not exile the whole run to Ollama -----------------

def _flaky(monkeypatch, fail_with, fail_times, retries=3):
    """Gemini fails `fail_times` times, then succeeds."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "GEMINI_RETRIES", retries)
    monkeypatch.setattr(config, "GEMINI_RETRY_MAX_SLEEP_SEC", 30.0)
    monkeypatch.setattr(llm.time, "sleep", lambda s: _slept.append(s))
    _slept.clear()
    for name, val in (("_primary_exhausted", False), ("_consecutive_errors", 0),
                      ("fallback_used", 0), ("retries_attempted", 0),
                      ("retries_succeeded", 0)):
        monkeypatch.setattr(llm, name, val)
    calls = []

    def fake_run(provider, text, images=None):
        calls.append(provider)
        if provider == "gemini":
            if calls.count("gemini") <= fail_times:
                raise RuntimeError(fail_with)
            return "GEMINI_OK"
        return "FALLBACK_OK"

    monkeypatch.setattr(llm, "_run", fake_run)
    return calls


def test_a_rate_limit_429_is_retried_not_latched(monkeypatch):
    """THE regression that cost an evening: one 429 latched Gemini off for the whole
    process, so the 18:00 run on 2026-08-05 ground at ~2 min/post on the local model
    while the daily allowance was intact (the counter went on to 501). The usage
    dashboard shows only 2-7 errors a DAY against ~700 requests — every one of them
    was forfeiting a run."""
    calls = _flaky(monkeypatch, "429 RESOURCE_EXHAUSTED", fail_times=1)
    assert llm.extract("post") == "GEMINI_OK"      # stayed on Gemini
    assert llm._primary_exhausted is False         # run keeps its Gemini access
    assert llm.fallback_used == 0                  # Ollama never touched
    assert calls.count("openai_compatible") == 0
    assert llm.retries_attempted == 1 and llm.retries_succeeded == 1


def test_a_503_is_retried_too(monkeypatch):
    """503 ServiceUnavailable is about as common as 429 on the dashboard, and
    `_is_quota_error` never matched it — so it took the consecutive-error path and
    spent a post on the local model at first sight."""
    calls = _flaky(monkeypatch, "503 UNAVAILABLE: model is overloaded", fail_times=2)
    assert llm.extract("post") == "GEMINI_OK"
    assert llm.fallback_used == 0 and calls.count("openai_compatible") == 0
    assert llm.retries_attempted == 2


def test_a_per_day_refusal_skips_the_retries(monkeypatch):
    """Waiting cannot conjure allowance back, so no retries and no sleeping. The one
    extra call is the LADDER — the next model's daily quota is separate, so it is worth
    one attempt before a ~2 min/post local model gets the rest of the run."""
    monkeypatch.setattr(config, "GEMINI_MODELS", ["m-one", "m-two"])
    calls = _flaky(monkeypatch, "429 quota_id: GenerateRequestsPerDayPerProject",
                   fail_times=99)
    assert llm.extract("post") == "FALLBACK_OK"
    assert llm._primary_exhausted is True
    assert calls.count("gemini") == 2               # first rung + second rung, no retries
    assert llm.retries_attempted == 0 and _slept == []


def test_a_per_day_exhaustion_moves_to_the_next_model(monkeypatch):
    """The quota is per project per MODEL, so the reserve has its own untouched
    allowance — 500 more calls instead of falling to Ollama at ~2 min/post."""
    monkeypatch.setattr(config, "GEMINI_MODELS", ["m-one", "m-two"])
    monkeypatch.setattr(llm, "_model_rung", 0)
    seen = []

    def fake_run(provider, text, images=None):
        if provider != "gemini":
            return "FALLBACK_OK"
        seen.append(llm.active_model())
        if llm.active_model() == "m-one":
            raise RuntimeError("429 quota_id: GenerateRequestsPerDayPerProject")
        return "SECOND_MODEL_OK"

    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_FALLBACK_PROVIDER", "openai_compatible")
    monkeypatch.setattr(llm, "_primary_exhausted", False)
    monkeypatch.setattr(llm, "_run", fake_run)
    assert llm.extract("post") == "SECOND_MODEL_OK"
    assert seen == ["m-one", "m-two"]
    assert llm._primary_exhausted is False, "the run keeps its Gemini access"
    assert llm.active_model() == "m-two"


def test_a_blip_never_burns_the_reserve_model(monkeypatch):
    """A per-minute 429 or a 503 is retried IN PLACE. Advancing on a blip would spend
    the second model's allowance on a problem that clears itself in seconds."""
    monkeypatch.setattr(config, "GEMINI_MODELS", ["m-one", "m-two"])
    _flaky(monkeypatch, "429 RESOURCE_EXHAUSTED", fail_times=1)
    llm.extract("post")
    assert llm.active_model() == "m-one", "stayed on the first rung"


def test_the_budget_is_counted_per_model(monkeypatch, tmp_path):
    """One shared number would stop the whole ladder the moment its first rung ran
    out — the opposite of the point."""
    monkeypatch.setattr(config, "GEMINI_MODELS", ["m-one", "m-two"])
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(llm, "_model_rung", 0)
    llm._spend_budget(400)
    assert llm.budget_state("m-one")[1] == 400
    assert llm.budget_state("m-two")[1] == 0, "the reserve has its own allowance"
    monkeypatch.setattr(llm, "_model_rung", 1)
    llm._spend_budget(5)
    assert llm.budget_state("m-two")[1] == 5
    assert llm.budget_state("m-one")[1] == 400      # untouched


def test_retries_exhausted_falls_back_and_the_post_is_not_lost(monkeypatch):
    calls = _flaky(monkeypatch, "429 RESOURCE_EXHAUSTED", fail_times=99, retries=2)
    assert llm.extract("post") == "FALLBACK_OK"     # served, not dropped
    assert calls.count("gemini") == 3               # first try + 2 retries
    assert llm._primary_exhausted is True
    assert llm.retries_attempted == 2 and llm.retries_succeeded == 0


def test_google_s_own_retry_delay_is_honoured_and_capped(monkeypatch):
    """Google usually names a delay; obeying it beats guessing. But it must never be
    able to park a run on one poisoned post."""
    _flaky(monkeypatch, "429 RESOURCE_EXHAUSTED retryDelay: 12.5s", fail_times=1)
    llm.extract("post")
    assert _slept == [12.5]

    _flaky(monkeypatch, "429 RESOURCE_EXHAUSTED please retry in 900s", fail_times=1)
    llm.extract("post")
    assert _slept == [30.0], "capped by GEMINI_RETRY_MAX_SLEEP_SEC"


def test_backoff_grows_when_google_names_no_delay(monkeypatch):
    _flaky(monkeypatch, "503 UNAVAILABLE", fail_times=99, retries=3)
    llm.extract("post")
    assert _slept == [5.0, 15.0, 30.0], _slept       # third capped from 45


def test_a_non_retryable_error_keeps_the_old_ladder(monkeypatch):
    """An ordinary 500/timeout still serves THIS post from the fallback and only
    abandons the primary after LLM_MAX_CONSECUTIVE_ERRORS."""
    monkeypatch.setattr(config, "LLM_MAX_CONSECUTIVE_ERRORS", 3)
    calls = _flaky(monkeypatch, "500 internal", fail_times=99)
    assert llm.extract("post") == "FALLBACK_OK"
    assert calls.count("gemini") == 1                # no retries for this class
    assert llm.retries_attempted == 0
    assert llm._primary_exhausted is False           # not yet at the threshold


# --- the budget must be sized from Google's own number, not from a usage chart -------

_REAL_REFUSAL = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
    "current quota... * Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 500, model: gemini-3.5-flash-lite\n"
    "Please retry in 38.495166512s.', 'details': [{'quotaId': "
    "'GenerateRequestsPerDayPerProjectPerModel-FreeTier', 'quotaValue': '500'}]}}"
)


def test_the_stated_limit_is_read_out_of_the_refusal(tmp_path, monkeypatch):
    """LLM_DAILY_BUDGET was 900 against a real limit of 500 because it had been set from
    a usage chart — which shows where you have BEEN, never where the cap IS. The refusal
    states it outright, so read it."""
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    assert llm._stated_limit(_REAL_REFUSAL) == 500
    llm._spend_budget(506)
    llm.record_quota_refusal(_REAL_REFUSAL)
    assert llm.stated_quota_limit() == 500
    assert llm.quota_refusal_kind() == "day"


def test_an_unknown_refusal_is_upgraded_by_a_later_named_one(tmp_path, monkeypatch):
    """First-writer-wins is right for the COUNT, but holding an `unknown` detail is how
    the answer ("limit: 500") was thrown away and had to be recovered by hand."""
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    llm._spend_budget(250)
    llm.record_quota_refusal("429 RESOURCE_EXHAUSTED")      # names nothing
    assert llm.quota_refusal_kind() == "unknown"
    llm._spend_budget(10)
    llm.record_quota_refusal(_REAL_REFUSAL)                 # names PerDay + the limit
    assert llm.quota_refusal_kind() == "day"
    assert llm.stated_quota_limit() == 500


def test_a_named_refusal_is_not_overwritten_by_a_later_one(tmp_path, monkeypatch):
    """Where the refusals START is the ceiling; a later one is noise."""
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    llm._spend_budget(480)
    llm.record_quota_refusal(_REAL_REFUSAL)
    first = llm.quota_refusal()
    llm._spend_budget(20)
    llm.record_quota_refusal(_REAL_REFUSAL.replace("limit: 500", "limit: 999"))
    assert llm.quota_refusal() == first and llm.stated_quota_limit() == 500


def test_a_legacy_flat_count_is_not_charged_to_any_model(monkeypatch, tmp_path):
    """A budget file written before the per-model split cannot say WHICH model spent
    those calls. Attributing them to "the first rung" is wrong the moment the ladder is
    reordered — it charged 429 calls to a model that had made ~100 of them, and would
    have stopped it 375 calls early. Google's own 429 is the real gate."""
    import json
    import dates
    monkeypatch.setattr(config, "GEMINI_MODELS", ["m-one", "m-two"])
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    (tmp_path / "b.json").write_text(
        json.dumps({"window": dates.quota_window(), "calls": 429}), encoding="utf-8")
    assert llm.budget_state("m-one")[1] == 0
    assert llm.budget_state("m-two")[1] == 0


def test_recording_a_refusal_does_not_reset_the_per_model_counts(monkeypatch, tmp_path):
    """Third time a writer in this file dropped a sibling field. Rebuilding the record
    here wiped `models`, which zeroed the whole ladder's budget the instant a refusal
    was recorded."""
    monkeypatch.setattr(config, "GEMINI_MODELS", ["m-one", "m-two"])
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(llm, "_model_rung", 0)
    llm._spend_budget(300)
    llm.record_quota_refusal("429 quota_id: GenerateRequestsPerDayPerProject")
    assert llm.budget_state("m-one")[1] == 300, "the count survived the refusal"
    llm._spend_budget(5)
    assert llm.budget_state("m-one")[1] == 305
