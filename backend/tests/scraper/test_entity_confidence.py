"""Confidence tiers for same-name entity groups (drives auto-merge vs review).

The full merge is covered against a real ArcadeDB in
tests/integration/test_entity_dedup_it.py."""

from app.scraper.maintenance import _group_confidence


def _m(**kw):
    return kw


class TestGroupConfidence:
    def test_shared_hard_id_is_definitive(self):
        # any shared hard external id ⇒ same company (no name/Wikidata needed)
        assert _group_confidence([_m(lei_id="X"), _m(lei_id="X")]) == "definitive"
        assert _group_confidence([_m(sec_cik="789019"), _m(sec_cik="789019")]) == "definitive"
        assert _group_confidence([_m(companies_house_id="07434180"),
                                  _m(companies_house_id="07434180")]) == "definitive"

    def test_same_registered_address_is_high(self):
        assert _group_confidence([_m(registered_address="1 a st london gb"),
                                  _m(registered_address="1 a st london gb")]) == "high"

    def test_distinct_ids_not_definitive(self):
        # different LEIs with nothing else in common ⇒ not a confident merge
        assert _group_confidence([_m(lei_id="X"), _m(lei_id="Y")]) == "low"
