"""Cleaning an address before it is handed to Nominatim.

A registered office is usually an agent's address — "C/O The Corporation Trust
Company, Corporation Trust Center, 1209 Orange St, Wilmington" — and Nominatim
reads that leading company name as a place it has never heard of and returns
nothing. 265 of 480 registered addresses geocoded to nothing because of it.

The stored value is never touched. Owlgraph keeps addresses as the source gave
them; this is the tool-facing workaround, applied to the query only.
"""
import pytest

from app.scraper.geocode import clean_for_geocoding as clean


class TestWhatItRemoves:
    def test_a_care_of_agent_prefix(self):
        assert clean("C/O The Corporation Trust Company, Corporation Trust Center, "
                     "1209 Orange St, Wilmington, 19801, US") == \
            "Corporation Trust Center, 1209 Orange St, Wilmington, 19801, US"

    @pytest.mark.parametrize("prefix", ["C/O", "c/o", "C.O.", "CO ", "c o "])
    def test_the_spellings_the_data_actually_uses(self, prefix):
        assert clean(f"{prefix} Some Agent Ltd, 1 High St, London, GB") == "1 High St, London, GB"

    def test_a_po_box(self):
        # Ugland House is a real building; the box number is not a place.
        assert clean("C/O MAPLES CORPORATE SERVICES LIMITED, P.O. BOX 309, UGLAND HOUSE, "
                     "SOUTH CHURCH STREET, GRAND CAYMAN, KY1-1104, KY") == \
            "UGLAND HOUSE, SOUTH CHURCH STREET, GRAND CAYMAN, KY1-1104, KY"

    @pytest.mark.parametrize("unit", ["SUITE 201", "Ste 700", "Floor 3", "3rd Floor",
                                      "Unit 12", "Of. 1733", "Room 4"])
    def test_a_unit_designator_between_street_and_city(self, unit):
        assert clean(f"1521 CONCORD PIKE, {unit}, WILMINGTON, 19803, US") == \
            "1521 CONCORD PIKE, WILMINGTON, 19803, US"


    def test_the_tail_of_an_agent_name_that_contained_a_comma(self):
        """"C/O UNITED CORPORATE SERVICES, INC., 800 North State Street" — the
        care-of strip stops at the first comma, leaving a bare "INC." that
        Nominatim cannot place. Measured: the street alone resolves exactly, the
        street with "INC.," in front of it returns nothing at all."""
        assert clean("C/O UNITED CORPORATE SERVICES, INC., 800 NORTH STATE STREET, "
                     "DOVER, 19901, US") == "800 NORTH STATE STREET, DOVER, 19901, US"

    @pytest.mark.parametrize("suffix", ["Inc.", "LLC", "Ltd", "GmbH", "S.A.", "N.V.", "PLC"])
    def test_the_common_suffixes(self, suffix):
        assert clean(f"C/O Some Agent, {suffix}, 1 High St, London, GB") == "1 High St, London, GB"


class TestWhatItLeavesAlone:
    def test_an_ordinary_address_is_unchanged(self):
        addr = "1 CHURCHILL PLACE, LONDON, E14 5HP, GB"
        assert clean(addr) == addr

    def test_a_street_that_merely_starts_with_a_unit_word_survives(self):
        # "Office Park" is a place; "Office 3" is not. The distinction is that the
        # whole segment has to be the designator.
        addr = "Coventry Office Park, Birmingham, B1 1AA, GB"
        assert clean(addr) == addr

    def test_a_care_of_name_mid_address_is_kept(self):
        # Only a LEADING care-of is the agent; elsewhere it may be the only clue
        # to the building.
        addr = "1 High St, c/o Reception, London, GB"
        assert clean(addr) == addr

    def test_an_address_that_is_nothing_but_care_of_is_tried_as_is(self):
        # Returning "" would mean not geocoding at all, which is strictly worse
        # than letting Nominatim have a go.
        assert clean("C/O Someone") == "C/O Someone"

    def test_a_suffix_further_along_is_kept(self):
        # Only a LEADING fragment is an agent's tail. "Barclays Bank PLC" as the
        # building's name is the best clue the address has.
        addr = "1 Churchill Place, Barclays Bank PLC, London, GB"
        assert clean(addr) == addr

    def test_a_street_that_merely_begins_with_a_suffix_word_survives(self):
        # "Corporation Trust Center" is a building, not a dangling "Corp".
        addr = "Corporation Trust Center, 1209 Orange St, Wilmington, 19801, US"
        assert clean(addr) == addr

    def test_empty_stays_empty(self):
        assert clean("") == ""
        assert clean(None) == ""     # type: ignore[arg-type]


class TestItReachesTheGeocoder:
    def test_geocode_full_queries_the_cleaned_form(self, monkeypatch):
        """The point of the exercise: the cleaning has to happen on the way out,
        not merely exist as a function nobody calls."""
        from app.config import settings
        from app.scraper import geocode

        monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)
        monkeypatch.setattr(geocode, "_throttle", lambda: None)
        geocode._full_cache.clear()
        seen = {}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return [{"lat": "1.0", "lon": "2.0", "place_rank": 30}]

        class FakeClient:
            def get(self, url, params=None):
                seen.update(params or {})
                return FakeResp()

        monkeypatch.setattr(geocode, "_get_client", lambda: FakeClient())
        geocode.geocode_full("C/O Agent Ltd, 1 High St, London, GB")

        assert seen["q"] == "1 High St, London, GB"
