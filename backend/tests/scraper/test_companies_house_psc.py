"""Unit tests for Companies House PSC parsing (DB not involved). End-to-end write
is covered against a real ArcadeDB in tests/integration/test_ch_psc_it.py."""

import json
import zipfile

from app.scraper.companies_house_psc import (
    _band_floor, _birth_date, _control, _entity_psc_id, _iso2_country, _psc_address,
    _psc_name, import_ch_psc,
)


class TestPscAddress:
    """A corporate PSC's own correspondence address → its address + map location."""

    def test_full_address_city_and_country(self):
        addr = {"premises": "The Manor", "address_line_1": "Boddington Lane",
                "address_line_2": "Boddington", "locality": "Cheltenham",
                "country": "England", "postal_code": "GL51 0TJ"}
        display, city, country = _psc_address(addr)
        assert display == "The Manor, Boddington Lane, Boddington, Cheltenham, GL51 0TJ, England"
        assert city == "Cheltenham"
        assert country == "GB"

    def test_empty_address(self):
        assert _psc_address(None) == (None, None, None)

    def test_iso2_maps_uk_subdivisions_and_foreign(self):
        assert _iso2_country("England & Wales") == "GB"
        assert _iso2_country("Scotland") == "GB"
        assert _iso2_country("Jersey") == "JE"
        assert _iso2_country("Ireland") == "IE"
        assert _iso2_country(None) is None

    def test_process_stamps_the_corporate_psc_address(self, monkeypatch):
        from app.scraper import companies_house_psc as m
        captured = {}
        monkeypatch.setattr(m, "_entity",
                            lambda *a, **k: captured.update(k) or "gb-coh:00686734")
        rec = {"company_number": "07434180", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Robert Hitchins Limited",
            "address": {"premises": "The Manor", "address_line_1": "Boddington Lane",
                        "locality": "Cheltenham", "country": "England", "postal_code": "GL51 0TJ"},
            "identification": {"registration_number": "00686734", "country_registered": "England & Wales"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
        }}
        assert m._process(rec, m._BatchWriter(), "src", 97) == "entity"
        assert captured["hq_city"] == "Cheltenham"       # → on the map
        assert captured["hq_country"] == "GB"
        assert "The Manor" in captured["registered_address"]


def _psc_zip(tmp_path, company_numbers):
    """A tiny PSC snapshot zip — one individual-PSC line per given company number."""
    lines = "\n".join(json.dumps({
        "company_number": cn,
        "data": {"kind": "individual-person-with-significant-control",
                 "name": f"Mr {cn}",
                 "natures_of_control": ["ownership-of-shares-75-to-100-percent"]},
    }) for cn in company_numbers)
    zpath = tmp_path / "psc.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("psc.txt", lines)
    return str(zpath)


class TestOnlyCompanies:
    """--only / --only-file curated subset: import just the listed company numbers."""

    def test_filters_to_listed_company_and_stops_early(self, tmp_path, monkeypatch):
        from app.scraper import companies_house_psc as m
        seen = []
        monkeypatch.setattr(m, "_process",
                            lambda rec, *a: seen.append(rec["company_number"]) or "person")

        z = _psc_zip(tmp_path, ["00000001", "00000002", "00000003"])
        counts = import_ch_psc(z, "src", 97, only_companies={"00000002"})

        assert seen == ["00000002"]          # only the listed company processed
        assert counts["persons"] == 1
        assert counts["records"] == 1        # non-matches rejected before the JSON parse

    def test_no_filter_processes_all(self, tmp_path, monkeypatch):
        from app.scraper import companies_house_psc as m
        monkeypatch.setattr(m, "_process", lambda rec, *a: "person")
        z = _psc_zip(tmp_path, ["00000001", "00000002"])
        counts = import_ch_psc(z, "src", 97)
        assert counts["persons"] == 2


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
