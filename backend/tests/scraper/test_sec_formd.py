"""Form D parsing: the fixture is SpaceX's REAL 2022 Form D (Musk, Shotwell,
Gracias…), captured verbatim; the hostile patterns come from the probe band
(fund GPs filed as related persons with literal "N/A" first names)."""
from pathlib import Path

import pytest
from unittest.mock import patch

from app.scraper.sec_formd import latest_form_d, parse_form_d

FIXTURE = (Path(__file__).parent / "fixtures" / "formd_spacex.xml").read_text()


class TestParseFormD:
    def test_parses_spacexs_real_form_d(self):
        d = parse_form_d(FIXTURE)
        by_name = {p["name"]: p["roles"] for p in d["persons"]}
        assert by_name["Elon Musk"] == ["Executive Officer", "Director"]
        assert by_name["Gwynne Shotwell"] == ["Executive Officer", "Director"]
        assert "Antonio Gracias" in by_name
        assert len(d["persons"]) == 7
        assert d["jurisdiction"] == "DELAWARE"
        assert d["entity_type"] == "Corporation"
        assert d["amount_sold"] == 249999890.0

    def test_na_first_names_are_filler_not_names(self):
        # Fund filings list GP LLCs with firstName "N/A": the N/A must not
        # become part of a name ("N/a 137 Holdings Alpha, Llc" — probe band).
        xml = FIXTURE.replace(
            "<firstName>ELON</firstName>", "<firstName>N/A</firstName>", 1)
        d = parse_form_d(xml)
        names = [p["name"] for p in d["persons"]]
        assert "Musk" in names and not any(n.startswith("N/a") for n in names)

    def test_unparseable_xml_is_none(self):
        assert parse_form_d("not xml at all") is None


class TestLatestFormD:
    def _subs(self, forms):
        return {"filings": {"recent": {
            "form": [f for f, _, _ in forms],
            "accessionNumber": [a for _, a, _ in forms],
            "filingDate": [d for _, _, d in forms]}}}

    def test_newest_d_wins_and_carries_provenance(self):
        subs = self._subs([("8-K", "0000000000-26-000009", "2026-08-13"),
                           ("D/A", "0001181412-22-000003", "2022-08-05"),
                           ("D", "0001181412-20-000001", "2020-02-01")])
        with patch("app.scraper.sec_formd._get", return_value=subs), \
             patch("app.scraper.sec_formd._get_text", return_value=FIXTURE):
            d = latest_form_d("1181412")
        assert d["accession"] == "0001181412-22-000003"
        assert d["form"] == "D/A"
        assert d["filing_date"] == "2022-08-05"
        assert d["url"].endswith("primary_doc.xml")

    def test_no_form_d_is_none(self):
        subs = self._subs([("10-K", "0000000000-26-000001", "2026-01-01")])
        with patch("app.scraper.sec_formd._get", return_value=subs):
            assert latest_form_d("999999") is None

    def test_missing_xml_is_pre_2009_but_a_5xx_surfaces(self):
        import httpx
        subs = self._subs([("D", "0000000000-08-000001", "2008-01-01")])
        def err(code):
            return httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://x.test"),
                                         response=httpx.Response(code, request=httpx.Request("GET", "http://x.test")))
        with patch("app.scraper.sec_formd._get", return_value=subs), \
             patch("app.scraper.sec_formd._get_text", side_effect=err(404)):
            assert latest_form_d("1") is None
        with patch("app.scraper.sec_formd._get", return_value=subs), \
             patch("app.scraper.sec_formd._get_text", side_effect=err(503)), \
             pytest.raises(httpx.HTTPStatusError):
            latest_form_d("1")
