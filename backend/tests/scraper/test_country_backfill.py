"""Deciding a SEC filer's country from EDGAR.

The trap this exists to avoid: EDGAR's business address is where the *filing*
comes from, not where the company is. DEUTSCHE BANK AKTIENGESELLSCHAFT lists a New
York address. Trusting it would move German banks to the United States — wrong
data, which is worse than the blank it would replace.

So incorporation is read first, the address only as a fallback, and an unresolvable
filer stays blank rather than being guessed at.
"""
import pytest

from app.scraper.maintenance import sec_country


def subs(*, inc_code=None, inc_name=None, business_country=None, state=None):
    d: dict = {}
    if inc_code:
        d["stateOfIncorporation"] = inc_code
    if inc_name:
        d["stateOfIncorporationDescription"] = inc_name
    d["addresses"] = {"business": {"country": business_country, "stateOrCountry": state}}
    return d


class TestIncorporationWins:
    def test_a_us_state_of_incorporation_means_the_us(self):
        # Delaware, Nevada and the rest are US states, not countries.
        assert sec_country(subs(inc_code="DE", inc_name="Delaware")) == "US"
        assert sec_country(subs(inc_code="NV", inc_name="Nevada")) == "US"

    @pytest.mark.parametrize("name,code", [
        ("United Kingdom", "GB"), ("Germany", "DE"), ("Cayman Islands", "KY"),
        ("Singapore", "SG"), ("Canada", "CA"),
    ])
    def test_a_named_country_of_incorporation_is_used(self, name, code):
        assert sec_country(subs(inc_code="X0", inc_name=name)) == code

    def test_incorporation_beats_a_us_filing_address(self):
        """A Cayman fund run from California is Cayman, not the US."""
        assert sec_country(
            subs(inc_code="E9", inc_name="Cayman Islands",
                 business_country=None, state="CA")) == "KY"


class TestAddressOnlyAsFallback:
    def test_a_named_business_country_is_used_when_incorporation_is_silent(self):
        assert sec_country(subs(business_country="United Kingdom")) == "GB"

    def test_a_bare_us_state_address_is_NOT_taken_as_the_country(self):
        """The Deutsche Bank case: a New York filing office and no stated
        incorporation. EDGAR cannot tell us where the company is, so neither can
        we — and a wrong country is worse than none."""
        assert sec_country(subs(state="NY")) is None

    def test_nothing_at_all_is_none(self):
        assert sec_country({}) is None
        assert sec_country(subs()) is None

    def test_an_unrecognised_country_name_is_not_invented(self):
        assert sec_country(subs(inc_name="Freedonia")) is None
