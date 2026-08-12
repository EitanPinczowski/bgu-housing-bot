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


# --- `פינת X` is a bearing, not an address ------------------------------------------
#
# Measured 2026-08-12 on the live DB. `_norm_addr` took the LAST street named and hung
# the house number on it, so the key asserted a flat on the CROSS street:
#     שדרות רגר 93 פינת שלמה המלך  ->  שלמה המלך|93     (the flat is on רגר)
#     הורקנוס 45 פינת סוסו הכהן    ->  סוסו הכהן|45     (the flat is on הורקנוס)
# The geocoder was already right about all of these, which is what made it a dedup bug
# and not a placement one — and 579 of 10,402 archived posts carry the construct.

def _addr(text):
    return ListingExtract(is_apartment_ad=True, street_address_or_neighborhood=text,
                          contact_phone_or_link="050-8220245")


def test_a_corner_clause_does_not_steal_the_house_number():
    """The number belongs to the street BEFORE `פינת`. This is the pair that was sitting
    in the DB as two rows for one flat, under two different streets."""
    assert storage._norm_addr("שדרות רגר 93 פינת שלמה המלך, הבלוק") == "שדרות יצחק רגר|93"
    assert storage._norm_addr("רגר 93, הבלוק") == "שדרות יצחק רגר|93"
    assert storage.make_dedup_key(_addr("שדרות רגר 93 פינת שלמה המלך, הבלוק")) == \
           storage.make_dedup_key(_addr("רגר 93, הבלוק"))


def test_every_corner_form_in_the_live_data_resolves_to_the_primary_street():
    """Each of these was a real stored address on 2026-08-12. `פינה` is accepted beside
    `פינת`, and a parenthesised clause is cut without taking the rest of the line."""
    for text, want in (
            ("הורקנוס 45 פינת סוסו הכהן", "יוחנן הורקנוס|45"),
            ("רחוב הבשור 19, פינת סוסו הכהן", "הבשור|19"),
            ("רחוב הבשור 19 (פינת סוסו הכהן), צמודה לבנג'י", "הבשור|19"),
            ("הורקנוס 37 פינת סוסו", "יוחנן הורקנוס|37"),
    ):
        assert storage._norm_addr(text) == want, text


def test_a_corner_with_no_number_of_its_own_stays_unnumbered():
    """`רחוב השלום פינת קלישר` names no house number once the clause goes, so it returns
    None and falls back to phone-only keying — it must NOT borrow the cross street's."""
    assert storage._norm_addr("רחוב השלום פינת קלישר, שכונה ג") is None
    assert storage._norm_addr("רחוב השלום פינת בן גוריון, באר שבע") is None


def test_a_number_that_belongs_to_the_cross_street_stops_being_asserted():
    """`הורקנוס פינת סוסו הכהן 0` hung a bogus house 0 on the cross street: the key was
    `סוסו הכהן|0`, a flat that does not exist. With the clause gone there is no number
    left, so this takes the documented fallback — the scrubbed RAW wording — rather than
    a structured key. That is the conservative end: two reads with the same wording still
    collapse, and nothing claims a street and number the post never gave."""
    got = storage._norm_addr("הורקנוס פינת סוסו הכהן 0")
    assert got == "הורקנוס פינת סוסו הכהן 0", got
    assert "|" not in got, "it must not assert a canonical street|number"


def test_an_address_without_a_corner_is_untouched():
    """The strip must not disturb the phrasings `_norm_addr` already solved, nor the
    bearing forms the geocoder relies on keeping."""
    for text, want in (("רגר 93", "שדרות יצחק רגר|93"),
                       ("רגר 5, ליד האוניברסיטה", "שדרות יצחק רגר|5"),
                       ("ו' הישנה, בן מתיתיהו 13", "יוסף בן מתיתיהו|13"),
                       ("רח' וינגייט 64", "וינגייט|64")):
        assert storage._norm_addr(text) == want, text


def test_a_flat_already_stored_under_the_cross_street_is_still_recognised():
    """THE ROWS ARE ALREADY IN `seen` UNDER THE WRONG STREET. Renaming them without
    emitting the old form would make every corner flat re-alert as brand new — the same
    failure the legacy `_norm_addr_raw` keys exist to prevent."""
    keys = storage.dedup_keys(_addr("שדרות רגר 93 פינת שלמה המלך, הבלוק"))
    assert "phone:508220245|שדרות יצחק רגר|93" in keys, keys     # the new, correct key
    assert "phone:508220245|שלמה המלך|93" in keys, keys          # the one already stored
