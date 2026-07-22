"""replay --only-bare-nbhd: the predicate that selects which archived posts get a
re-extraction (bare neighborhood = a whole area with no specific street)."""
import replay
from models import ListingExtract


def _post(location):
    e = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=location)
    return {"parsed_json": e.model_dump_json()}


def test_is_bare_nbhd_post():
    assert replay._is_bare_nbhd_post(_post("שכונה ג"))          # bare neighborhood
    assert replay._is_bare_nbhd_post(_post("שכונה ד'"))
    assert not replay._is_bare_nbhd_post(_post("רינגלבלום 5"))  # a specific street
    assert not replay._is_bare_nbhd_post(_post("רחוב הנדיב"))   # a named street
    assert not replay._is_bare_nbhd_post(_post(None))           # no location
    assert not replay._is_bare_nbhd_post({"parsed_json": None}) # no parse
    assert not replay._is_bare_nbhd_post({"parsed_json": "not json"})
