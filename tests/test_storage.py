"""storage — the vote ledger (one vote per user, final) and the file_id cache
that keeps top-N albums alive after Facebook URLs expire."""
import config
import storage
from models import ListingExtract, PipelineResult, Status


def _res(key):
    e = ListingExtract(is_apartment_ad=True, price_per_room_ils=1500,
                       available_rooms_count=2, total_roommates_in_apt=3,
                       street_address_or_neighborhood="רגר 1")
    return PipelineResult(status=Status.MATCH, dedup_key=key, location_tier="GREEN",
                          score=80, images=["http://u1", "http://u2"], extract=e)


def test_vote_is_once_per_user_and_final(temp_db):
    k = "phone:501234567"
    assert storage.set_mark(k, "u1", "saved") is True      # first vote records
    assert storage.set_mark(k, "u1", "saved") is False     # repeat rejected
    assert storage.set_mark(k, "u1", "dismissed") is False  # no flipping
    assert storage.get_user_mark(k, "u1") == "saved"        # original stands


def test_counts_and_net_adjustment(temp_db):
    k = "phone:1"
    storage.set_mark(k, "u1", "saved")
    storage.set_mark(k, "u2", "saved")
    storage.set_mark(k, "u3", "dismissed")
    assert storage.mark_counts(k) == {"saved": 2, "dismissed": 1}
    assert storage.mark_adjustment(k) == config.MARK_SCORE_DELTA   # 2*Δ - 1*Δ = Δ


def test_effective_score_is_base_plus_votes(temp_db):
    k = "hash:xyz"
    assert storage.base_score(k) == 0                       # no listing row yet
    storage.set_mark(k, "u1", "saved")
    assert storage.effective_score(k, base=10) == 10 + config.MARK_SCORE_DELTA


def test_file_ids_roundtrip_and_no_wipe(temp_db):
    k = "phone:2"
    storage.save_listing(_res(k))
    assert storage.get_images(k) == ["http://u1", "http://u2"]
    assert storage.get_file_ids(k) == []
    storage.set_file_ids(k, ["AAA", "BBB"])
    assert storage.get_file_ids(k) == ["AAA", "BBB"]
    storage.set_file_ids(k, [])                             # empty must be a no-op
    assert storage.get_file_ids(k) == ["AAA", "BBB"]


def test_save_listing_persists_score(temp_db):
    k = "phone:3"
    storage.save_listing(_res(k))
    assert storage.base_score(k) == 80
