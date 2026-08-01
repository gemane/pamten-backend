"""Unit tests for LEI-CDF entity parsing (DB not involved). End-to-end upsert is
covered against a real ArcadeDB in tests/integration/test_gleif_lei_cdf_it.py."""

from app.scraper.gleif_lei_cdf import _country, _entity_props, _founded


def _w(v):
    return {"$": v}


def _rec(lei, name, jurisdiction="US", other_form=None, address=None, created=None,
         elf_code=None, reg=None):
    entity = {"LegalName": _w(name), "LegalJurisdiction": _w(jurisdiction)}
    form = {}
    if other_form is not None:
        form["OtherLegalForm"] = _w(other_form)
    if elf_code is not None:
        form["EntityLegalFormCode"] = _w(elf_code)
    if form:
        entity["LegalForm"] = form
    if address:
        entity["LegalAddress"] = address
    if created:
        entity["EntityCreationDate"] = _w(created)
    if reg is not None:
        authority_id, entity_id = reg
        entity["RegistrationAuthority"] = {
            "RegistrationAuthorityID": _w(authority_id),
            "RegistrationAuthorityEntityID": _w(entity_id),
        }
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


class TestDetailFields:
    """Details section: legal form (ELF), Registered At (RA), display address."""

    def test_resolves_elf_and_ra_codes_to_names(self):
        rec = _rec("L1", "Example Holdings Limited", jurisdiction="GB",
                   elf_code="H0PO", reg=("RA000585", "07428111"),
                   address={"FirstAddressLine": _w("1 Example Street"),
                            "City": _w("London"), "PostalCode": _w("EC1A 1BB"),
                            "Country": _w("GB")})
        _, props = _entity_props(rec, "gleif", 92)
        assert props["legal_form"] == "Private Limited Company"      # H0PO resolved
        assert props["registration_authority"] == "Companies Register"  # RA000585 resolved
        assert props["registration_number"] == "07428111"
        # display address keeps original case, comma-joined (contrast registered_address)
        assert props["address"] == "1 Example Street, London, EC1A 1BB, GB"
        assert props["registered_address"] == "1 example street london ec1a 1bb gb"

    def test_legal_form_falls_back_to_freetext_then_code(self):
        # unlisted ELF code with an OtherLegalForm free text → free text wins
        _, p1 = _entity_props(_rec("L2", "X", elf_code="ZZZZ", other_form="Sociedad X"),
                              "gleif", 92)
        assert p1["legal_form"] == "Sociedad X"
        # unlisted code, no free text → the raw code, not None (nothing silently dropped)
        _, p2 = _entity_props(_rec("L3", "Y", elf_code="ZZZZ"), "gleif", 92)
        assert p2["legal_form"] == "ZZZZ"

    def test_missing_detail_fields_are_none(self):
        _, props = _entity_props(_rec("L4", "Plain Co"), "gleif", 92)
        assert props["legal_form"] is None
        assert props["registration_authority"] is None
        assert props["registration_number"] is None
        assert props["address"] is None
