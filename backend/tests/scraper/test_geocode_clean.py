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



@pytest.fixture(autouse=True)
def _no_geocache(monkeypatch):
    """Take the durable cache out of these tests.

    They exercise the HTTP path with no database behind them, and `lookup` now
    lets a ConnectionError through on purpose — if the database is unreachable
    the answer has nowhere to go, so spending a Nominatim request would be
    waste. Stubbing it here keeps that production behaviour intact while these
    tests stay about the request and the parsing.
    """
    from app.scraper import geo_cache
    monkeypatch.setattr(geo_cache, "lookup", lambda q: None)
    monkeypatch.setattr(geo_cache, "store", lambda *a, **k: None)

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


class TestRetryWithoutTheLeadingSegment:
    """A company address often begins with something that is not a place.

    GLEIF's record for Microsoft India Corporation is "CSC Services of Nevada,
    2215-B, Renaissance Drive, Las Vegas,, Renaissance Drive, 89119, US" — the
    agent's name in front, and a `city` field containing a street name, which is
    wrong at the source and stored faithfully anyway. Measured against Nominatim:
    the whole thing finds nothing; without the first segment it resolves to the
    street, rank 26.
    """

    def _geocoder(self, monkeypatch, answers):
        """Nominatim that answers per query, recording what it was asked."""
        from app.config import settings
        from app.scraper import geocode

        asked: list[str] = []
        monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)
        monkeypatch.setattr(geocode, "_throttle", lambda: None)
        geocode._full_cache.clear()

        class Resp:
            def __init__(self, payload): self._p = payload
            def raise_for_status(self): pass
            def json(self): return self._p

        class Client:
            def get(self, url, params=None):
                q = (params or {})["q"]
                asked.append(q)
                return Resp(answers.get(q, []))

        monkeypatch.setattr(geocode, "_get_client", lambda: Client())
        monkeypatch.setattr(geocode.geo_cache if hasattr(geocode, "geo_cache") else geocode,
                            "__name__", "geocode", raising=False)
        from app.scraper import geo_cache
        monkeypatch.setattr(geo_cache, "lookup", lambda q: None)
        monkeypatch.setattr(geo_cache, "store", lambda *a, **k: None)
        return geocode, asked

    def test_retries_without_the_agent_name(self, monkeypatch):
        hit = [{"lat": "36.10", "lon": "-115.12", "place_rank": 26}]
        geocode, asked = self._geocoder(monkeypatch, {
            "2215-B, Renaissance Drive, Las Vegas, 89119, US": hit,
        })
        got = geocode.geocode_full(
            "CSC Services of Nevada, 2215-B, Renaissance Drive, Las Vegas, 89119, US")
        assert got == ((36.10, -115.12), "exact")
        assert len(asked) == 2                      # full first, then shortened

    def test_does_not_retry_when_the_first_try_works(self, monkeypatch):
        # The retry costs a request from a one-per-second budget; it is for
        # addresses that would otherwise have no coordinates at all.
        hit = [{"lat": "51.5", "lon": "-0.1", "place_rank": 30}]
        geocode, asked = self._geocoder(monkeypatch, {"1 High St, London, GB": hit})
        assert geocode.geocode_full("1 High St, London, GB") is not None
        assert asked == ["1 High St, London, GB"]

    def test_never_reduces_an_address_to_a_country(self, monkeypatch):
        """"London, GB" must not become "GB": a bare country returns its
        centroid, the one answer worse than none."""
        geocode, asked = self._geocoder(monkeypatch, {})
        assert geocode.geocode_full("London, GB") is None
        assert asked == ["London, GB"]              # no retry attempted

    def test_still_returns_nothing_when_neither_form_resolves(self, monkeypatch):
        geocode, asked = self._geocoder(monkeypatch, {})
        assert geocode.geocode_full("A, B, C, D") is None
        assert asked == ["A, B, C, D", "B, C, D"]
