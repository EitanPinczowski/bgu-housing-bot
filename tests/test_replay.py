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
