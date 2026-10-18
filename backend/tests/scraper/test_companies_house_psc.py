"""Unit tests for Companies House PSC parsing (DB not involved). End-to-end write
is covered against a real ArcadeDB in tests/integration/test_ch_psc_it.py."""

import json
import zipfile

from app.scraper.companies_house_psc import (
    _band_floor, _birth_date, _control, _ENTITY_KINDS, _entity_psc_id, _iso2_country,
    _PERSON_KINDS, _psc_address, _psc_name, _SKIP_KINDS, import_ch_psc, psc_record, psc_slug_id)


class TestPscAddress:
    """A corporate PSC's own correspondence address → its address + map location."""

    def test_full_address_city_and_country(self):
        addr = {"premises": "The Manor", "address_line_1": "Boddington Lane",
                "address_line_2": "Boddington", "locality": "Cheltenham",
                "country": "England", "postal_code": "GL51 0TJ"}
        out = _psc_address(addr)
        assert out["registered_address"] == "The Manor, Boddington Lane, Boddington, Cheltenham, GL51 0TJ, England"
        assert out["hq_city"] == "Cheltenham"
        assert out["hq_country"] == "GB"
        # the parts, for a structured geocode: the street is the lines joined in
        # the register's order, the postcode its own field
        assert out["hq_street"] == "The Manor, Boddington Lane, Boddington"
        assert out["hq_postcode"] == "GL51 0TJ"

    def test_empty_address(self):
        assert _psc_address(None) == {"registered_address": None, "hq_city": None, "hq_country": None,
                                      "hq_street": None, "hq_postcode": None}

    def test_blank_parts_are_none_not_empty_strings(self):
        out = _psc_address({"premises": " ", "locality": "Leeds", "postal_code": ""})
        assert (out["hq_street"], out["hq_postcode"], out["registered_address"]) == (None, None, "Leeds")

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
        # the parts reach the writer (they were mapped and dropped until 2026-10-18)
        assert captured["extra"]["hq_street"] == "The Manor, Boddington Lane"
        assert captured["extra"]["hq_postcode"] == "GL51 0TJ"

    def test_a_record_without_address_parts_writes_none_of_them(self, monkeypatch):
        """A bulk SET of null would blank the parts GLEIF wrote on a merged node."""
        from app.scraper import companies_house_psc as m
        captured = {}
        monkeypatch.setattr(m, "_entity",
                            lambda *a, **k: captured.update(k) or "chpsc:x")
        rec = {"company_number": "07434180", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Nameplate Holdings", "address": {"locality": "Leeds", "country": "England"},
            "identification": {"country_registered": "England"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
        }}
        assert m._process(rec, m._BatchWriter(), "src", 97) == "entity"
        assert captured["hq_city"] == "Leeds"
        assert "hq_street" not in captured["extra"] and "hq_postcode" not in captured["extra"]


class TestFirstYearLowerBounds:
    """A notification inside the register's first year on a company that
    predates the register is when the company first SAID it (its first
    confirmation statement), not when the control began. The PSC import cannot
    know the incorporation date, so this runs after ch-company-data."""

    def _run(self, pages, edges, company_ids=("gb-coh:1", "gb-coh:2", "gb-coh:3"), apply=True):
        from app.scraper.companies_house_psc import mark_first_year_lower_bounds
        sql, scripts = [], []

        def run_sql(cmd, *a, **k):
            sql.append(cmd)
            return edges if "expand(inE('OWNS'))" in cmd else pages
        res = mark_first_year_lower_bounds(list(company_ids) if company_ids else None, apply=apply,
                                           run_sql=run_sql,
                                           run_sqlscript=lambda s, p=None: scripts.append((s, p)))
        return res, sql, scripts

    PAGE = [{"id": "gb-coh:1", "rid": "#1:1", "founded_date": "1903-08-28"},   # predates the register
            {"id": "gb-coh:2", "rid": "#1:2", "founded_date": "2016-09-01"},   # named its PSC at incorporation
            {"id": "gb-coh:3", "rid": "#1:3", "founded_date": None}]           # not enriched yet
    EDGES = [{"rid": "#9:1", "i": "#1:1", "s": "2016-07-04"},
             {"rid": "#9:2", "i": "#1:1", "s": "2016-07-04"},                  # a second PSC, same day
             {"rid": "#9:3", "i": "#1:1", "s": "2016-10-13"}]

    def test_only_companies_incorporated_before_the_register_are_expanded(self):
        res, sql, scripts = self._run(self.PAGE, self.EDGES)
        assert res == {"companies_before_register": 1, "edges": 3, "claims": 2, "applied": True}
        expand = next(c for c in sql if "expand(inE('OWNS'))" in c)
        assert "FROM [#1:1]" in expand and "#1:2" not in expand and "#1:3" not in expand
        # the edge predicate: PSC, no basis yet, inside the first year but not its first day
        assert ("WHERE filing_type = 'PSC' AND since_basis IS NULL "
                "AND since > '2016-04-06' AND since <= '2017-04-20'") in expand

    def test_edges_are_marked_by_rid_and_claims_by_company_and_day(self):
        _, _, scripts = self._run(self.PAGE, self.EDGES)
        assert len(scripts) == 1
        script, params = scripts[0]
        stmts = script.split(";")
        assert stmts[:3] == [f"UPDATE #9:{n} SET since_basis = 'register_first_year'" for n in (1, 2, 3)]
        assert len(stmts) == 5 and all("UPDATE Claim SET since_basis = 'register_first_year'" in s
                                       and "kind = 'owns' AND filing_type = 'PSC'" in s
                                       and "since_basis IS NULL" in s for s in stmts[3:])
        assert params == {"t0": "gb-coh:1", "s0": "2016-07-04", "t1": "gb-coh:1", "s1": "2016-10-13"}

    def test_a_dry_run_counts_and_writes_nothing(self):
        res, _, scripts = self._run(self.PAGE, self.EDGES, apply=False)
        assert (res["edges"], res["claims"], res["applied"]) == (3, 2, False)
        assert scripts == []

    def test_scoped_ids_are_read_by_id_list_and_nothing_is_expanded_without_old_companies(self):
        res, sql, scripts = self._run(self.PAGE[1:], [], company_ids=("gb-coh:2", "gb-coh:3", "it's"))
        assert sql == ["SELECT id, @rid AS rid, founded_date FROM Entity "
                       "WHERE id IN ['gb-coh:2', 'gb-coh:3', 'it\\'s']"]
        assert res["companies_before_register"] == 0 and scripts == []

    def test_a_full_run_walks_every_uk_company_by_the_id_index(self, monkeypatch):
        from app.db import paging
        seen = {}
        monkeypatch.setattr(paging, "iter_id_pages",
                            lambda vtype, prefix="", page=5000, columns="": seen.update(
                                vtype=vtype, prefix=prefix, columns=columns) or iter([self.PAGE]))
        res, sql, _ = self._run(self.PAGE, self.EDGES, company_ids=None)
        assert seen == {"vtype": "Entity", "prefix": "gb-coh:", "columns": "id, @rid AS rid, founded_date"}
        assert res["edges"] == 3 and not any("WHERE id IN" in c for c in sql)


def _psc_zip(tmp_path, company_numbers):
    """A tiny PSC snapshot zip — one individual-PSC line per given company number."""
    lines = "\n".join(json.dumps({
        "company_number": cn,
        "data": {"kind": "individual-person-with-significant-control",
                 "name": f"Mr {cn}",
                 "links": {"self": f"/company/{cn}/persons-with-significant-control/individual/{cn}"},
                 "natures_of_control": ["ownership-of-shares-75-to-100-percent"]},
    }) for cn in company_numbers)
    zpath = tmp_path / "psc.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("psc.txt", lines)
    return str(zpath)


class TestOnlyCompanies:
    """--only / --only-file curated subset: import just the listed company numbers."""

    def test_filters_to_the_listed_company(self, tmp_path, monkeypatch):
        # Named for what it checks: the raw-bytes prefilter. It used to be called
        # "…and stops early", but with one target among three lines the prefilter
        # alone produces this result — the early stop it claimed to cover was never
        # exercised by it, which is how the bug below survived.
        from app.scraper import companies_house_psc as m
        seen = []
        monkeypatch.setattr(m, "_process",
                            lambda rec, *a: seen.append(rec["company_number"]) or "person")

        z = _psc_zip(tmp_path, ["00000001", "00000002", "00000003"])
        counts = import_ch_psc(z, "src", 97, only_companies={"00000002"})

        assert seen == ["00000002"]          # only the listed company processed
        assert counts["persons"] == 1
        assert counts["records"] == 1        # non-matches rejected before the JSON parse

    def test_a_company_whose_records_are_not_adjacent_is_fully_imported(self, tmp_path, monkeypatch):
        """The bug the early stop caused, in miniature.

        A snapshot does NOT group a company's PSC records together — measured on the
        real file, 16.9% of companies reappear after another has intervened. Reading
        stopped at the first sighting of each target, so those companies lost every
        later PSC. Silently: the run reported success with a plausible count.
        """
        from app.scraper import companies_house_psc as m
        seen = []
        monkeypatch.setattr(m, "_process",
                            lambda rec, *a: seen.append(rec["data"]["name"]) or "person")

        # Target, interloper, target again — the shape the real file has.
        z = _psc_zip(tmp_path, ["00000002", "00000009", "00000002"])
        counts = import_ch_psc(z, "src", 97, only_companies={"00000002"})

        assert len(seen) == 2, "the second PSC of a non-adjacent company was dropped"
        assert counts["persons"] == 2

    def test_reports_how_many_requested_companies_were_found(self, tmp_path, monkeypatch):
        # A company with no PSC records is normal — dissolved, exempt, or simply not
        # filed — but a large gap means the wrong list, and the run should say so
        # rather than look like a success.
        from app.scraper import companies_house_psc as m
        monkeypatch.setattr(m, "_process", lambda rec, *a: "person")

        z = _psc_zip(tmp_path, ["00000001", "00000002"])
        counts = import_ch_psc(z, "src", 97,
                               only_companies={"00000002", "00000404", "00000405"})

        assert counts["requested"] == 3 and counts["found"] == 1

    def test_the_digest_covers_the_whole_file_not_just_the_subset(self, tmp_path, monkeypatch):
        """The digest describes the SNAPSHOT, the subset describes what we imported.

        Conflating them is silent and total: the refresh digests the whole file and
        diffs it against this one, so a digest covering only the imported companies
        makes the very next run see all 15.8M records as newly added — which the
        churn guard then refuses, leaving the subset unrefreshable.

        Found by running the real baseline import and looking at the sidecar.
        """
        import gzip

        from app.scraper import companies_house_psc as m
        monkeypatch.setattr(m, "_process", lambda rec, *a: "person")

        z = _psc_zip(tmp_path, ["00000001", "00000002", "00000003"])
        out = tmp_path / "digest.tsv.gz"
        counts = import_ch_psc(z, "src", 97, only_companies={"00000002"},
                               digest_out=str(out))

        with gzip.open(out, "rt") as fh:
            assert len(fh.readlines()) == 3, "the digest must cover every record"
        assert counts["persons"] == 1, "…while still importing only the one asked for"

    def test_says_nothing_about_requested_counts_without_a_list(self, tmp_path, monkeypatch):
        from app.scraper import companies_house_psc as m
        monkeypatch.setattr(m, "_process", lambda rec, *a: "person")
        counts = import_ch_psc(_psc_zip(tmp_path, ["00000001"]), "src", 97)
        assert "requested" not in counts and "found" not in counts

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
        # the Register of Overseas Entities' open band — read as no stake before
        assert _band_floor("ownership-of-shares-more-than-25-percent-registered-overseas-entity") == 25
        assert _band_floor("ownership-of-shares-more-than-25-percent-as-trust-registered-overseas-entity") == 25

    def test_an_llp_members_surplus_assets_share_is_the_stake(self):
        stake, voting, otype, _ = _control([
            "right-to-share-surplus-assets-25-to-50-percent-limited-liability-partnership",
            "voting-rights-25-to-50-percent-limited-liability-partnership"])
        assert stake == 25 and voting == 25 and otype == "controlling"

    def test_an_overseas_entity_share_record_carries_its_floor(self):
        stake, voting, otype, _ = _control(
            ["ownership-of-shares-more-than-25-percent-registered-overseas-entity"])
        assert stake == 25 and voting is None and otype == "controlling"   # derived: 20–50 % band

    def test_voting_and_economic_kept_separate(self):
        stake, voting, otype, its = _control([
            "ownership-of-shares-75-to-100-percent", "voting-rights-75-to-100-percent"])
        assert stake == 75 and voting == 75 and otype == "controlling"
        assert its == ["ownership-of-shares-75-to-100-percent", "voting-rights-75-to-100-percent"]

    def test_shares_only_derives_type(self):
        stake, voting, otype, _ = _control(["ownership-of-shares-75-to-100-percent"])
        assert stake == 75 and voting is None and otype == "majority"   # derived, not a voting flag

    def test_a_null_in_the_natures_list_is_skipped(self):
        # 2 of the 15.8M records in the 2026-09 snapshot carry a null there; the
        # whole record used to be dropped with "NoneType is not iterable".
        stake, voting, otype, its = _control([None, "ownership-of-shares-75-to-100-percent"])
        assert stake == 75 and otype == "majority"
        assert its == ["ownership-of-shares-75-to-100-percent"]

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

    def test_an_individuals_name_is_built_from_its_parts_without_the_title(self):
        from app.scraper.companies_house_psc import _person_name
        # the real shape: `name` is a display string, the parts sit beside it
        rec = {"name": "Mr Michael Charles Saunders",
               "name_elements": {"forename": "Michael", "middle_name": "Charles",
                                 "surname": "Saunders", "title": "Mr"}}
        assert _person_name(rec) == ("Michael Charles Saunders", "Michael", "Saunders")
        # no elements: the title is stripped off the display string
        assert _person_name({"name": "Dr. Jane Doe"}) == ("Jane Doe", "Jane", "Doe")
        assert _person_name({"name": "Lady Ann Smith-Jones"}) == ("Ann Smith-Jones", "Ann", "Smith-Jones")
        # a name that is not a title stays whole
        assert _person_name({"name": "Mister Rogers"}) == ("Mister Rogers", "Mister", "Rogers")
        assert _person_name({"name": "Mr"}) is None
        assert _person_name({}) is None

    def test_a_mapped_individual_carries_the_parts(self):
        rec = {"company_number": "01234567", "data": {
            "kind": "individual-person-with-significant-control",
            "name": "Mrs Jean Carol Randle",
            "name_elements": {"forename": "Jean", "middle_name": "Carol", "surname": "Randle", "title": "Mrs"},
            "links": {"self": "/company/01234567/persons-with-significant-control/individual/abc"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        m = psc_record(rec, "src", 97)
        assert m.owner_props["full_name"] == "Jean Carol Randle"
        assert m.owner_props["first_name"] == "Jean"
        assert m.owner_props["last_name"] == "Randle"
        assert m.owner_props["search_text"] == "Jean Carol Randle"

    def test_birth_date(self):
        assert _birth_date({"date_of_birth": {"year": 1951, "month": 8}}) == "1951-08"
        assert _birth_date({"date_of_birth": {"year": 1951}}) == "1951"
        assert _birth_date({}) == ""

    def test_entity_psc_id_uses_uk_company_number(self):
        node_id, chid = _entity_psc_id({"identification": {
            "registration_number": "00686734", "country_registered": "England & Wales"},
            "links": {"self": "/company/x/.../abc"}})
        assert node_id == "gb-coh:00686734" and chid == "00686734"

    def test_a_delaware_corporate_psc_gets_a_state_register_id(self):
        # The bridge the country rule can never build: "United States" names 64
        # registers, but the filer names the STATE, and Delaware names exactly
        # one. This register_id is what lets the PSC node hard-merge with its
        # GLEIF twin instead of living as a name-only duplicate.
        rec = {"company_number": "09533203", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Tesla, Inc.",
            "identification": {"registration_number": "3903573",
                               "country_registered": "United States",
                               "place_registered": "Delaware"},
            "links": {"self": "/company/09533203/persons-with-significant-control/corporate-entity/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        mapped = psc_record(rec, "s1", 80)
        assert mapped.owner_props["register_id"] == "RA000602:3903573"

    def test_a_japanese_corporate_psc_gets_the_companies_register_id(self):
        # SoftBank Group's real record: Japan names four registers, the place
        # field says "Tokyo Stock Exchange (First Section)" — only the dashed
        # number identifies the register, and it is the key the LEI node has.
        rec = {"company_number": "03115186", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Softbank Group Corp",
            "identification": {"registration_number": "0104-01-056795",
                               "country_registered": "Japan",
                               "place_registered": "Tokyo Stock Exchange (First Section)",
                               "legal_authority": "Companies Act"},
            "links": {"self": "/company/03115186/persons-with-significant-control/corporate-entity/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        mapped = psc_record(rec, "s1", 80)
        assert mapped.owner_props["register_id"] == "RA000412:0104-01-056795"

    def test_a_swiss_corporate_psc_gets_the_commercial_register_id_as_gleif_spells_it(self):
        # Nestlé S.A.'s real record: "Che-105.909.036" under the canton of Vaud's
        # register office — the LEI node carries RA000549:CHE-105.909.036.
        rec = {"company_number": "00462438", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Nestle S.A.",
            "identification": {"registration_number": "Che-105.909.036",
                               "country_registered": "Switzerland",
                               "place_registered": "Office Du Registre Du Commerce Du Canton De Vaud"},
            "links": {"self": "/company/00462438/persons-with-significant-control/corporate-entity/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        mapped = psc_record(rec, "s1", 80)
        assert mapped.owner_props["register_id"] == "RA000549:CHE-105.909.036"
        assert mapped.owner_props["country"] == "CH", "ISO-2 like every other source"

    def test_filers_country_shorthand_resolves(self):
        from app.scraper.companies_house_psc import _iso2_country
        assert _iso2_country("Usa") == "US" and _iso2_country("U.S.A.") == "US"
        assert _iso2_country("England And Wales") == "GB" and _iso2_country("Switzerland") == "CH"
        assert _iso2_country("Korea") is None, "ambiguous: kept as the filer wrote it"

    def test_a_uk_corporate_psc_number_is_padded_to_companies_house_form(self):
        # Unilever PLC filed as "41424"; Companies House and GLEIF say 00041424.
        # The unpadded form minted a second node beside the GLEIF one.
        rec = {"company_number": "00017049", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Unilever Plc",
            "identification": {"registration_number": "41424",
                               "country_registered": "England",
                               "legal_authority": "Companies Act 2006",
                               "place_registered": "Companies House"},
            "links": {"self": "/company/00017049/persons-with-significant-control/corporate-entity/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        mapped = psc_record(rec, "s1", 80)
        assert mapped.owner_id == "gb-coh:00041424"
        assert mapped.owner_props["companies_house_id"] == "00041424"
        assert mapped.owner_props["registration_number"] == "00041424"
        assert mapped.owner_props["country"] == "GB", "England is GB, like the GLEIF node"
        node_id, chid = _entity_psc_id({"identification": {
            "registration_number": "SC123456", "country_registered": "Scotland"},
            "links": {"self": "/company/x/.../abc"}})
        assert node_id == "gb-coh:SC123456", "prefixed numbers are already eight characters"

    def test_a_dutch_corporate_psc_is_keyed_by_the_audited_general_register(self, monkeypatch):
        # The Netherlands lists four registers, but the audit says every Dutch
        # company sits on the KVK — the last resort after country, place and
        # number-format rules.
        from app.scraper import gleif_reference
        monkeypatch.setattr(gleif_reference, "_load", lambda name: {"countries": {"NL": {"code": "RA000463"}}})
        gleif_reference._general_registers.cache_clear()
        try:
            rec = {"company_number": "00000001", "data": {
                "kind": "corporate-entity-person-with-significant-control",
                "name": "Unilever N.V.",
                "identification": {"registration_number": "33129581",
                                   "country_registered": "Netherlands",
                                   "place_registered": "Kamer van Koophandel"},
                "links": {"self": "/company/00000001/persons-with-significant-control/corporate-entity/x"},
                "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
            mapped = psc_record(rec, "s1", 80)
            assert mapped.owner_props["register_id"] == "RA000463:33129581"
            assert mapped.owner_props["country"] == "NL"
        finally:
            gleif_reference._general_registers.cache_clear()

    def test_country_spellings_from_the_survey(self):
        from app.scraper.companies_house_psc import _iso2_country, _psc_country
        assert _iso2_country("Delaware") == "US" and _iso2_country("Delaware, Usa") == "US"
        assert _iso2_country("British Virgin Islands") == "VG" and _iso2_country("Bvi") == "VG"
        for uk in ("Gbr", "Gb-Eng", "Gb-Sct", "U.K.", "London", "United Kingdon"):
            assert _iso2_country(uk) == "GB", uk
        assert _iso2_country("The Netherlands") == "NL" and _iso2_country("Republic Of Ireland") == "IE"
        assert _iso2_country("Not Specified/Other") is None
        # "Not Specified/Other" but the register field says where.
        assert _psc_country({"country_registered": "Not Specified/Other",
                             "place_registered": "Companies House"}) == "GB"
        assert _psc_country({"country_registered": "Not Specified/Other",
                             "legal_authority": "England And Wales"}) == "GB"
        assert _psc_country({"country_registered": "Not Specified/Other"}) is None

    def test_uk_controllers_in_every_spelling_get_the_uk_key_scheme(self):
        for country in ("Gbr", "Gb-Eng", "U.K.", "United Kingdon", "London"):
            node_id, chid = _entity_psc_id({"identification": {
                "registration_number": "686734", "country_registered": country},
                "links": {"self": "/company/x/.../abc"}})
            assert (node_id, chid) == ("gb-coh:00686734", "00686734"), country
        node_id, _ = _entity_psc_id({"identification": {
            "registration_number": "00686734", "country_registered": "Not Specified/Other",
            "place_registered": "Companies House"}, "links": {"self": "/company/x/.../abc"}})
        assert node_id == "gb-coh:00686734"

    def test_a_us_state_named_as_the_country_keys_by_the_state(self):
        rec = {"company_number": "09533203", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Tesla, Inc.",
            "identification": {"registration_number": "3903573", "country_registered": "Delaware, Usa"},
            "links": {"self": "/company/09533203/persons-with-significant-control/corporate-entity/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        mapped = psc_record(rec, "s1", 80)
        assert mapped.owner_props["register_id"] == "RA000602:3903573"
        assert mapped.owner_props["country"] == "US"

    def test_a_named_register_keys_a_fund(self):
        # The named register is the one rule that keys a fund: "CSSF" is the
        # Luxembourg supervisor's register, not the trade register.
        rec = {"company_number": "00000002", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Some Fund SICAV",
            "identification": {"registration_number": "O00001234", "country_registered": "Luxembourg",
                               "place_registered": "CSSF"},
            "links": {"self": "/company/00000002/persons-with-significant-control/corporate-entity/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        assert psc_record(rec, "s1", 80).owner_props["register_id"] == "RA000433:O00001234"

    def test_which_rule_fired(self):
        from app.scraper.companies_house_psc import resolve_register_code
        assert resolve_register_code("NL", {"place_registered": "Kamer Van Koophandel"}, "1") == ("RA000463", "named")
        assert resolve_register_code("NL", {}, "1") == ("RA000463", "general")
        assert resolve_register_code("PA", {}, "1")[1] == "sole"
        assert resolve_register_code("US", {"place_registered": "Delaware Division Of Corporations"}, "1") == ("RA000602", "place")
        assert resolve_register_code("JP", {"place_registered": "Tokyo Stock Exchange"}, "0104-01-056795") == ("RA000412", "format")
        assert resolve_register_code("DE", {}, "HRB 1") == (None, None)
        assert resolve_register_code("GB", {"place_registered": "England And Wales"}, "09913788") == (None, None), \
            "UK controllers are keyed by companies_house_id, never by a guessed register"

    def test_a_country_registered_that_names_the_state_still_bridges(self):
        rec = {"company_number": "09533203", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Tesla, Inc.",
            "identification": {"registration_number": "3903573",
                               "country_registered": "Delaware"},
            "links": {"self": "/company/09533203/persons-with-significant-control/corporate-entity/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        mapped = psc_record(rec, "s1", 80)
        assert mapped.owner_props["register_id"] == "RA000602:3903573"

    def test_the_register_hides_in_legal_authority_teslas_actual_shape(self):
        # The real 2026 filing: place_registered "N/A", legal_authority
        # "Texas", the number without GLEIF's leading zero. All three quirks
        # must land on the same key GLEIF's node carries.
        rec = {"company_number": "09533203", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Tesla, Inc.",
            "identification": {"registration_number": "805587591",
                               "country_registered": "United States",
                               "legal_authority": "Texas",
                               "place_registered": "N/A"},
            "links": {"self": "/company/09533203/persons-with-significant-control/corporate-entity/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        mapped = psc_record(rec, "s1", 80)
        assert mapped.owner_props["register_id"] == "RA000637:805587591"

    def test_entity_psc_id_foreign_uses_the_slugged_self_link(self):
        node_id, chid = _entity_psc_id({"identification": {
            "registration_number": "999", "country_registered": "Delaware"},
            "links": {"self": "/company/x/corporate-entity/zzz"}})
        assert node_id == "chpsc:x:zzz" and chid is None


class TestPscSlugId:
    """Slug-safe on purpose: an id with slashes can never match a path
    parameter (ASGI decodes %2F before routing), so the old verbatim-link ids
    made their pages unloadable."""

    def test_the_normal_link_becomes_company_and_notification(self):
        assert psc_slug_id(
            "/company/09533203/persons-with-significant-control/"
            "corporate-entity/louLWFr-OOPqCpCa3K7gR4MK5u4"
        ) == "chpsc:09533203:louLWFr-OOPqCpCa3K7gR4MK5u4"

    def test_no_slash_survives_even_an_unrecognised_link(self):
        assert "/" not in psc_slug_id("some/strange/shape")
        assert "/" not in psc_slug_id("")


class TestEveryKindIsAccountedFor:
    """All eight kinds the snapshot contains, sorted deliberately.

    Two were dropped until 2026-08-19: `corporate-entity-beneficial-owner` (~13k
    register-wide) and `legal-person-beneficial-owner` (~540), while their
    individual twin was mapped. That mattered more than the counts suggest — the
    incremental refresh can never backfill an unmapped kind, because a record that
    produces nothing when imported also produces nothing when it changes.
    """

    ALL_KINDS = {
        "individual-person-with-significant-control": "person",
        "individual-beneficial-owner": "person",
        "corporate-entity-person-with-significant-control": "entity",
        "legal-person-person-with-significant-control": "entity",
        "corporate-entity-beneficial-owner": "entity",
        "legal-person-beneficial-owner": "entity",
        "super-secure-person-with-significant-control": None,
        "super-secure-beneficial-owner": None,
    }

    def _rec(self, kind):
        return {"company_number": "00000001", "data": {
            "kind": kind, "name": "A Body",
            "links": {"self": f"/company/00000001/psc/{kind}"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}

    def test_each_kind_lands_where_it_should(self):
        for kind, expected in self.ALL_KINDS.items():
            mapped = psc_record(self._rec(kind), "src", 97)
            got = mapped.kind_cat if mapped else None
            assert got == expected, f"{kind} mapped to {got}, expected {expected}"

    def test_the_corporate_beneficial_owners_are_imported(self):
        # The regression itself, stated on its own so a future trim of the tuple
        # fails with the reason rather than as one row of a loop.
        for kind in ("corporate-entity-beneficial-owner", "legal-person-beneficial-owner"):
            assert psc_record(self._rec(kind), "src", 97) is not None, f"{kind} dropped"

    def test_skipping_super_secure_is_a_decision(self):
        # Companies House withholds these for personal safety and the record carries
        # no name to write. Listed explicitly so the skip is not an accident of the
        # allow-list test.
        assert set(_SKIP_KINDS) == {"super-secure-person-with-significant-control",
                                    "super-secure-beneficial-owner"}
        assert not set(_SKIP_KINDS) & set(_PERSON_KINDS + _ENTITY_KINDS)

    def test_the_lists_cover_the_kinds_the_snapshot_contains(self):
        assert set(self.ALL_KINDS) == set(_PERSON_KINDS + _ENTITY_KINDS + _SKIP_KINDS)


class TestTheMappingIsPure:
    """`psc_record` is what the incremental refresh reuses, so it must be a function
    of its input alone — no clock, no writer, no database."""

    def _rec(self):
        return {"company_number": "07434180", "data": {
            "kind": "individual-person-with-significant-control", "name": "Ann Owner",
            "links": {"self": "/company/07434180/psc/individual/abc"},
            "notified_on": "2016-04-06",
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}

    def test_the_same_record_maps_the_same_way_twice(self):
        assert psc_record(self._rec(), "src", 97) == psc_record(self._rec(), "src", 97)

    def test_it_carries_no_timestamp(self):
        # `last_scraped_at` is a clock reading and belongs to the writer. If it
        # leaked in here, two mappings of one record would differ and the refresh's
        # digest would report every record as changed, every night.
        mapped = psc_record(self._rec(), "src", 97)
        assert "last_scraped_at" not in mapped.edge_props

    def test_the_edge_carries_the_key_the_refresh_matches_on(self):
        mapped = psc_record(self._rec(), "src", 97)
        assert mapped.edge_props["psc_self_link"] == "/company/07434180/psc/individual/abc"
        assert mapped.self_link == mapped.edge_props["psc_self_link"]

    def test_a_ceased_psc_carries_its_end_date(self):
        rec = self._rec()
        rec["data"]["ceased_on"] = "2020-01-31"
        assert psc_record(rec, "src", 97).edge_props["until"] == "2020-01-31"

    def test_an_active_psc_has_no_end_date(self):
        assert psc_record(self._rec(), "src", 97).edge_props["until"] is None


class TestCorporateRegistration:
    """A corporate PSC's register number is identity, not trivia — it used to be
    dropped entirely for every non-UK parent."""

    @staticmethod
    def _corp(country, number, place=None):
        ident = {"country_registered": country}
        if number is not None:
            ident["registration_number"] = number
        if place is not None:
            ident["place_registered"] = place
        return psc_record({"company_number": "07434180", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Holdco Ltd", "identification": ident,
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
            "links": {"self": "/company/07434180/persons-with-significant-control/corporate/x"},
        }}, "src", 97)

    def test_a_foreign_number_and_register_name_are_stored(self):
        mapped = self._corp("Germany", "HRB 12345", place="Amtsgericht München")
        assert mapped.owner_props["registration_number"] == "HRB 12345"
        assert mapped.owner_props["registration_authority"] == "Amtsgericht München"
        # DE has 177 per-court registers sharing HRB numbering — unsafe from the
        # country alone. But the filer NAMED the court, and a court names
        # exactly one register.
        assert mapped.owner_props["register_id"] == "RA000304:HRB12345"
        bare = self._corp("Germany", "HRB 12345")
        assert bare.owner_props["register_id"] is None, "a bare country stays unkeyed"

    def test_a_sole_register_country_yields_a_register_id(self):
        from app.scraper.gleif_reference import sole_register_for_country
        code = sole_register_for_country("PA")
        assert code, "Panama left the sole-register list — pick another fixture country"
        mapped = self._corp("Panama", "155 692 169")
        assert mapped.owner_props["register_id"] == f"{code}:155692169"

    def test_a_uk_corporate_keeps_its_key_scheme(self):
        mapped = self._corp("England & Wales", "00686734")
        assert mapped.owner_id == "gb-coh:00686734"          # node id unchanged
        assert mapped.owner_props["companies_house_id"] == "00686734"
        assert mapped.owner_props["registration_number"] == "00686734"
        assert mapped.owner_props["register_id"] is None     # CH id already merges

    def test_no_number_stores_nothing(self):
        mapped = self._corp("Germany", None)
        assert mapped.owner_props["registration_number"] is None
        assert mapped.owner_props["register_id"] is None


def test_every_psc_edge_is_tagged_with_its_record_kind():
    """filing_type "PSC" rides in the shared mapper's edge_props, so bulk and
    incremental both carry it — and the claim builders read it from there."""
    rec = {"company_number": "07434180", "data": {
        "kind": "individual-person-with-significant-control",
        "name": "Jean Carol Randle",
        "natures_of_control": ["ownership-of-shares-50-to-75-percent"],
        "links": {"self": "/company/07434180/persons-with-significant-control/individual/x"},
    }}
    mapped = psc_record(rec, "src", 97)
    assert mapped.edge_props["filing_type"] == "PSC"


class TestSourceDate:
    """`source_date` is when the register SAID it — the snapshot's date — not
    when the control began. With `notified_on` there, a 2016 appointment the
    2026-10-16 snapshot still lists read as ten-year-old evidence."""

    def _rec(self):
        return {"company_number": "07434180", "data": {
            "kind": "individual-person-with-significant-control", "name": "Ann Owner",
            "name_elements": {"forename": "Ann", "surname": "Owner"},
            "links": {"self": "/company/07434180/psc/individual/abc"},
            "notified_on": "2016-07-01",
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}

    def test_the_edge_is_dated_by_the_snapshot_and_starts_at_the_notification(self):
        e = psc_record(self._rec(), "src", 97, "2026-10-16").edge_props
        assert e["source_date"] == "2026-10-16"
        assert e["since"] == "2016-07-01"

    def test_without_a_snapshot_date_the_notification_stands_in(self):
        assert psc_record(self._rec(), "src", 97).edge_props["source_date"] == "2016-07-01"

    def test_the_bulk_load_reads_the_date_from_the_entry_name(self, tmp_path, monkeypatch):
        from app.scraper import companies_house_psc as m
        dates = []
        monkeypatch.setattr(m, "_process", lambda rec, batch, s, c, snap=None: dates.append(snap) or "person")
        z = tmp_path / "psc.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("persons-with-significant-control-snapshot-2026-10-16.txt",
                        json.dumps(self._rec()))
        import_ch_psc(str(z), "src", 97)
        assert dates == ["2026-10-16"]

    def test_a_bare_file_is_dated_by_its_name_too(self, tmp_path, monkeypatch):
        from app.scraper import companies_house_psc as m
        dates = []
        monkeypatch.setattr(m, "_process", lambda rec, batch, s, c, snap=None: dates.append(snap) or "person")
        f = tmp_path / "persons-with-significant-control-snapshot-2026-10-17.txt"
        f.write_text(json.dumps(self._rec()))
        import_ch_psc(str(f), "src", 97)
        assert dates == ["2026-10-17"]

    def test_an_undated_file_passes_no_date(self, tmp_path, monkeypatch):
        from app.scraper import companies_house_psc as m
        dates = []
        monkeypatch.setattr(m, "_process", lambda rec, batch, s, c, snap=None: dates.append(snap) or "person")
        import_ch_psc(_psc_zip(tmp_path, ["00000001"]), "src", 97)      # entry "psc.txt"
        assert dates == [None]


class TestCountryIsACodeOrNothing:
    """`country` is ISO-2 everywhere else; a filer's words that resolve to no
    code — 271 "Not Specified/Other" in 3M records, an ambiguous "Korea" —
    used to land there, where a country filter found nothing and a flag drew
    nonsense. They stay, as written, in `country_registered`."""

    def _corp(self, country):
        return {"company_number": "07434180", "data": {
            "kind": "corporate-entity-person-with-significant-control", "name": "Holdco",
            "identification": {"registration_number": "99", "country_registered": country},
            "links": {"self": "/company/07434180/psc/corporate-entity/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}

    def test_unresolvable_words_are_kept_beside_an_empty_country(self):
        props = psc_record(self._corp("Not Specified/Other"), "src", 97).owner_props
        assert props["country"] is None
        assert props["country_registered"] == "Not Specified/Other"

    def test_korea_is_words_not_a_guess(self):
        props = psc_record(self._corp("Korea"), "src", 97).owner_props
        assert (props["country"], props["country_registered"]) == (None, "Korea")

    def test_a_resolved_country_carries_no_words(self):
        props = psc_record(self._corp("Switzerland"), "src", 97).owner_props
        assert (props["country"], props["country_registered"]) == ("CH", None)

    def test_no_country_at_all_is_nothing_twice(self):
        rec = self._corp(None)
        del rec["data"]["identification"]["country_registered"]
        props = psc_record(rec, "src", 97).owner_props
        assert (props["country"], props["country_registered"]) == (None, None)


class TestOneWriterForTheCorporateOwner:
    """The refresh had its own copy of the corporate-owner upsert without the
    `extra` fields, so a corporate PSC it touched lost its registration number,
    register authority and `register_id`. Both paths now call one function."""

    def _mapped(self):
        return psc_record({"company_number": "00462438", "data": {
            "kind": "corporate-entity-person-with-significant-control", "name": "Nestle S.A.",
            "identification": {"registration_number": "CHE-105.909.036",
                               "country_registered": "Not Specified/Other",
                               "place_registered": "Office Du Registre Du Commerce Du Canton De Vaud",
                               "legal_authority": "Swiss Code Of Obligations"},
            "links": {"self": "/company/00462438/psc/corporate-entity/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}, "src", 97)

    def test_the_refresh_writes_what_the_bulk_load_writes(self, monkeypatch):
        from app.scraper import ch_psc_incremental as inc
        from app.scraper import companies_house_psc as m
        calls = []
        monkeypatch.setattr(m, "_entity", lambda *a, **k: calls.append(k))
        m.write_entity_owner(object(), self._mapped(), "src", 97)
        inc._write_entity_owner(object(), self._mapped(), "src", 97)
        assert len(calls) == 2 and calls[0] == calls[1]
        extra = calls[1]["extra"]
        assert extra["registration_number"] == "CHE-105.909.036"
        assert extra["registration_authority"] == "Office Du Registre Du Commerce Du Canton De Vaud"
        assert extra["country_registered"] == "Not Specified/Other"
        assert calls[1]["country"] is None
