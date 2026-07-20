"""fit.score / fit.stars — the ⭐ ranking. These thresholds caused the
'every listing got 5 stars' bug, so they're worth pinning down."""
import config
import fit


def test_stars_thresholds():
    assert fit.stars(88) == "⭐⭐⭐⭐⭐"
    assert fit.stars(87) == "⭐⭐⭐⭐"
    assert fit.stars(70) == "⭐⭐⭐⭐"
    assert fit.stars(69) == "⭐⭐⭐"
    assert fit.stars(52) == "⭐⭐⭐"
    assert fit.stars(51) == "⭐⭐"
    assert fit.stars(34) == "⭐⭐"
    assert fit.stars(33) == "⭐"
    assert fit.stars(0) == "⭐"


def test_score_stays_in_range():
    assert 0 <= fit.score(None, None, None) <= 100
    assert fit.score(500, 3, "GREEN", 3, 2, False, age_hours=1) <= 100
    assert fit.score(9999, 99, "RED", 0, 9, True, age_hours=999) >= 0


def test_green_close_cheap_is_five_stars():
    hi = fit.score(int(config.TARGET_PRICE_PER_ROOM_ILS * 0.7), 5, "GREEN", 3, 2)
    lo = fit.score(config.MAX_PRICE_PER_ROOM_ILS, 30, "AMBER", 1, 4)
    assert hi >= 88          # genuinely excellent -> 5 stars
    assert hi > lo           # and clearly beats a far, pricey, crowded place


def test_uncertain_price_is_penalized():
    certain = fit.score(1500, 10, "GREEN", 2, 3, price_uncertain=False)
    uncertain = fit.score(1500, 10, "GREEN", 2, 3, price_uncertain=True)
    assert uncertain < certain


def test_missing_price_is_penalized():
    known = fit.score(1400, 10, "GREEN", 2, 3)
    unknown = fit.score(None, 10, "GREEN", 2, 3)
    assert unknown < known


def test_furnished_is_a_bonus_only():
    base = fit.score(1400, 10, "GREEN", 2, 3)          # 81, room for +5
    assert fit.score(1400, 10, "GREEN", 2, 3, furnished=True) == base + config.FURNISHED_BONUS
    assert fit.score(1400, 10, "GREEN", 2, 3, furnished=False) == base   # no penalty
    assert fit.score(1400, 10, "GREEN", 2, 3, furnished=None) == base


def test_fresher_beats_stale():
    fresh = fit.score(1400, 10, "GREEN", 2, 3, age_hours=2)
    stale = fit.score(1400, 10, "GREEN", 2, 3, age_hours=48)
    assert fresh > stale


def test_unknown_age_is_neutral():
    # a manual paste has no age and must not be punished for it
    assert fit.score(1400, 10, "GREEN", 2, 3, age_hours=None) == \
           fit.score(1400, 10, "GREEN", 2, 3, age_hours=20)  # 18–36h band = 0


def test_breakdown_sums_to_score():
    args = (1400, 7.0, "GREEN", 2, 3, False, 3.0, "1.10", True)
    parts = fit.breakdown(*args)
    raw = sum(d for _, d in parts)
    assert fit.score(*args) == max(0, min(100, raw))       # score is the clamped sum
    # top_factors returns the biggest positives, descending
    top = fit.top_factors(parts, n=3)
    assert len(top) == 3 and all(d > 0 for _, d in top)
    assert [d for _, d in top] == sorted((d for _, d in top), reverse=True)


def test_breakdown_penalties_are_negative():
    parts = dict(fit.breakdown(None, 25.0, None, price_uncertain=True))  # worst-ish
    assert parts["אי-ודאות מחיר"] == -6
    assert parts["מחיר מהתגובות"] == -8


def test_lease_month_parsing():
    assert fit._lease_month("1.10") == 10
    assert fit._lease_month("01/10") == 10
    assert fit._lease_month("כניסה 1.9") == 9
    assert fit._lease_month("כניסה בספטמבר") == 9
    assert fit._lease_month("מיידי") is None
    assert fit._lease_month(None) is None


def test_entry_date_is_smallest_factor():
    # target month is October (config.TARGET_MOVE_IN_MONTH = 10)
    on = fit.score(1400, 10, "GREEN", 2, 3, lease_start="1.10")
    adj = fit.score(1400, 10, "GREEN", 2, 3, lease_start="1.9")   # September, adjacent
    off = fit.score(1400, 10, "GREEN", 2, 3, lease_start="1.3")   # March, far
    assert on > adj > off                     # closer to target scores higher
    assert on - off <= 4                       # ...but by at most 4 points (tiny)
