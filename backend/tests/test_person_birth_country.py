"""A person's birth country travels with the birthplace it qualifies.

The panel says "Rio de Janeiro, Brazil": the country is Wikidata's P17 of the
P19 place, written beside that place. A merge must not pair one birthplace with
the country of another.
"""
import pytest

from app.routers.persons import _merged_birth_country


@pytest.mark.parametrize("keep, dup, expected", [
    ({"birth_place": "Lages", "birth_country": "BR"}, {}, "BR"),
    ({"birth_place": "Lages"}, {"birth_place": "Lages", "birth_country": "BR"}, "BR"),
    # the kept place stays; the duplicate's country belongs to its OTHER place
    ({"birth_place": "Lages"}, {"birth_place": "Porto", "birth_country": "PT"}, None),
    # no kept place: the duplicate's place and its country come together
    ({}, {"birth_place": "Porto", "birth_country": "PT"}, "PT"),
    ({}, {}, None),
])
def test_merged_birth_country(keep, dup, expected):
    assert _merged_birth_country(keep, dup) == expected
