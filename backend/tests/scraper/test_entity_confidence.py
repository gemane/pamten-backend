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


class TestWikidataGleifBridge:
    """The Microsoft case: why the pair used to sit unmerged, and what changes."""

    GLEIF    = dict(lei_id="INR2EJN1ERAN0W5ZP974", country="US", registered_address="One Microsoft Way")
    WIKIDATA = dict(wikidata_id="Q2283", sec_cik="0000789019", country="US")

    def test_pair_without_a_shared_id_is_not_auto_merged(self):
        # What we had: GLEIF holds lei_id, Wikidata holds wikidata_id + sec_cik.
        # No overlap, so nothing above "medium" — below the auto-merge threshold,
        # which is why one node had the ownership graph and the other the
        # executives, with no way for a user to see both.
        verdict = _group_confidence([self.GLEIF, self.WIKIDATA])
        assert verdict != "definitive"

    def test_lei_from_wikidata_makes_the_pair_definitive(self):
        # With P1278 read from Wikidata the two carry the same lei_id, which is
        # exactly the signal the dedup already looks for.
        bridged = {**self.WIKIDATA, "lei_id": "INR2EJN1ERAN0W5ZP974"}
        assert _group_confidence([self.GLEIF, bridged]) == "definitive"

    def test_a_differing_lei_is_not_definitive(self):
        # Two genuinely different companies that happen to share a name must not
        # be merged just because both now carry an LEI.
        other = {**self.WIKIDATA, "lei_id": "549300G0CFPGEF6X2043"}
        assert _group_confidence([self.GLEIF, other]) != "definitive"
