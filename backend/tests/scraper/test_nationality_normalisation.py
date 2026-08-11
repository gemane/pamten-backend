"""Nationality normalisation to ISO-2.

Two sources write Person.nationality in two shapes: Wikidata gives an ISO-2 code
(P297), Companies House PSC gives a demonym typed by the filer. The field held
`GB` and `British` side by side — the same nationality, grouping as two.

The rule that matters most here is what happens to a value we do NOT recognise.
PSC nationality is free text with a long tail, and a value we cannot map is still
what the register said. It is kept verbatim, never blanked and never guessed.
"""
import pytest

from app.scraper.companies_house_psc import _nationality
from app.scraper.maintenance import nationality_to_iso2


class TestRecognisedValues:
    @pytest.mark.parametrize("raw,code", [
        ("British", "GB"), ("english", "GB"), ("Scottish", "GB"), ("Welsh", "GB"),
        ("Northern Irish", "GB"),          # UK, not Ireland
        ("Irish", "IE"),                   # the one it is easiest to get wrong
        ("German", "DE"), ("Austrian", "AT"), ("American", "US"), ("Indian", "IN"),
        ("Singaporean", "SG"), ("Turkish", "TR"), ("Cuban", "CU"), ("French", "FR"),
    ])
    def test_demonyms_map_to_codes(self, raw, code):
        assert nationality_to_iso2(raw) == code

    @pytest.mark.parametrize("raw", ["British", "BRITISH", "  british  ", "British."])
    def test_case_whitespace_and_a_trailing_dot_do_not_matter(self, raw):
        assert nationality_to_iso2(raw) == "GB"

    def test_a_country_name_is_accepted_too(self):
        # PSC filers do type "Ireland" where the form wants "Irish".
        assert nationality_to_iso2("Ireland") == "IE"
        assert nationality_to_iso2("United States of America") == "US"

    def test_existing_codes_pass_through(self):
        """Idempotence: the Wikidata half of the data must not be disturbed."""
        assert nationality_to_iso2("DE") == "DE"
        assert nationality_to_iso2("us") == "US"      # canonicalised to upper


class TestUnrecognisedValues:
    """A gap is data we could not classify, not data to throw away."""

    @pytest.mark.parametrize("raw", ["Klingon", "Dual British/Irish", "Prefer not to say", "XX"])
    def test_unknown_values_return_none_from_the_mapper(self, raw):
        assert nationality_to_iso2(raw) is None

    @pytest.mark.parametrize("raw", ["Klingon", "Dual British/Irish", "Prefer not to say"])
    def test_and_the_importer_keeps_them_verbatim(self, raw):
        assert _nationality(raw) == raw

    def test_empty_stays_empty(self):
        assert nationality_to_iso2("") is None
        assert nationality_to_iso2(None) is None
        assert _nationality(None) == ""

    def test_a_two_letter_non_country_is_not_invented_into_a_code(self):
        # "XX" is not ISO-3166; guessing would be worse than leaving it.
        assert _nationality("XX") == "XX"


class TestTheImporter:
    def test_psc_demonyms_arrive_already_normalised(self):
        """New imports should not need the maintenance pass at all."""
        assert _nationality("British") == "GB"
        assert _nationality("  Irish ") == "IE"

    def test_it_never_returns_none(self):
        # The person record writes this field directly; None would become "None".
        assert _nationality(None) == ""
        assert isinstance(_nationality("British"), str)
