"""Reading a filer's HEADQUARTERS out of EDGAR's business address.

The distinction this file exists to hold: `sec_country` refuses to read a
*domicile* from the business address, because a foreign filer often files through
a US office and Deutsche Bank would come out American. That refusal is about the
wrong question. The same address is good evidence of where a company is **run**,
and it was being fetched on every scrape and thrown away — 40 of 43 SEC companies
in the graph had no headquarters while EDGAR held their street address.

So: business address → `hq_*`, incorporation → `country`, and never the reverse.
"""

import pytest

from app.scraper.sec_edgar import sec_headquarters, sec_country


def subs(**business):
    return {"addresses": {"business": business}}


class TestReadingTheAddress:
    def test_a_us_filer(self):
        hq = sec_headquarters(subs(street1="790 N WATER STREET", city="MILWAUKEE",
                                   stateOrCountry="WI", zipCode="53202"))
        assert hq == {"address": "790 N WATER STREET, MILWAUKEE, WI, 53202, US",
                      "city": "MILWAUKEE", "country": "US",
                      # The parts as well as the string: EDGAR gives them
                      # separately and Nominatim takes them separately, so
                      # flattening and re-parsing is work nobody needs.
                      "street": "790 N WATER STREET", "postcode": "53202", "state": "WI"}

    def test_the_parts_come_back_for_a_structured_geocode(self):
        hq = sec_headquarters(subs(street1="1 Great Winchester St", city="LONDON",
                                   stateOrCountry="X0",
                                   stateOrCountryDescription="United Kingdom",
                                   zipCode="EC2N 2DB"))
        assert hq["street"] == "1 Great Winchester St"
        assert hq["postcode"] == "EC2N 2DB"
        assert hq["state"] is None          # a SEC foreign code is not a state

    def test_a_foreign_filer_by_sec_code(self):
        """SEC puts foreign countries in the same two-letter field as US states,
        with codes of its own (X0, E9, 2M). The description is what names them."""
        hq = sec_headquarters(subs(street1="1 Great Winchester St", city="LONDON",
                                   stateOrCountry="X0",
                                   stateOrCountryDescription="United Kingdom"))
        assert hq["country"] == "GB"
        assert hq["city"] == "LONDON"

    def test_an_explicit_country_wins(self):
        hq = sec_headquarters(subs(street1="Taunusanlage 12", city="FRANKFURT",
                                   country="Germany"))
        assert hq["country"] == "DE"

    def test_the_second_street_line_is_kept(self):
        hq = sec_headquarters(subs(street1="1 High St", street2="Floor 4",
                                   city="LONDON", stateOrCountry="X0"))
        assert "Floor 4" in hq["address"]

    def test_a_us_state_code_is_not_printed_as_a_country(self):
        # "WI" belongs in the address as a state; the country is US, once.
        hq = sec_headquarters(subs(street1="790 N Water St", city="MILWAUKEE",
                                   stateOrCountry="WI"))
        assert hq["address"].endswith("US")
        assert hq["address"].count("US") == 1


class TestWhenItSaysNothing:
    def test_no_addresses_at_all(self):
        assert sec_headquarters({}) is None
        assert sec_headquarters({"addresses": {}}) is None
        assert sec_headquarters(subs()) is None

    def test_a_country_with_no_place_is_not_an_address(self):
        # A bare country would put a pin at a centroid and call it a head office.
        assert sec_headquarters(subs(stateOrCountry="WI")) is None

    def test_a_city_alone_is_enough(self):
        hq = sec_headquarters(subs(city="MILWAUKEE", stateOrCountry="WI"))
        assert hq["city"] == "MILWAUKEE" and hq["country"] == "US"


class TestTheTwoQuestionsStaySeparate:
    def test_the_address_places_the_headquarters_but_not_the_domicile(self):
        """A filer with a US office and no stated incorporation: a headquarters,
        yes; a registration country, no. This is the Deutsche Bank guard, and the
        new reader must not undermine it."""
        doc = {"stateOfIncorporation": "", "stateOfIncorporationDescription": "",
               "addresses": {"business": {"street1": "60 Wall St", "city": "NEW YORK",
                                          "stateOrCountry": "NY"}}}
        assert sec_country(doc) is None            # unchanged, deliberately
        assert sec_headquarters(doc)["country"] == "US"

    def test_incorporation_elsewhere_does_not_move_the_headquarters(self):
        doc = {"stateOfIncorporation": "E9", "stateOfIncorporationDescription": "Cayman Islands",
               "addresses": {"business": {"street1": "1 Churchill Place", "city": "LONDON",
                                          "stateOrCountry": "X0",
                                          "stateOrCountryDescription": "United Kingdom"}}}
        assert sec_country(doc) == "KY"            # registered
        assert sec_headquarters(doc)["country"] == "GB"   # run


class TestTheBackfill:
    def _rows(self, *rows):
        return list(rows)

    def test_fills_only_what_is_blank(self, monkeypatch):
        from app.scraper import maintenance

        commands = []
        monkeypatch.setattr(maintenance, "run_query", lambda *a, **k: [
            {"id": "e1", "name": "Heartland Advisors", "cik": "0000937394"}])
        monkeypatch.setattr(maintenance, "run_command",
                            lambda sql, params=None: commands.append((sql, params)))

        res = maintenance.backfill_sec_headquarters(
            fetch=lambda cik: subs(street1="790 N WATER STREET", city="MILWAUKEE",
                                   stateOrCountry="WI", zipCode="53202"))

        assert res["filled"] == 1
        sql, params = commands[0]
        # COALESCE everywhere: a repair, not a re-import.
        assert sql.count("COALESCE") == 3
        assert params["c"] == "MILWAUKEE" and params["k"] == "US"

    def test_never_touches_the_registration_country(self, monkeypatch):
        from app.scraper import maintenance

        commands = []
        monkeypatch.setattr(maintenance, "run_query", lambda *a, **k: [
            {"id": "e1", "name": "Somewhere Inc", "cik": "1"}])
        monkeypatch.setattr(maintenance, "run_command",
                            lambda sql, params=None: commands.append((sql, params)))

        maintenance.backfill_sec_headquarters(
            fetch=lambda cik: subs(street1="60 Wall St", city="NEW YORK", stateOrCountry="NY"))

        assert "e.country" not in commands[0][0]

    def test_a_filer_edgar_cannot_place_is_skipped(self, monkeypatch):
        from app.scraper import maintenance

        monkeypatch.setattr(maintenance, "run_query", lambda *a, **k: [
            {"id": "e1", "name": "No Address Co", "cik": "1"}])
        monkeypatch.setattr(maintenance, "run_command",
                            lambda *a, **k: pytest.fail("wrote for a filer with no address"))

        res = maintenance.backfill_sec_headquarters(fetch=lambda cik: {})
        assert res["filled"] == 0 and res["still_unknown"] == 1

    def test_a_fetch_failure_does_not_stop_the_run(self, monkeypatch):
        from app.scraper import maintenance

        rows = [{"id": "e1", "name": "Broken", "cik": "1"},
                {"id": "e2", "name": "Fine", "cik": "2"}]
        monkeypatch.setattr(maintenance, "run_query", lambda *a, **k: rows)
        monkeypatch.setattr(maintenance, "run_command", lambda *a, **k: None)

        def fetch(cik):
            if cik == "1":
                raise RuntimeError("EDGAR timeout")
            return subs(street1="1 High St", city="LONDON", stateOrCountry="X0",
                        stateOrCountryDescription="United Kingdom")

        assert maintenance.backfill_sec_headquarters(fetch=fetch)["filled"] == 1
