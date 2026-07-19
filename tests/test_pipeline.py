"""pipeline helpers — the שכונה ד' no-amber rule and the text fingerprint."""
import pipeline


def test_no_amber_area_matches_dalet_only():
    assert pipeline._no_amber_area("שכונה ד'")
    assert pipeline._no_amber_area("רחוב הפלמ\"ח, שכונה ד")
    assert pipeline._no_amber_area("שכונת ד")
    # other neighborhoods keep their amber grace
    assert not pipeline._no_amber_area("שכונה ג")
    assert not pipeline._no_amber_area("שכונה ה'")
    assert not pipeline._no_amber_area("הבלוק")
    assert not pipeline._no_amber_area(None)
