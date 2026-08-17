"""
People have to be findable, and must not be written as companies.

Both of these were live on the dev graph at once, from one search for "Larry Page":

* **He could not be found.** `/search` matches persons through a FULL_TEXT index on
  `search_text`, and nothing on the write path ever set it — 174 of 177 persons had
  none, so person search returned nothing for almost everybody in the graph.
* **He was written as a company.** `infer_entity_type` falls back to "company" for any
  P31 it does not recognise, and Q5 (human) is one of those. Owners and officers are
  checked for Q5 before they are written; the *search target* never was.

Against a real ArcadeDB because both live in Cypher a mocked session would accept
regardless — the first is a column that silently stayed null, the second a query
that wrote the wrong node type perfectly successfully.
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def runner(it_db, monkeypatch):
    from app.config import settings
    from app.scraper import runner as mod

    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_WIKIDATA_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)
    monkeypatch.setattr(mod, "get_source_enabled", lambda _n: True)
    return mod


def _person(full_name):
    from app.db.arcadedb import run_sql
    rows = run_sql("SELECT full_name, search_text, alias FROM Person WHERE full_name = :n",
                   {"n": full_name})
    return rows[0] if rows else None


def _find(query):
    """What /search would return, ranked, through the same code path."""
    from app.routers.search import search
    return [(r["type"], r["node"].get("name") or r["node"].get("full_name"))
            for r in search(q=query, limit=10)]


class TestAPersonIsFindable:
    def test_a_person_written_by_name_gets_search_text(self, runner):
        runner._upsert_person_by_name("Ada Lovelace", source_id=None)
        assert _person("Ada Lovelace")["search_text"] == "Ada Lovelace"

    def test_a_person_written_with_detail_gets_search_text(self, runner):
        runner._upsert_person(full_name="Larry Page", nationality="US", description="",
                              wikidata_id="Q4934", aliases=["Lawrence Edward Page"])
        assert _person("Larry Page")["search_text"] == "Larry Page Lawrence Edward Page"

    def test_and_search_actually_finds_them(self, runner):
        # The end of the story: the column exists so this query works.
        runner._upsert_person(full_name="Larry Page", nationality="US", description="",
                              wikidata_id="Q4934", aliases=["Lawrence Edward Page"])
        assert ("Person", "Larry Page") in _find("Larry Page")

    def test_an_alias_finds_them_too(self, runner):
        runner._upsert_person(full_name="Larry Page", nationality="US", description="",
                              wikidata_id="Q4934", aliases=["Lawrence Edward Page"])
        assert ("Person", "Larry Page") in _find("Lawrence")

    def test_aliases_arriving_later_are_folded_in(self, runner):
        # First seen as a bare name from one source, enriched by the next: the
        # derived column has to be recomputed, not blank-filled, or the alias is
        # never searchable.
        runner._upsert_person_by_name("Larry Page", source_id=None)
        assert _person("Larry Page")["search_text"] == "Larry Page"

        runner._upsert_person(full_name="Larry Page", nationality="US", description="",
                              wikidata_id="Q4934", aliases=["Lawrence Edward Page"])
        assert "Lawrence" in _person("Larry Page")["search_text"]
        assert ("Person", "Larry Page") in _find("Lawrence")


class TestAHumanIsNotACompany:
    def _wikidata_says(self, runner, monkeypatch, instances, name="Larry Page"):
        monkeypatch.setattr(runner, "fetch_company_data",
                            lambda qid: {"name": name, "instances": instances,
                                         "description": "", "aliases": []})

    def test_a_human_search_target_writes_no_entity(self, runner, monkeypatch):
        from app.db.arcadedb import run_sql

        self._wikidata_says(runner, monkeypatch, ["Q5"])
        runner._scrape_node("Q4934", 0, set(), [], source_id="s1")

        assert run_sql("SELECT count(*) AS n FROM Entity")[0]["n"] == 0, \
            "a person was written into the company graph"

    def test_a_company_search_target_still_writes_one(self, runner, monkeypatch):
        from app.db.arcadedb import run_sql

        # Q4830453 = business. The guard must be about humans, not about every
        # P31 the type table does not list.
        self._wikidata_says(runner, monkeypatch, ["Q4830453"], name="Acme GmbH")
        runner._scrape_node("Q42", 0, set(), [], source_id="s1")

        assert run_sql("SELECT count(*) AS n FROM Entity")[0]["n"] == 1

    def test_an_unknown_type_is_still_treated_as_a_company(self, runner, monkeypatch):
        # The existing fallback is deliberate — Wikidata has endless company
        # classes — and this change must not narrow it.
        from app.db.arcadedb import run_sql

        self._wikidata_says(runner, monkeypatch, ["Q99999999"], name="Odd Co")
        runner._scrape_node("Q43", 0, set(), [], source_id="s1")

        assert run_sql("SELECT count(*) AS n FROM Entity")[0]["n"] == 1


class TestTheProfileCarriesAHistory:
    """A career is ended roles. The profile used to drop them, so Steve Jobs came
    back with three positions out of six — missing both spells on Apple's board
    and his run as its CEO — and no timeline could show what was never sent."""

    def _jobs(self, runner):
        person = runner._upsert_person_by_name("Steve Jobs", source_id=None)
        apple = runner._upsert_entity_by_name(name="Apple Inc.", entity_type="company")
        runner._upsert_role(person, apple, "Board Member", "s1",
                            since="1977-03-01", until="1985-09-01")
        runner._upsert_role(person, apple, "Board Member", "s1",
                            since="1997-01-01", until="2011-10-05")
        runner._upsert_role(person, apple, "Founder", "s1", since="1976-04-01")
        return person

    def _positions(self, person_id):
        from app.routers.search import get_person_profile
        return [(p["entity"]["name"], p["role"].get("role"), p["role"].get("since"))
                for p in get_person_profile(person_id)["positions"]]

    def test_an_ended_role_is_returned(self, runner):
        person = self._jobs(runner)
        assert ("Apple Inc.", "Board Member", "1977-03-01") in self._positions(person)

    def test_both_spells_survive(self, runner):
        # Keyed on the start date, not just company + role — the same identity
        # the writer uses. Collapsing them loses the second appointment, which is
        # usually the one worth knowing about.
        person = self._jobs(runner)
        boards = [p for p in self._positions(person) if p[1] == "Board Member"]
        assert len(boards) == 2 and {b[2] for b in boards} == {"1977-03-01", "1997-01-01"}

    def test_a_current_role_still_comes_back(self, runner):
        person = self._jobs(runner)
        assert ("Apple Inc.", "Founder", "1976-04-01") in self._positions(person)

    def test_an_accidental_duplicate_is_still_collapsed(self, runner):
        # Deduping still has a job: the same assertion twice is one position.
        person = self._jobs(runner)
        apple = runner._upsert_entity_by_name(name="Apple Inc.", entity_type="company")
        runner._upsert_role(person, apple, "Board Member", "s2", since="1977-03-01")
        boards = [p for p in self._positions(person) if p[1] == "Board Member"]
        assert len(boards) == 2
