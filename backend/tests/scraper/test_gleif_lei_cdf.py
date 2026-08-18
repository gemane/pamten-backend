"""Unit tests for LEI-CDF entity parsing (DB not involved). End-to-end upsert is
covered against a real ArcadeDB in tests/integration/test_gleif_lei_cdf_it.py."""

import json
import zipfile

from app.scraper.gleif_lei_cdf import (
    _country, _entity_props, _founded, _validated_credibility, _validation_sources,
    import_lei_cdf_entities,
)


def _w(v):
    return {"$": v}


def _rec(lei, name, jurisdiction="US", other_form=None, address=None, created=None,
         elf_code=None, reg=None, hq=None, validation=None):
    entity = {"LegalName": _w(name), "LegalJurisdiction": _w(jurisdiction)}
    if hq:
        entity["HeadquartersAddress"] = hq
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
    out = {"LEI": _w(lei), "Entity": entity}
    if validation is not None:
        out["Registration"] = {"ValidationSources": _w(validation)}
    return out


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
        assert props["founded"] == 1990                 # headline = year
        assert props["founded_date"] == "1990-01-01"    # full date for the Details section
        assert "main st 1" in props["registered_address"] and props["registered_address"].endswith("de")
        assert props["is_nominee"] is False
        assert props["source_url"] == "https://search.gleif.org/#/record/LEI123"  # deep link, not home page
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
        # RA000585 is Companies House → key the CH number like the PSC import does
        assert props["companies_house_id"] == "07428111"
        # display address keeps original case, comma-joined (contrast registered_address)
        assert props["address"] == "1 Example Street, London, EC1A 1BB, GB"
        assert props["registered_address"] == "1 example street london ec1a 1bb gb"

    def test_multiline_address_keeps_all_lines(self):
        # AdditionalAddressLine is a LIST in the CDF — every line must survive
        rec = _rec("L1b", "Multi Line Co",
                   address={"FirstAddressLine": _w("C/O United Corporate Services"),
                            "AdditionalAddressLine": [_w("800 North State Street"), _w("Suite 304")],
                            "City": _w("Dover"), "Region": _w("US-DE"),
                            "PostalCode": _w("19901"), "Country": _w("US")})
        _, props = _entity_props(rec, "gleif", 92)
        assert props["address"] == (
            "C/O United Corporate Services, 800 North State Street, Suite 304, "
            "Dover, 19901, US")   # region (US-DE) intentionally omitted

    def test_hq_address_surfaces_as_location(self):
        # Real location comes from HeadquartersAddress, not the (registered-agent) legal one
        rec = _rec("L1c", "MercadoLibre Inc", jurisdiction="US-DE",
                   address={"FirstAddressLine": _w("C/O Agent"), "City": _w("Dover"),
                            "Country": _w("US")},
                   hq={"FirstAddressLine": _w("WTC Free Zone"),
                       "AdditionalAddressLine": [_w("Dr. Luis Bonavita 1294")],
                       "City": _w("Montevideo"), "PostalCode": _w("11300"), "Country": _w("UY")})
        _, props = _entity_props(rec, "gleif", 92)
        assert props["country"] == "US"          # jurisdiction (domicile) unchanged
        assert props["hq_city"] == "Montevideo"  # top-of-node location = real HQ
        assert props["hq_country"] == "UY"
        # full HQ address (geocoded to the map pin), distinct from the legal address
        assert props["hq_address"] == "WTC Free Zone, Dr. Luis Bonavita 1294, Montevideo, 11300, UY"
        assert props["address"] == "C/O Agent, Dover, US"

    def test_no_hq_leaves_location_unset(self):
        _, props = _entity_props(_rec("L1d", "No HQ Co"), "gleif", 92)
        assert "hq_city" not in props            # never clobber an existing HQ with null
        assert "hq_country" not in props

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

    def test_companies_house_id_only_for_uk_registrar(self):
        # a non-Companies-House RA must NOT be treated as a CH number
        _, props = _entity_props(_rec("L5", "US Co", reg=("RA000602", "3112015")), "gleif", 92)
        assert "companies_house_id" not in props
        assert props["registration_number"] == "3112015"


# Valid-format LEIs (20 chars, [0-9A-Z]) so the byte-scan regex matches, like the real file.
LEI_A = "AAAA1111AAAA1111AAAA"
LEI_B = "BBBB2222BBBB2222BBBB"
LEI_C = "CCCC3333CCCC3333CCCC"


def _lei_zip(tmp_path, leis, indent=2):
    """A tiny LEI-CDF golden-copy zip with one record per given LEI, in order.
    `indent` mirrors the real golden copy, which is pretty-printed (whitespace between
    the LEI tokens) — the fast-path regex must tolerate it."""
    records = [{"LEI": _w(lei), "Entity": {"LegalName": _w(f"Co {lei}"),
                                           "LegalJurisdiction": _w("US")}} for lei in leis]
    zpath = tmp_path / "lei2.json.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("lei2.json", json.dumps({"records": records}, indent=indent))
    return str(zpath)


class TestOnlyLeis:
    """--only / --only-file curated test subset: import just the listed LEIs."""

    def test_imports_only_listed_leis(self, tmp_path, monkeypatch):
        from app.scraper import bulk_import
        written = []
        monkeypatch.setattr(bulk_import._BatchWriter, "entity",
                            lambda self, nid, props: written.append(nid))
        monkeypatch.setattr(bulk_import._BatchWriter, "flush", lambda self: None)

        z = _lei_zip(tmp_path, [LEI_A, LEI_B, LEI_C])
        counts = import_lei_cdf_entities(z, "src", 92, only_leis={LEI_B})

        assert written == [f"lei:{LEI_B}"]   # only the listed one written
        assert counts["entities"] == 1

    def test_no_filter_imports_everything(self, tmp_path, monkeypatch):
        from app.scraper import bulk_import
        written = []
        monkeypatch.setattr(bulk_import._BatchWriter, "entity",
                            lambda self, nid, props: written.append(nid))
        monkeypatch.setattr(bulk_import._BatchWriter, "flush", lambda self: None)

        z = _lei_zip(tmp_path, [LEI_A, LEI_B])
        counts = import_lei_cdf_entities(z, "src", 92)
        assert counts["entities"] == 2 and set(written) == {f"lei:{LEI_A}", f"lei:{LEI_B}"}


class TestFastLeiScan:
    """The fast byte-scan record iterator (`_iter_records_for_leis`) used for subsets."""

    def _stream(self, tmp_path, leis, indent):
        import zipfile as zf_mod
        path = _lei_zip(tmp_path, leis, indent=indent)
        z = zf_mod.ZipFile(path)
        return z.open(z.namelist()[0])

    def test_extracts_targets_across_chunk_boundaries(self, tmp_path):
        from app.scraper.gleif_lei_cdf import _iter_records_for_leis, _v
        raw = self._stream(tmp_path, [LEI_A, LEI_B, LEI_C], indent=4)
        # chunk_size=8 forces records (and even the marker) to span many chunk reads
        got = [_v(r.get("LEI")) for r in _iter_records_for_leis(raw, {LEI_A, LEI_C}, chunk_size=8)]
        assert sorted(got) == sorted([LEI_A, LEI_C])   # B skipped, boundaries handled

    def test_compact_json_also_matches(self, tmp_path):
        # a compact (no-whitespace) file must work too
        from app.scraper.gleif_lei_cdf import _iter_records_for_leis, _v
        records = [{"LEI": _w(LEI_A), "Entity": {"LegalName": _w("A")}}]
        zpath = tmp_path / "c.json.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("c.json", json.dumps({"records": records}, separators=(",", ":")))
        z = zipfile.ZipFile(zpath)
        raw = z.open(z.namelist()[0])
        got = [_v(r.get("LEI")) for r in _iter_records_for_leis(raw, {LEI_A})]
        assert got == [LEI_A]


class TestHowFarGleifCheckedIt:
    """`ValidationSources` — the only per-record quality signal in the file.

    It says whether an LOU corroborated the record against the business register
    or simply took the entity's word for it. Every GLEIF record used to score the
    source's flat 92 either way, so a self-declared name outranked a
    registry-checked one from elsewhere on the strength of its source alone.
    """

    def test_the_raw_value_is_kept(self):
        # Stored as GLEIF's own enum: it is published with a defined meaning, and
        # the UI can put it in prose without a translation baked into the graph.
        _, props = _entity_props(_rec("L1", "Checked Co", validation="FULLY_CORROBORATED"),
                                 "gleif", 92)
        assert props["validation_sources"] == "FULLY_CORROBORATED"

    def test_a_corroborated_record_keeps_the_full_source_score(self):
        _, props = _entity_props(_rec("L2", "Checked Co", validation="FULLY_CORROBORATED"),
                                 "gleif", 92)
        assert props["name_credibility"] == 92

    def test_a_partly_checked_record_scores_lower(self):
        _, props = _entity_props(_rec("L3", "Half Co", validation="PARTIALLY_CORROBORATED"),
                                 "gleif", 92)
        assert props["name_credibility"] == 88

    def test_an_unchecked_record_scores_lower_still(self):
        _, props = _entity_props(_rec("L4", "Said So Co", validation="ENTITY_SUPPLIED_ONLY"),
                                 "gleif", 92)
        assert props["name_credibility"] == 82

    def test_pending_validation_counts_as_unchecked(self):
        # Nobody has corroborated it *yet*, which is the same evidential state as
        # nobody having corroborated it at all.
        _, props = _entity_props(_rec("L5", "Waiting Co", validation="PENDING"), "gleif", 92)
        assert props["name_credibility"] == 82

    def test_the_ladder_is_ordered(self):
        def score(value):
            return _validated_credibility({"Registration": {"ValidationSources": _w(value)}}, 92)

        assert (score("FULLY_CORROBORATED") > score("PARTIALLY_CORROBORATED")
                > score("ENTITY_SUPPLIED_ONLY"))

    def test_even_an_unchecked_record_outranks_the_community_sources(self):
        # A company's own statement of its own legal name is still good evidence —
        # better than a Wikidata label, which is usually the common name rather
        # than the registered one. The deduction is a tie-break, not a demotion.
        assert _validated_credibility(
            {"Registration": {"ValidationSources": _w("ENTITY_SUPPLIED_ONLY")}}, 92) > 80

    def test_a_record_that_says_nothing_is_not_penalised(self):
        # The deduction is for GLEIF telling us a record is unverified, never for
        # a field we failed to read.
        _, props = _entity_props(_rec("L6", "Quiet Co"), "gleif", 92)
        assert props["name_credibility"] == 92
        assert "validation_sources" not in props

    def test_an_unrecognised_value_is_not_penalised_either(self):
        _, props = _entity_props(_rec("L7", "Odd Co", validation="SOMETHING_NEW"), "gleif", 92)
        assert props["name_credibility"] == 92
        assert props["validation_sources"] == "SOMETHING_NEW"

    def test_the_score_never_goes_negative(self):
        assert _validated_credibility(
            {"Registration": {"ValidationSources": _w("ENTITY_SUPPLIED_ONLY")}}, 3) == 0

    def test_reading_the_value_off_a_record(self):
        assert _validation_sources(_rec("L8", "X", validation="FULLY_CORROBORATED")) \
            == "FULLY_CORROBORATED"
        assert _validation_sources(_rec("L9", "X")) is None
