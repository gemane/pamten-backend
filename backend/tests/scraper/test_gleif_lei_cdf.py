"""Unit tests for LEI-CDF entity parsing (DB not involved). End-to-end upsert is
covered against a real ArcadeDB in tests/integration/test_gleif_lei_cdf_it.py."""

from app.scraper.gleif_lei_cdf import _country, _entity_props, _founded


def _w(v):
    return {"$": v}


def _rec(lei, name, jurisdiction="US", other_form=None, address=None, created=None):
    entity = {"LegalName": _w(name), "LegalJurisdiction": _w(jurisdiction)}
    if other_form is not None:
        entity["LegalForm"] = {"OtherLegalForm": _w(other_form)}
    if address:
        entity["LegalAddress"] = address
    if created:
        entity["EntityCreationDate"] = _w(created)
    return {"LEI": _w(lei), "Entity": entity}


class TestCountry:
    def test_iso2_and_subdivision(self):
        assert _country({"LegalJurisdiction": _w("US")}) == "US"
        assert _country({"LegalJurisdiction": _w("US-DE")}) == "US"   # subdivision stripped
        assert _country({}) is None

    def test_falls_back_to_hq_country(self):
        assert _country({"HeadquartersAddress": {"Country": _w("DE")}}) == "DE"


class TestFounded:
    def test_year_from_creation_date(self):
        assert _founded({"EntityCreationDate": _w("1997-03-01T00:00:00Z")}) == 1997
        assert _founded({}) is None


class TestEntityProps:
    def test_maps_core_fields(self):
        rec = _rec("LEI123", "Acme AG", jurisdiction="DE",
                   address={"FirstAddressLine": _w("Main St 1"), "City": _w("Berlin"),
                            "PostalCode": _w("10115"), "Country": _w("DE")},
                   created="1990-01-01")
        node_id, props = _entity_props(rec, "gleif", 92)
        assert node_id == "lei:LEI123"
        assert props["name"] == "Acme AG"
        assert props["country"] == "DE"
        assert props["lei_id"] == "LEI123"
        assert props["founded"] == 1990
        assert "main st 1" in props["registered_address"] and props["registered_address"].endswith("de")
        assert props["is_nominee"] is False
        assert "type" not in props            # generic → type left untouched

    def test_legal_form_refines_type(self):
        _, props = _entity_props(_rec("L1", "Fidelity Leveraged Company Stock Fund",
                                      other_form="FUND"), "gleif", 92)
        assert props["type"] == "fund"

    def test_nominee_flagged(self):
        _, props = _entity_props(_rec("L2", "Talbot Nominees Limited"), "gleif", 92)
        assert props["is_nominee"] is True

    def test_no_lei_or_name(self):
        assert _entity_props({"Entity": {"LegalName": _w("X")}}, "gleif", 92) is None
        assert _entity_props({"LEI": _w("L")}, "gleif", 92) is None
