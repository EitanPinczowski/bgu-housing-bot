"""resolve_unknowns.resolve: splits the backlog into resolved / still-unknown and
proposes fuzzy hits as static-table pins (geocoder mocked, no network)."""
import geocode
import resolve_unknowns


def test_resolve_categorizes_and_proposes(monkeypatch):
    fake = {
        "רחוב קדש": ((31.25, 34.80), "overpass"),      # fuzzy hit -> proposal
        "הבלוק": ((31.259, 34.796), "static"),         # already pinned -> not a proposal
        "5 דקות מהאוניברסיטה": (None, None),           # vague -> still unknown
    }
    monkeypatch.setattr(geocode, "geocode_detailed", lambda a: fake[a])
    resolved, still, proposals = resolve_unknowns.resolve(list(fake))
    assert set(resolved) == {"רחוב קדש", "הבלוק"}
    assert still == ["5 דקות מהאוניברסיטה"]
    assert set(proposals) == {"רחוב קדש"}              # only the fuzzy one is proposed
    assert resolve_unknowns._is_vague("5 דקות מהאוניברסיטה") is True
    assert resolve_unknowns._is_vague("רחוב קדש") is False
