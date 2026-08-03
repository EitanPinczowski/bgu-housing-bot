"""main: yield-scaled scan depth and the --hot group selection. The point is to raise
matches per run WITHOUT increasing total reads on the single Facebook account."""
import config
import main


def _yield(rows):
    """rows: [(url, posts, matches)] -> the storage.group_yield() shape."""
    return [(u, tot, m, 0, 0, 0) for u, tot, m in rows]


def test_low_yield_groups_read_shallowly(monkeypatch):
    rich, poor = "https://g/rich", "https://g/poor"
    monkeypatch.setattr(config, "FB_GROUPS", [rich, poor])
    monkeypatch.setattr(main.storage, "group_yield",
                        lambda: _yield([(rich, 200, 20), (poor, 300, 1)]))   # 10% vs 0.3%
    d = main._group_depths()
    assert rich not in d                                   # productive -> full depth
    assert d[poor] == config.GROUP_MIN_POSTS_FLOOR         # noisy -> floor, never 0


def test_new_group_is_not_starved(monkeypatch):
    new = "https://g/new"
    monkeypatch.setattr(config, "FB_GROUPS", [new])
    monkeypatch.setattr(main.storage, "group_yield", lambda: _yield([(new, 5, 0)]))
    assert main._group_depths() == {}                      # too little history -> full depth


def test_scaling_can_be_disabled(monkeypatch):
    poor = "https://g/poor"
    monkeypatch.setattr(config, "FB_GROUPS", [poor])
    monkeypatch.setattr(config, "GROUP_YIELD_SCALING", False)
    monkeypatch.setattr(main.storage, "group_yield", lambda: _yield([(poor, 300, 1)]))
    assert main._group_depths() == {}


def test_hot_picks_the_best_groups(monkeypatch):
    a, b, c = "https://g/a", "https://g/b", "https://g/c"
    monkeypatch.setattr(config, "FB_GROUPS", [a, b, c])
    monkeypatch.setattr(config, "HOT_GROUP_COUNT", 2)
    monkeypatch.setattr(main.storage, "group_yield",
                        lambda: _yield([(a, 100, 1), (b, 100, 9), (c, 100, 5)]))
    assert main._hot_groups() == [b, c]                    # highest match rate first


def test_hot_falls_back_without_history(monkeypatch):
    a, b = "https://g/a", "https://g/b"
    monkeypatch.setattr(config, "FB_GROUPS", [a, b])
    monkeypatch.setattr(config, "HOT_GROUP_COUNT", 1)
    monkeypatch.setattr(main.storage, "group_yield", lambda: [])
    assert main._hot_groups() == [a]                       # configured order, never empty


# --- the local-fallback cap ends the run (see config.LOCAL_FALLBACK_MAX_POSTS_PER_RUN) ---

class _FakeCtx:
    pages: list = []

    def new_page(self):
        return object()

    def close(self):
        pass


class _FakeP:
    def stop(self):
        pass


def _stub_run(monkeypatch, groups, posts_per_group, cap):
    """A --dry-run over `groups` fake groups, with Gemini 'exhausted' from the start
    so every post costs a local extraction."""
    import llm
    import pipeline
    import scraper
    from models import PipelineResult, Status

    monkeypatch.setattr(config, "SCRAPER_SKIP_RUN_PROBABILITY", 0.0)
    monkeypatch.setattr(config, "LOCAL_FALLBACK_MAX_POSTS_PER_RUN", cap)
    monkeypatch.setattr(config, "SCRAPER_GROUP_DELAY", (0, 0))
    monkeypatch.setattr(main, "_select_groups", lambda: list(groups))
    monkeypatch.setattr(main, "_group_depths", lambda: {})
    monkeypatch.setattr(main, "_record_scrape", lambda url: None)
    monkeypatch.setattr(main, "_log_search", lambda *a, **k: None)
    monkeypatch.setattr(scraper, "acquire_lock", lambda: True)
    monkeypatch.setattr(scraper, "release_lock", lambda: None)
    monkeypatch.setattr(scraper, "start_self_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(scraper, "beat", lambda *a, **k: None)
    monkeypatch.setattr(scraper, "open_browser", lambda: (_FakeP(), _FakeCtx()))

    seen_groups = []

    def fake_scrape_group(page, url, **kw):
        seen_groups.append(url)
        return ([{"text": f"post {i}", "permalink": None} for i in range(posts_per_group)],
                {"read": posts_per_group, "age_skipped": 0, "seen_skipped": 0})

    monkeypatch.setattr(scraper, "scrape_group", fake_scrape_group)

    monkeypatch.setattr(llm, "fallback_used", 0)
    monkeypatch.setattr(llm, "_primary_exhausted", True)      # quota gone from the start

    def fake_process(*a, **kw):
        llm.fallback_used += 1                                # what the local path costs
        return PipelineResult(status=Status.NOT_AD, reason="stub")

    monkeypatch.setattr(pipeline, "process_post", fake_process)
    return seen_groups


def test_the_fallback_cap_ends_the_run_not_just_the_group(monkeypatch):
    """The break has to escape BOTH loops. Breaking only the inner one would move to
    the next group and keep grinding — which is the 5h12m run it exists to prevent."""
    seen = _stub_run(monkeypatch, ["g1", "g2", "g3", "g4"], posts_per_group=5, cap=7)
    main.run(dry_run=True)
    # cap 7 with 5 posts/group: g1 (5) then g2 trips it at 7 — g3/g4 never opened
    assert seen == ["g1", "g2"], seen


def test_an_uncapped_run_still_scans_every_group(monkeypatch):
    """The cap must not fire on a healthy run."""
    seen = _stub_run(monkeypatch, ["g1", "g2", "g3"], posts_per_group=2, cap=100)
    main.run(dry_run=True)
    assert seen == ["g1", "g2", "g3"], seen


# --- batched extraction in the run loop (config.LLM_BATCH_SIZE) --------------------

def _batch_run(monkeypatch, groups, posts_per_group, batch_size,
               early=None, ocr=False):
    """A --dry-run whose posts all survive the pre-LLM gates unless `early` says
    otherwise. Returns the list of batch sizes llm.extract_many was asked for."""
    import llm
    import pipeline
    import scraper
    from models import ListingExtract, PipelineResult, Status

    monkeypatch.setattr(config, "SCRAPER_SKIP_RUN_PROBABILITY", 0.0)
    monkeypatch.setattr(config, "LLM_BATCH_SIZE", batch_size)
    monkeypatch.setattr(config, "LOCAL_FALLBACK_MAX_POSTS_PER_RUN", 10_000)
    monkeypatch.setattr(config, "SCRAPER_GROUP_DELAY", (0, 0))
    monkeypatch.setattr(main, "_select_groups", lambda: list(groups))
    monkeypatch.setattr(main, "_group_depths", lambda: {})
    monkeypatch.setattr(main, "_record_scrape", lambda url: None)
    monkeypatch.setattr(main, "_log_search", lambda *a, **k: None)
    monkeypatch.setattr(scraper, "acquire_lock", lambda: True)
    monkeypatch.setattr(scraper, "release_lock", lambda: None)
    monkeypatch.setattr(scraper, "start_self_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(scraper, "beat", lambda *a, **k: None)
    monkeypatch.setattr(scraper, "open_browser", lambda: (_FakeP(), _FakeCtx()))
    monkeypatch.setattr(scraper, "scrape_group", lambda page, url, **kw: (
        [{"text": f"{url}-post{i}", "permalink": None} for i in range(posts_per_group)],
        {"read": posts_per_group, "age_skipped": 0, "seen_skipped": 0}))

    monkeypatch.setattr(pipeline, "pre_llm_verdict", lambda *a, **k: early)
    monkeypatch.setattr(pipeline, "is_ocr_post", lambda *a, **k: ocr)
    monkeypatch.setattr(pipeline, "process_post",
                        lambda *a, **k: PipelineResult(status=Status.NOT_AD, reason="stub"))

    sizes = []

    def fake_many(posts):
        sizes.append(len(posts))
        return [ListingExtract(is_apartment_ad=True) for _ in posts]

    monkeypatch.setattr(llm, "extract_many", fake_many)
    main.run(dry_run=True)
    return sizes


def test_posts_are_extracted_five_at_a_time(monkeypatch):
    """The whole quota fix: the free tier meters REQUESTS, so 10 posts is 2 calls."""
    sizes = _batch_run(monkeypatch, ["g1"], posts_per_group=10, batch_size=5)
    assert sizes == [5, 5]


def test_a_partial_batch_flushes_at_the_group_boundary(monkeypatch):
    """Otherwise a group's last few posts wait for the NEXT group to fill the buffer,
    and the final group's remainder is never extracted at all."""
    sizes = _batch_run(monkeypatch, ["g1", "g2"], posts_per_group=7, batch_size=5)
    assert sizes == [5, 2, 5, 2]          # per group: a full batch, then the tail
    assert sum(sizes) == 14               # and every post was extracted


def test_a_post_the_cheap_gates_answered_never_costs_a_batch_slot(monkeypatch):
    """Those gates already spare ~27% of posts an LLM call; batching them would pay
    for exactly what they just saved."""
    from models import PipelineResult, Status
    sizes = _batch_run(monkeypatch, ["g1"], posts_per_group=10, batch_size=5,
                       early=PipelineResult(status=Status.DROP, reason="already seen"))
    assert sizes == []                    # extract_many never called


def test_ocr_posts_stay_on_the_single_path(monkeypatch):
    """They need the image, which is Gemini-only and separately capped per run."""
    sizes = _batch_run(monkeypatch, ["g1"], posts_per_group=6, batch_size=5, ocr=True)
    assert sizes == []


def test_batch_size_one_disables_batching(monkeypatch):
    sizes = _batch_run(monkeypatch, ["g1"], posts_per_group=4, batch_size=1)
    assert sizes == [1, 1, 1, 1]
