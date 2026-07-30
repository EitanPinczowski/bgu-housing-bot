"""replay --only-bare-nbhd: the predicate that selects which archived posts get a
re-extraction (bare neighborhood = a whole area with no specific street)."""
import replay
from models import ListingExtract


def _post(location):
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=location)
    return {"parsed_json": e.model_dump_json()}


def test_is_imprecise_post():
    # --only-imprecise = bare neighborhood OR bare street (no house number)
    assert replay._is_imprecise_post(_post("שכונה ג"))          # bare neighborhood
    assert replay._is_imprecise_post(_post("רחוב הנדיב"))       # bare street (no number)
    assert not replay._is_imprecise_post(_post("רינגלבלום 5"))  # numbered street = precise
    assert not replay._is_imprecise_post(_post(None))
    assert not replay._is_imprecise_post({"parsed_json": None})
    assert not replay._is_imprecise_post({"parsed_json": "not json"})
    # --only-bare-nbhd (bare_nbhd_only) is the narrower subset: a bare street is excluded
    assert replay._is_imprecise_post(_post("שכונה ג"), bare_nbhd_only=True)
    assert not replay._is_imprecise_post(_post("רחוב הנדיב"), bare_nbhd_only=True)


def test_score_is_the_same_whichever_path_computed_it(monkeypatch):
    """Replay used to pass age_hours=None while a live run passed the real age, so
    the freshness factor contributed on one path and not the other — every
    `replay --apply` silently rewrote scores 2-4 points downward. Same post, same
    number, whichever code path last touched the row."""
    from datetime import datetime, timedelta
    import fit
    import replay
    posted = datetime.now() - timedelta(hours=3)
    age = replay._age_hours({"posted_at": posted.strftime("%Y-%m-%d %H:%M:%S")})
    assert age is not None and 2.9 < age < 3.2
    # the freshness factor is what differed; with a real age both paths agree
    live = fit.score(1400, 6.0, "GREEN", 2, 2, age_hours=3.0)
    replayed = fit.score(1400, 6.0, "GREEN", 2, 2, age_hours=age)
    assert live == replayed
    # and it differs from the old None behaviour, which is the bug being fixed
    assert fit.score(1400, 6.0, "GREEN", 2, 2, age_hours=None) != live


def test_age_hours_tolerates_a_missing_or_broken_timestamp():
    import replay
    assert replay._age_hours({}) is None
    assert replay._age_hours({"posted_at": None}) is None
    assert replay._age_hours({"posted_at": "not-a-date"}) is None
