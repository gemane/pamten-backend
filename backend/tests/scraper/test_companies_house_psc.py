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
        # DE has 177 per-court registers sharing HRB numbering — the unsafe case.
        assert mapped.owner_props["register_id"] is None

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
