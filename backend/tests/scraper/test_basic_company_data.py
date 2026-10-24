"""Unit tests for Companies House BasicCompanyData parsing (DB not involved).
End-to-end enrichment is covered against a real ArcadeDB in
tests/integration/test_basic_company_data_it.py."""
import csv
import io
import zipfile
from unittest.mock import patch

import pytest

from app.scraper.basic_company_data import (
    _company_type, _founded, _prev_names, _reg_address, import_basic_company_data,
)


@pytest.fixture(autouse=True)
def _no_first_year_pass():
    """The import ends with the PSC first-year lower-bound pass, which reads the
    graph; these tests have no graph (the one that pins the call patches it
    again with a recorder)."""
    with patch("app.scraper.basic_company_data.mark_first_year_lower_bounds",
               lambda company_ids=None, **k: {"edges": 0}), \
         patch("app.scraper.basic_company_data.sync_uk_sic", lambda **k: 0):
        yield


def _one_row_zip(tmp_path):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["CompanyName", "CompanyNumber", "CompanyCategory", "IncorporationDate"])
    w.writerow(["ACME LTD", "00000001", "Private Limited Company", "01/01/2000"])
    zpath = tmp_path / "basic.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("BasicCompanyData.csv", buf.getvalue())
    return str(zpath)


class TestBulkLoad:
    """The importer owns the bulk-load index toggling (drop before, rebuild after)."""

    def test_bulk_load_drops_then_rebuilds_around_the_load(self, tmp_path):
        order = []
        with patch("app.scraper.basic_company_data._drop_secondary_indexes",
                   side_effect=lambda: order.append("drop")), \
             patch("app.scraper.basic_company_data._rebuild_indexes",
                   side_effect=lambda: order.append("rebuild")), \
             patch("app.scraper.basic_company_data._flush_script",
                   side_effect=lambda *a, **k: order.append("load")):
            import_basic_company_data(_one_row_zip(tmp_path), 97, bulk_load=True)
        assert order == ["drop", "load", "rebuild"]   # drop before load, rebuild after

    def test_no_index_changes_without_bulk_load(self, tmp_path):
        with patch("app.scraper.basic_company_data._drop_secondary_indexes") as drop, \
             patch("app.scraper.basic_company_data._rebuild_indexes") as rebuild, \
             patch("app.scraper.basic_company_data._flush_script"):
            import_basic_company_data(_one_row_zip(tmp_path), 97)
        drop.assert_not_called()
        rebuild.assert_not_called()

    def test_batch_size_is_threaded_to_the_writer(self, tmp_path):
        from app.scraper import basic_company_data as m
        captured = {}
        orig = m._UpdateBatch.__init__

        def spy(self, batch_size=400):
            captured["bs"] = batch_size
            orig(self, batch_size)

        with patch.object(m._UpdateBatch, "__init__", spy), \
             patch("app.scraper.basic_company_data._flush_script"):
            import_basic_company_data(_one_row_zip(tmp_path), 97, batch_size=50)
        assert captured["bs"] == 50


def _multi_row_zip(tmp_path, numbers):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["CompanyName", "CompanyNumber", "CompanyCategory", "IncorporationDate"])
    for n in numbers:
        w.writerow([f"CO {n} LTD", n, "Private Limited Company", "01/01/2000"])
    zpath = tmp_path / "basic.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("BasicCompanyData.csv", buf.getvalue())
    return str(zpath)


class TestOnlyCompanies:
    """--only / --only-file curated subset: enrich just the listed company numbers."""

    def test_enriches_only_listed_and_stops_early(self, tmp_path):
        from app.scraper import basic_company_data as m
        written = []
        with patch.object(m._UpdateBatch, "update",
                          lambda self, nid, props: written.append(nid)), \
             patch("app.scraper.basic_company_data._flush_script"):
            counts = import_basic_company_data(
                _multi_row_zip(tmp_path, ["00000001", "00000002", "00000003"]),
                97, only_companies={"00000002"})
        assert written == ["gb-coh:00000002"]
        assert counts["companies"] == 1

    def test_the_registered_office_is_written_in_parts_and_never_as_headquarters(self, tmp_path):
        from app.scraper import basic_company_data as m
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["CompanyName", "CompanyNumber", "RegAddress.CareOf", "RegAddress.AddressLine1",
                    "RegAddress.AddressLine2", "RegAddress.PostTown", "RegAddress.PostCode",
                    "RegAddress.Country", "IncorporationDate"])
        w.writerow(["ACME LTD", "00000001", "", "1 HIGH STREET", "SUITE 2", "HARROGATE", "HG1 1AA",
                    "ENGLAND", "11/09/2012"])
        zpath = tmp_path / "b.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("BasicCompanyData.csv", buf.getvalue())

        captured = {}
        with patch.object(m._UpdateBatch, "update",
                          lambda self, nid, props: captured.update(props)), \
             patch("app.scraper.basic_company_data._flush_script"):
            import_basic_company_data(str(zpath), 97)
        assert captured["founded"] == 2012             # headline = year (was a full date)
        assert captured["founded_date"] == "2012-09-11"  # full date for the Details section
        # the registered office in parts, under the registered pin only
        assert (captured["reg_street"], captured["reg_city"], captured["reg_postcode"]) == (
            "1 HIGH STREET, SUITE 2", "HARROGATE", "HG1 1AA")
        # as a string twice: the dedup corroborator and the panel's display address
        assert captured["address"] == captured["registered_address"] == \
            "1 HIGH STREET, SUITE 2, HARROGATE, ENGLAND, HG1 1AA"
        # and NOTHING under hq_*: the register says where a company is registered,
        # not where it is run (copied there until 2026-10-18 — finding #7)
        assert not any(k.startswith("hq_") for k in captured)

    def test_a_row_without_address_columns_writes_no_parts(self):
        """Only the parts the row has: a SET of null would blank GLEIF's."""
        from app.scraper.basic_company_data import _reg_address_parts
        assert _reg_address_parts({"RegAddress.PostTown": "", "RegAddress.PostCode": " "}) == {}
        assert _reg_address_parts({"RegAddress.CareOf": "C/O AGENTS LTD", "RegAddress.POBox": "PO BOX 1",
                                   "RegAddress.PostCode": "N1 1AA"}) == {
            "address": "C/O AGENTS LTD, PO BOX 1, N1 1AA",
            "reg_street": "C/O AGENTS LTD, PO BOX 1", "reg_postcode": "N1 1AA"}

    def test_the_import_then_marks_first_year_lower_bounds_for_the_companies_it_touched(self, tmp_path):
        """The incorporation dates arrive with this import, so the PSC lower-bound
        rule (companies_house_psc.mark_first_year_lower_bounds) runs at its end —
        scoped to the subset on an --only run, over every UK company otherwise."""
        from app.scraper import basic_company_data as m
        calls = []
        with patch.object(m._UpdateBatch, "update", lambda self, nid, props: None), \
             patch("app.scraper.basic_company_data._flush_script"), \
             patch("app.scraper.basic_company_data.mark_first_year_lower_bounds",
                   lambda company_ids=None, **k: calls.append(company_ids) or {"edges": 0}):
            zpath = _multi_row_zip(tmp_path, ["00000001", "00000002", "00000003"])
            counts = import_basic_company_data(zpath, 97, only_companies={"00000002", "00000003"})
            import_basic_company_data(zpath, 97)
        assert calls == [["gb-coh:00000002", "gb-coh:00000003"], None]
        assert counts["first_year_bounds"] == {"edges": 0}

    def test_prefilter_keeps_header_and_matching_lines_only(self):
        from app.scraper.basic_company_data import _prefiltered_lines
        lines = ['"CompanyName","CompanyNumber"\n', '"A LTD","00000001"\n',
                 '"B LTD","00000002"\n', '"C LTD","00000003"\n']
        out = list(_prefiltered_lines(iter(lines), {"00000002"}))
        assert out == [lines[0], lines[2]]           # header + only the matching row


class TestFieldParsing:
    def test_company_type_default_and_nonprofit(self):
        assert _company_type("Private Limited Company") == "company"
        assert _company_type("Public Limited Company") == "company"
        assert _company_type("Charitable Incorporated Organisation") == "nonprofit"
        assert _company_type("Community Interest Company") == "nonprofit"
        assert _company_type(None) == "company"

    def test_founded_parses_uk_date(self):
        assert _founded("11/09/2012") == "2012-09-11"
        assert _founded("1/2/2020") == "2020-02-01"
        assert _founded("") is None
        assert _founded(None) is None
        assert _founded("2012") is None

    def test_reg_address_joins_present_parts(self):
        row = {
            "RegAddress.AddressLine1": "9 PRINCES SQUARE",
            "RegAddress.AddressLine2": "",
            "RegAddress.PostTown": "HARROGATE",
            "RegAddress.Country": "ENGLAND",
            "RegAddress.PostCode": "HG1 1ND",
        }
        assert _reg_address(row) == "9 PRINCES SQUARE, HARROGATE, ENGLAND, HG1 1ND"
        assert _reg_address({}) is None

    def test_former_names_carry_the_change_of_name_date(self):
        from app.scraper.basic_company_data import _former_names
        # Companies House 08401611 as the register lists it (newest first)
        row = {"PreviousName_1.CONDATE": "20/08/2021", "PreviousName_1.CompanyName": "BRIGHTSTAR 20:20 UK LIMITED",
               "PreviousName_2.CONDATE": "27/11/2013", "PreviousName_2.CompanyName": "20:20 MOBILE 1 LIMITED",
               "PreviousName_3.CONDATE": "", "PreviousName_3.CompanyName": "UNDATED LTD",
               "PreviousName_4.CONDATE": "01/01/2000", "PreviousName_4.CompanyName": "likewize services uk limited",
               "PreviousName_5.CONDATE": "20/08/2021", "PreviousName_5.CompanyName": "BRIGHTSTAR 20:20 UK LIMITED"}
        assert _former_names(row, "LIKEWIZE SERVICES UK LIMITED") == [
            "BRIGHTSTAR 20:20 UK LIMITED|2021-08-20", "20:20 MOBILE 1 LIMITED|2013-11-27", "UNDATED LTD|"]
        assert _former_names({}, "X") == []

    def test_import_writes_the_dated_former_names_beside_the_aliases(self, tmp_path):
        from app.scraper import basic_company_data as m
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["CompanyName", "CompanyNumber", "IncorporationDate",
                    "PreviousName_1.CONDATE", "PreviousName_1.CompanyName"])
        w.writerow(["LIKEWIZE SERVICES UK LIMITED", "08401611", "14/02/2013",
                    "20/08/2021", "BRIGHTSTAR 20:20 UK LIMITED"])
        zpath = tmp_path / "b.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("BasicCompanyData.csv", buf.getvalue())
        captured = {}
        with patch.object(m._UpdateBatch, "update",
                          lambda self, nid, props: captured.update(props)), \
             patch("app.scraper.basic_company_data._flush_script"):
            import_basic_company_data(str(zpath), 97)
        assert captured["aliases"] == ["BRIGHTSTAR 20:20 UK LIMITED"]
        assert captured["former_names"] == ["BRIGHTSTAR 20:20 UK LIMITED|2021-08-20"]
        assert "BRIGHTSTAR 20:20 UK LIMITED" in captured["search_text"]

    def test_prev_names_dedupes_and_drops_current(self):
        row = {
            "PreviousName_1.CompanyName": "OLD NAME LTD",
            "PreviousName_2.CompanyName": "old name ltd",   # case dupe
            "PreviousName_3.CompanyName": "CURRENT LTD",     # == current, dropped
            "PreviousName_4.CompanyName": "OLDER NAME LTD",
        }
        assert _prev_names(row, "Current Ltd") == ["OLD NAME LTD", "OLDER NAME LTD"]
        assert _prev_names({}, "X") == []


class TestSicCodes:
    """The register's SIC codes ride on the row into `uk_sic_codes`, as
    written, and the import ends by turning them into industry statements
    dated by the snapshot (app.industry)."""

    def _zip(self, tmp_path, *sic):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["CompanyName", "CompanyNumber", "CompanyCategory", "IncorporationDate",
                    "SICCode.SicText_1", "SICCode.SicText_2", "SICCode.SicText_3", "SICCode.SicText_4"])
        w.writerow(["WIENERBERGER LTD", "00000002", "Private Limited Company", "01/01/2000", *sic])
        zpath = tmp_path / "basic.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("BasicCompanyDataAsOneFile-2026-07-01.csv", buf.getvalue())
        return str(zpath)

    def test_the_codes_are_kept_as_written_and_in_order(self, tmp_path):
        from app.scraper.basic_company_data import _sic_codes
        assert _sic_codes({"SICCode.SicText_1": "23320 - Manufacture of bricks, tiles and construction products, in baked clay",
                           "SICCode.SicText_2": "46730 - Wholesale of wood, construction materials and sanitary equipment",
                           "SICCode.SicText_3": "", "SICCode.SicText_4": "None Supplied"}) == [
            "23320 - Manufacture of bricks, tiles and construction products, in baked clay",
            "46730 - Wholesale of wood, construction materials and sanitary equipment"]
        assert _sic_codes({}) == []

    def test_the_update_carries_them(self, tmp_path):
        captured = {}
        with patch("app.scraper.basic_company_data._flush_script",
                   side_effect=lambda script, params: captured.update(params) or []):
            import_basic_company_data(self._zip(tmp_path, "23320 - Manufacture of bricks", "", "", ""), 97)
        assert captured["uk_sic_codes__0"] == ["23320 - Manufacture of bricks"]

    def test_the_statements_follow_the_flush_scoped_and_dated(self, tmp_path):
        calls = []
        with patch("app.scraper.basic_company_data._flush_script", lambda *a, **k: []), \
             patch("app.scraper.basic_company_data.sync_uk_sic",
                   side_effect=lambda **k: calls.append(k) or 1):
            counts = import_basic_company_data(self._zip(tmp_path, "23320 - Manufacture of bricks", "", "", ""), 97,
                                               only_companies={"00000002"})
        assert calls == [{"entity_ids": ["gb-coh:00000002"], "snapshot_date": "2026-07-01"}]
        assert counts["industry"] == 1

    def test_a_full_run_syncs_every_uk_company(self, tmp_path):
        calls = []
        with patch("app.scraper.basic_company_data._flush_script", lambda *a, **k: []), \
             patch("app.scraper.basic_company_data.sync_uk_sic",
                   side_effect=lambda **k: calls.append(k) or 0):
            import_basic_company_data(self._zip(tmp_path, "23320 - Manufacture of bricks", "", "", ""), 97)
        assert calls[0]["entity_ids"] is None and calls[0]["snapshot_date"] == "2026-07-01"
