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


# --- --only-merged: the posts whose archived text holds two Facebook stories --------

def test_only_merged_selects_posts_that_run_into_a_second_story():
    """404 of 6,606 archived posts on 2026-08-05. Their stored parse came from the
    merged blob, so the listing can carry one post's flat under another's permalink."""
    import replay
    merged = {"raw_text": "\n".join([
        "Noya Moyal", "זוג סטודנטים מחפשים דירה", "שכירות עד 3,300",
        "Avidan Mandelman", "1h", "דירה של 95 מטר", "איתי - 0522629429"])}
    single = {"raw_text": "\n".join([
        "Shaked Avikzer", "מתפנה דירת 4 חדרים ברגר 133", "מחיר 3200"])}
    assert replay._is_merged_post(merged) is True
    assert replay._is_merged_post(single) is False
    assert replay._is_merged_post({"raw_text": None}) is False


def test_the_reparse_cut_does_not_touch_the_archive(monkeypatch):
    """The cut is applied to the text HANDED to the LLM, never written back.
    `posts.raw_text` is the record of what was actually scraped, and the trailing story
    is sometimes the only copy of a flat never captured on its own."""
    import replay
    raw = "\n".join(["Noya Moyal", "מחפשים דירה", "Avidan Mandelman", "1h",
                     "דירה של 95 מטר", "איתי - 0522629429"])
    post = {"raw_text": raw, "images": None, "source_url": "u", "group": "g",
            "comments": None, "parsed_json": None}
    seen = {}
    monkeypatch.setattr(replay, "_USE_LLM", True)
    monkeypatch.setattr(replay.pipeline, "process_post",
                        lambda text, **kw: seen.setdefault("text", text))
    replay._reclassify(post)
    assert "מחפשים דירה" in seen["text"]
    assert "0522629429" not in seen["text"], "the second story must not reach the LLM"
    assert post["raw_text"] == raw, "the archive itself is untouched"


def test_frozen_turns_off_every_live_geocoder():
    """A REPLAY THAT CALLS THE NETWORK PRODUCES A SAMPLE, NOT AN ANSWER.

    Measured 2026-08-13: two passes minutes apart over the same 10,565 posts disagreed on
    **1,144 rows** — 736 of them `street_geom -> overpass` — purely because the mirrors
    answered differently. Only 116 rows were the code change under test. `--apply` writes
    those verdicts, so an un-frozen apply bakes one roll of the dice into the DB.

    Run in a subprocess because the flag is read from `sys.argv` at import, which is also
    the only place the switch can be got wrong."""
    import subprocess
    import sys
    probe = (
        "import sys; sys.argv=['replay.py','--frozen']; import replay, config;"
        "print(replay._FROZEN, config.USE_OVERPASS_FALLBACK,"
        " config.USE_NOMINATIM_FALLBACK, config.USE_GOOGLE_GEOCODE)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[-4:] == ["True", "False", "False", "False"], out.stdout


def test_without_frozen_the_geocoders_are_left_alone():
    """Opt-in: WITHOUT a warmed cache, freezing is not lossless, so it must not be the
    default. `full_replay.py` is what pairs the warm with the freeze."""
    import subprocess
    import sys
    probe = ("import sys; sys.argv=['replay.py']; import replay, config;"
             "print(replay._FROZEN, config.USE_OVERPASS_FALLBACK)")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[-2] == "False", out.stdout
