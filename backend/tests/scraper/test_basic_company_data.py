"""Unit tests for Companies House BasicCompanyData parsing (DB not involved).
End-to-end enrichment is covered against a real ArcadeDB in
tests/integration/test_basic_company_data_it.py."""

from app.scraper.basic_company_data import (
    _company_type, _founded, _prev_names, _reg_address,
)


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
