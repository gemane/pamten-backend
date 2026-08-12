"""Counting companies by jurisdiction versus headquarters, against a real ArcadeDB.

The property being grouped on cannot be parameterised — ArcadeDB's Cypher will not
take `e[$prop]` — so each basis is a literal query string. That is exactly the kind
of thing a mocked session accepts happily while the real database returns nothing,
which is why these run against ArcadeDB.

A third basis, `subdivision`, groups on the ISO 3166-2 jurisdiction GLEIF states
(`US-DE`). It shares every code path with the other two, so what its tests are
really pinning is that a finer grouping did not disturb the coarser one.

The case that matters is a company registered in one place and run from another:
BARCLAYS CAPITAL (CAYMAN) LIMITED is KY by jurisdiction and GB by headquarters. If
the two bases ever return the same thing, the feature is doing nothing.
"""
import pytest
from fastapi import HTTPException

from app.routers.entities import (
    get_entities_by_country,
    get_entities_for_country,
    get_entities_without_country,
)

pytestmark = pytest.mark.integration


def _entity(it_db, eid: str, name: str, country: str | None, hq: str | None,
            subdivision: str | None = None) -> None:
    props = [f"id:'{eid}'", f"name:'{name}'", "type:'company'"]
    if country is not None:
        props.append(f"country:'{country}'")
    if hq is not None:
        props.append(f"hq_country:'{hq}'")
    if subdivision is not None:
        props.append(f"jurisdiction_code:'{subdivision}'")
    it_db.run_command(f"CREATE (:Entity {{{', '.join(props)}}})")


def _seed(it_db):
    _entity(it_db, "e1", "Offshore Holdings", "KY", "GB")   # registered KY, run from GB
    _entity(it_db, "e2", "Berlin Werke", "DE", "DE")        # same either way
    _entity(it_db, "e3", "No HQ Recorded", "DE", None)      # placeable only by jurisdiction
    _entity(it_db, "e4", "Nowhere Ltd", None, None)         # placeable by neither


def counts(groups):
    return {g["country"]: g["count"] for g in groups}


class TestCounting:
    def test_jurisdiction_counts_where_companies_are_registered(self, it_db):
        _seed(it_db)
        assert counts(get_entities_by_country(basis="jurisdiction")) == {"KY": 1, "DE": 2, None: 1}

    def test_headquarters_counts_where_they_are_run(self, it_db):
        _seed(it_db)
        # The KY company moves to GB, and the one with no HQ joins the unplaced group.
        assert counts(get_entities_by_country(basis="hq")) == {"GB": 1, "DE": 1, None: 2}

    def test_the_default_is_jurisdiction(self, client):
        """Existing callers must not silently change meaning.

        Asserted against the published schema rather than by calling the function
        with no argument: that passes FastAPI's Query object rather than the
        string, so it proves nothing about what an HTTP caller gets.
        """
        for path in ("/v1/entities/by-country", "/v1/entities/without-country"):
            params = client.get("/openapi.json").json()["paths"][path]["get"]["parameters"]
            basis = next(p for p in params if p["name"] == "basis")
            assert basis["required"] is False
            assert basis["schema"]["default"] == "jurisdiction"

    def test_every_company_is_accounted_for_in_both(self, it_db):
        """The null group exists so the totals add up — a map that quietly drops a
        tenth of the graph is wrong about how much it is showing."""
        _seed(it_db)
        for basis in ("jurisdiction", "hq"):
            assert sum(g["count"] for g in get_entities_by_country(basis=basis)) == 4

    def test_counts_are_ordered_by_size(self, it_db):
        _seed(it_db)
        placed = [g for g in get_entities_by_country(basis="jurisdiction") if g["country"]]
        assert [g["country"] for g in placed] == ["DE", "KY"]   # 2 then 1

    def test_equal_counts_are_broken_alphabetically(self, it_db):
        """Without a tie-break the row order varies between identical calls, which
        is a trap for anything that reads the endpoint. Needs countries with the
        SAME count — the ordering test above never exercises it."""
        _entity(it_db, "a1", "Alpha", "ZW", None)
        _entity(it_db, "a2", "Beta", "AT", None)
        _entity(it_db, "a3", "Gamma", "MX", None)

        placed = [g["country"] for g in get_entities_by_country(basis="jurisdiction")
                  if g["country"]]
        assert placed == ["AT", "MX", "ZW"]      # all count 1, so alphabetical


class TestListing:
    def test_a_country_lists_different_companies_per_basis(self, it_db):
        _seed(it_db)
        by_j = [e["name"] for e in get_entities_for_country("KY", basis="jurisdiction", limit=200)]
        by_h = [e["name"] for e in get_entities_for_country("KY", basis="hq", limit=200)]
        assert by_j == ["Offshore Holdings"]
        assert by_h == []                       # it is not run from the Caymans
        assert [e["name"] for e in get_entities_for_country("GB", basis="hq", limit=200)] \
            == ["Offshore Holdings"]

    def test_the_unplaceable_can_be_listed(self, it_db):
        _seed(it_db)
        assert [e["name"] for e in get_entities_without_country(basis="jurisdiction", limit=200)] \
            == ["Nowhere Ltd"]
        assert [e["name"] for e in get_entities_without_country(basis="hq", limit=200)] \
            == ["No HQ Recorded", "Nowhere Ltd"]


class TestSubdivision:
    """The third basis: where a company is registered, one level finer than the
    country. GLEIF states an ISO 3166-2 jurisdiction for about 1% of records, and
    on the real data 35 of 47 US companies are US-DE — the Delaware concentration
    the country-level map cannot show."""

    def _seed_us(self, it_db):
        _entity(it_db, "u1", "Delaware One", "US", "US", "US-DE")
        _entity(it_db, "u2", "Delaware Two", "US", "US", "US-DE")
        _entity(it_db, "u3", "Nevada Inc", "US", "US", "US-NV")
        _entity(it_db, "u4", "Plain American", "US", "US", None)   # US, no subdivision stated
        _entity(it_db, "u5", "Toronto Co", "CA", "CA", "CA-ON")
        _entity(it_db, "u6", "Berlin Werke", "DE", "DE", None)     # a country that uses none

    def test_counts_group_by_subdivision(self, it_db):
        self._seed_us(it_db)
        assert counts(get_entities_by_country(basis="subdivision")) == {
            "US-DE": 2, "CA-ON": 1, "US-NV": 1, None: 2,
        }

    def test_a_country_that_states_none_falls_into_the_null_group(self, it_db):
        """Absent means "not stated", never "none" — so the American company with
        no subdivision must be countable, not silently dropped."""
        self._seed_us(it_db)
        assert [e["name"] for e in get_entities_without_country(basis="subdivision", limit=200)] \
            == ["Berlin Werke", "Plain American"]

    def test_a_subdivision_lists_its_companies(self, it_db):
        self._seed_us(it_db)
        assert [e["name"] for e in get_entities_for_country("US-DE", basis="subdivision", limit=200)] \
            == ["Delaware One", "Delaware Two"]

    def test_the_country_basis_still_counts_countries(self, it_db):
        """The regression that matters: adding a finer basis must not change what
        the existing map shows. All four American companies stay US."""
        self._seed_us(it_db)
        assert counts(get_entities_by_country(basis="jurisdiction")) == {"US": 4, "CA": 1, "DE": 1}

    def test_a_subdivision_code_is_not_a_country_code(self, it_db):
        """'US-DE' must not leak into the country basis, and 'US' must not collect
        subdivision rows — they are separate properties, not a prefix match."""
        self._seed_us(it_db)
        assert get_entities_for_country("US-DE", basis="jurisdiction", limit=200) == []
        assert get_entities_for_country("US", basis="subdivision", limit=200) == []


class TestRejectingNonsense:
    @pytest.mark.parametrize("basis", ["", "HQ ", "country", "hq_country", "Jurisdiction",
                                       "subdivisions", "jurisdiction_code"])
    def test_an_unknown_basis_is_refused(self, it_db, basis):
        """Not silently defaulted: a typo in a client would otherwise render the
        wrong map with nothing to say it is wrong."""
        with pytest.raises(HTTPException) as exc:
            get_entities_by_country(basis=basis)
        assert exc.value.status_code == 422

    def test_the_other_endpoints_refuse_it_too(self, it_db):
        for call in (lambda: get_entities_for_country("DE", basis="nope", limit=10),
                     lambda: get_entities_without_country(basis="nope", limit=10)):
            with pytest.raises(HTTPException) as exc:
                call()
            assert exc.value.status_code == 422
