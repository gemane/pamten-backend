"""Unit tests for Companies House PSC parsing (DB not involved). End-to-end write
is covered against a real ArcadeDB in tests/integration/test_ch_psc_it.py."""

from app.scraper.companies_house_psc import (
    _band_floor, _birth_date, _control, _entity_psc_id, _psc_name,
)


class TestBandAndControl:
    def test_band_floor(self):
        assert _band_floor("ownership-of-shares-75-to-100-percent") == 75
        assert _band_floor("voting-rights-25-to-50-percent") == 25
        assert _band_floor("right-to-appoint-and-remove-directors") is None

    def test_voting_and_economic_kept_separate(self):
        stake, voting, otype, its = _control([
            "ownership-of-shares-75-to-100-percent", "voting-rights-75-to-100-percent"])
        assert stake == 75 and voting == 75 and otype == "controlling"
        assert its == ["ownership-of-shares-75-to-100-percent", "voting-rights-75-to-100-percent"]

    def test_shares_only_derives_type(self):
        stake, voting, otype, _ = _control(["ownership-of-shares-75-to-100-percent"])
        assert stake == 75 and voting is None and otype == "majority"   # derived, not a voting flag

    def test_appointment_is_controlling_without_a_stake(self):
        stake, voting, otype, _ = _control(["right-to-appoint-and-remove-directors"])
        assert stake is None and voting is None and otype == "controlling"

    def test_empty(self):
        assert _control([]) == (None, None, "minority", [])


class TestPscFields:
    def test_name_from_field_or_elements(self):
        assert _psc_name({"name": "Mr John Smith"}) == "Mr John Smith"
        assert _psc_name({"name_elements": {"forename": "Jane", "middle_name": "Q",
                                            "surname": "Doe"}}) == "Jane Q Doe"
        assert _psc_name({}) is None

    def test_birth_date(self):
        assert _birth_date({"date_of_birth": {"year": 1951, "month": 8}}) == "1951-08"
        assert _birth_date({"date_of_birth": {"year": 1951}}) == "1951"
        assert _birth_date({}) == ""

    def test_entity_psc_id_uses_uk_company_number(self):
        node_id, chid = _entity_psc_id({"identification": {
            "registration_number": "00686734", "country_registered": "England & Wales"},
            "links": {"self": "/company/x/.../abc"}})
        assert node_id == "gb-coh:00686734" and chid == "00686734"

    def test_entity_psc_id_foreign_uses_self_link(self):
        node_id, chid = _entity_psc_id({"identification": {
            "registration_number": "999", "country_registered": "Delaware"},
            "links": {"self": "/company/x/corporate-entity/zzz"}})
        assert node_id == "chpsc:/company/x/corporate-entity/zzz" and chid is None
