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
