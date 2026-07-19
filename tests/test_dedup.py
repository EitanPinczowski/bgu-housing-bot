"""storage.make_dedup_key — the key that stops the same apartment being stored
twice. Phone-based when possible (survives reposts/cross-posting), else a hash."""
import storage
from models import ListingExtract


def test_phone_key_survives_formatting():
    a = ListingExtract(is_apartment_ad=True, contact_phone_or_link="050-123-4567")
    b = ListingExtract(is_apartment_ad=True, contact_phone_or_link="0501234567")
    c = ListingExtract(is_apartment_ad=True, contact_phone_or_link="tel: 050 123 4567")
    assert storage.make_dedup_key(a).startswith("phone:")
    assert storage.make_dedup_key(a) == storage.make_dedup_key(b) == storage.make_dedup_key(c)


def test_hash_key_when_no_phone_is_stable():
    fields = dict(is_apartment_ad=True, street_address_or_neighborhood="רגר 12",
                  price_per_room_ils=1500, available_rooms_count=2, total_roommates_in_apt=3)
    k1 = storage.make_dedup_key(ListingExtract(**fields))
    k2 = storage.make_dedup_key(ListingExtract(**fields))
    assert k1.startswith("hash:")
    assert k1 == k2


def test_different_listings_get_different_keys():
    a = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="רגר 12",
                       price_per_room_ils=1500, available_rooms_count=2)
    b = ListingExtract(is_apartment_ad=True, street_address_or_neighborhood="בן גוריון 5",
                       price_per_room_ils=1500, available_rooms_count=2)
    assert storage.make_dedup_key(a) != storage.make_dedup_key(b)
