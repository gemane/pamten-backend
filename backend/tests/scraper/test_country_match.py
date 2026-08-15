"""
The country rule every instant source applies.

Small enough to look obviously right and wrong in both directions, which is why
it is tested on its own: the same three lines decide, in Wikidata, in SEC EDGAR
and in OpenCorporates, whether a match is the company the user asked for.
"""
from app.scraper.country_match import country_mismatch, matches_requested


class TestNothingAsked:
    """No country chosen — the filter is off and must not reject anything."""

    def test_any_country_passes(self):
        assert matches_requested("US", None) is True
        assert matches_requested("DE", "") is True
        assert matches_requested(None, None) is True


class TestUnknownIsNotAMismatch:
    """A source that states no country is not claiming a different one.

    Many records legitimately carry none — a Wikidata item without P17 is
    ordinary — and rejecting them would empty out the sparse sources without
    preventing a single wrong import.
    """

    def test_no_country_found_is_accepted(self):
        assert matches_requested(None, "DE") is True
        assert matches_requested("", "DE") is True
        assert matches_requested("   ", "DE") is True


class TestComparison:
    def test_same_country_matches(self):
        assert matches_requested("DE", "DE") is True

    def test_case_and_spacing_do_not_decide(self):
        # It arrives from a URL, a JSON body, or a source's own payload.
        assert matches_requested("de", "DE") is True
        assert matches_requested("DE", "de") is True
        assert matches_requested(" de ", "DE") is True

    def test_a_different_country_is_rejected(self):
        # The whole point: "Alphabet" in Germany must not be answered with the
        # company in Mountain View.
        assert matches_requested("US", "DE") is False

    def test_a_country_that_merely_starts_the_same_is_rejected(self):
        assert matches_requested("DEU", "DE") is False


class TestTheRejection:
    def test_carries_both_countries_so_the_reason_survives(self):
        out = country_mismatch("Alphabet", "US", "DE")
        assert out["found_country"] == "US" and out["requested_country"] == "DE"

    def test_looks_like_every_other_empty_result(self):
        # `total` is what the run log reads; a rejection must travel the same
        # path as an empty search rather than need handling of its own.
        out = country_mismatch("Alphabet", "US", "DE")
        assert out["total"] == 0 and out["scraped"] == []
        assert out["status"] == "country_mismatch" and out["query"] == "Alphabet"
