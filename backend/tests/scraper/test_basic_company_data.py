"""Unit tests for Companies House BasicCompanyData parsing (DB not involved).
End-to-end enrichment is covered against a real ArcadeDB in
tests/integration/test_basic_company_data_it.py."""
import csv
import io
import zipfile
from unittest.mock import patch

from app.scraper.basic_company_data import (
    _company_type, _founded, _prev_names, _reg_address, import_basic_company_data,
)


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

    def test_registered_town_becomes_hq_for_the_map(self, tmp_path):
        from app.scraper import basic_company_data as m
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["CompanyName", "CompanyNumber", "RegAddress.PostTown",
                    "RegAddress.Country", "IncorporationDate"])
        w.writerow(["ACME LTD", "00000001", "HARROGATE", "ENGLAND", "11/09/2012"])
        zpath = tmp_path / "b.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("BasicCompanyData.csv", buf.getvalue())

        captured = {}
        with patch.object(m._UpdateBatch, "update",
                          lambda self, nid, props: captured.update(props)), \
             patch("app.scraper.basic_company_data._flush_script"):
            import_basic_company_data(str(zpath), 97)
        assert captured["hq_city"] == "HARROGATE"      # → geocoder pins it on the map
        assert captured["hq_country"] == "GB"
        assert captured["founded"] == 2012             # headline = year (was a full date)
        assert captured["founded_date"] == "2012-09-11"  # full date for the Details section

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

    def test_prev_names_dedupes_and_drops_current(self):
        row = {
            "PreviousName_1.CompanyName": "OLD NAME LTD",
            "PreviousName_2.CompanyName": "old name ltd",   # case dupe
            "PreviousName_3.CompanyName": "CURRENT LTD",     # == current, dropped
            "PreviousName_4.CompanyName": "OLDER NAME LTD",
        }
        assert _prev_names(row, "Current Ltd") == ["OLD NAME LTD", "OLDER NAME LTD"]
        assert _prev_names({}, "X") == []
