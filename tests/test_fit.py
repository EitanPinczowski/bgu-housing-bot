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


def test_fresher_beats_stale():
    fresh = fit.score(1400, 10, "GREEN", 2, 3, age_hours=2)
    stale = fit.score(1400, 10, "GREEN", 2, 3, age_hours=48)
    assert fresh > stale


def test_unknown_age_is_neutral():
    # a manual paste has no age and must not be punished for it
    assert fit.score(1400, 10, "GREEN", 2, 3, age_hours=None) == \
           fit.score(1400, 10, "GREEN", 2, 3, age_hours=20)  # 18–36h band = 0
