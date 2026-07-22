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
    # excellent base + features clears 5★; featureless-excellent is now ~4★ (rescale)
    hi = fit.score(int(config.TARGET_PRICE_PER_ROOM_ILS * 0.7), 5, "GREEN", 3, 2,
                   has_balcony=True, furnished=True)
    lo = fit.score(config.MAX_PRICE_PER_ROOM_ILS, 30, "AMBER", 1, 4)
    assert hi >= 88          # genuinely excellent + features -> 5 stars
    assert hi > lo           # and clearly beats a far, pricey, crowded place


def test_uncertain_price_is_penalized():
    certain = fit.score(1500, 10, "GREEN", 2, 3, price_uncertain=False)
    uncertain = fit.score(1500, 10, "GREEN", 2, 3, price_uncertain=True)
    assert uncertain < certain


def test_missing_price_is_penalized():
    known = fit.score(1400, 10, "GREEN", 2, 3)
    unknown = fit.score(None, 10, "GREEN", 2, 3)
    assert unknown < known


def test_furnished_lifts_score_one_way():
    base = fit.score(1400, 10, "GREEN", 2, 3)
    assert fit.score(1400, 10, "GREEN", 2, 3, furnished=True) > base      # furnished helps
    assert fit.score(1400, 10, "GREEN", 2, 3, furnished=False) == base    # unfurnished: no penalty
    assert fit.score(1400, 10, "GREEN", 2, 3, furnished=None) == base


def test_fresher_beats_stale():
    fresh = fit.score(1400, 10, "GREEN", 2, 3, age_hours=2)
    stale = fit.score(1400, 10, "GREEN", 2, 3, age_hours=48)
    assert fresh > stale


def test_unknown_age_is_neutral():
    # a manual paste has no age and must not be punished for it
    assert fit.score(1400, 10, "GREEN", 2, 3, age_hours=None) == \
           fit.score(1400, 10, "GREEN", 2, 3, age_hours=20)  # 18–36h band = 0


def test_score_is_rescaled_raw():
    args = (1400, 7.0, "GREEN", 2, 3, False, 3.0, "1.10", True)
    parts = fit.breakdown(*args)
    raw = sum(d for _, d in parts)
    # score is the raw sum rescaled onto 0–100 by the theoretical max (not clamped raw)
    assert fit.score(*args) == max(0, min(100, round(100 * raw / fit._max_possible())))
    top = fit.top_factors(parts, n=3)
    assert len(top) == 3 and all(d > 0 for _, d in top)
    assert [d for _, d in top] == sorted((d for _, d in top), reverse=True)


def test_rescale_uncompresses_the_top():
    # both of these hit the 100 clamp before; now the featureless one is strictly
    # lower and balcony+furnished lift the score — features finally differentiate.
    plain = fit.score(1200, 7.0, "GREEN", 2, 2, age_hours=3)
    full = fit.score(1200, 7.0, "GREEN", 2, 2, age_hours=3, has_balcony=True, furnished=True)
    assert plain < 100
    assert plain < full <= 100


def test_breakdown_penalties_are_negative():
    parts = dict(fit.breakdown(None, 25.0, None, price_uncertain=True))  # worst-ish
    assert parts["אי-ודאות מחיר"] == -6
    assert parts["מחיר מהתגובות"] == -3       # softened comment-price penalty


def test_walk_and_price_bands():
    # walk: ≤10 is the "close" band = 25
    assert dict(fit.breakdown(1400, 8.0, "GREEN"))["הליכה 8 דק׳"] == 25
    assert dict(fit.breakdown(1400, 13.0, "GREEN"))["הליכה 13 דק׳"] == 20
    assert dict(fit.breakdown(1400, None, "GREEN"))["הליכה לא ידועה"] == 13
    # price: forgiving absolute bands
    def price_pts(p):
        return next(v for k, v in dict(fit.breakdown(p, 8.0, "GREEN")).items() if "מחיר" in k)
    assert price_pts(1200) == 25
    assert price_pts(1500) == 22
    assert price_pts(1600) == 20
    assert price_pts(1700) == 18
    assert price_pts(2000) == 15
    assert price_pts(2500) == 0


def test_floor_num_parsing():
    assert fit._floor_num("קרקע") == 0
    assert fit._floor_num("3 מתוך 5") == 3      # apartment's own floor, not the total
    assert fit._floor_num("קומה 5") == 5
    assert fit._floor_num("שלישית") == 3
    assert fit._floor_num("ראשונה") == 1
    assert fit._floor_num(None) is None
    assert fit._floor_num("מרתף") is None       # no digit, no known ordinal


_BASE = (1500, 7.0, "GREEN", 2, 3)   # price, walk, tier, avail, mates


def test_balcony_and_furnished_factors():
    assert dict(fit.breakdown(*_BASE, has_balcony=True))["מרפסת/גינה"] == 18
    assert dict(fit.breakdown(*_BASE, furnished=True))["מרוהט"] == 10
    assert "מרפסת/גינה" not in dict(fit.breakdown(*_BASE))   # absent when not present


def test_floor_penalty_exponential_and_gated():
    def pen(**kw):
        d = dict(fit.breakdown(*_BASE, **kw))
        return next((v for k, v in d.items() if k.startswith("קומה")), 0)
    assert pen(floor="2") == -2          # base 2.5 ** 1
    assert pen(floor="4") == -16         # 2.5 ** 3 = 15.6 -> 16
    assert pen(floor="5") == -39         # 2.5 ** 4 = 39.06
    assert pen(floor="8") == -40         # capped
    assert pen(floor="5", has_elevator=True) == 0    # elevator -> no penalty
    assert pen(floor="5", has_elevator=False) == -39  # explicit no-elevator penalizes
    assert pen(floor="1") == 0           # floor <= 1
    assert pen(floor="קרקע") == 0
    assert pen(floor=None) == 0          # unknown floor


def test_lease_month_parsing():
    assert fit._lease_month("1.10") == 10
    assert fit._lease_month("01/10") == 10
    assert fit._lease_month("כניסה 1.9") == 9
    assert fit._lease_month("כניסה בספטמבר") == 9
    assert fit._lease_month("מיידי") is None
    assert fit._lease_month(None) is None


def test_neighborhood_preference_b_over_c_equals_d():
    base = fit.score(1500, 10, "GREEN", 2, 3)
    b = fit.score(1500, 10, "GREEN", 2, 3, neighborhood="ב")
    c = fit.score(1500, 10, "GREEN", 2, 3, neighborhood="ג")
    d = fit.score(1500, 10, "GREEN", 2, 3, neighborhood="ד")
    assert b > c            # ב is preferred
    assert c == d == base   # ג and ד tie, and get no bonus (== the no-neighborhood base)
    # the bonus is only shown for ב, and it is tie-breaker sized (a few points)
    assert dict(fit.breakdown(1500, 10, "GREEN", 2, 3, neighborhood="ב"))["שכונה ב מועדפת"] == 4
    assert not any("שכונה" in k for k, _ in fit.breakdown(1500, 10, "GREEN", 2, 3, neighborhood="ג"))


def test_entry_date_is_smallest_factor():
    # target month is October (config.TARGET_MOVE_IN_MONTH = 10)
    on = fit.score(1400, 10, "GREEN", 2, 3, lease_start="1.10")
    adj = fit.score(1400, 10, "GREEN", 2, 3, lease_start="1.9")   # September, adjacent
    off = fit.score(1400, 10, "GREEN", 2, 3, lease_start="1.3")   # March, far
    assert on > adj > off                     # closer to target scores higher
    assert on - off <= 4                       # ...but by at most 4 points (tiny)
