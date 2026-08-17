"""
Scraping a person: who they are, and the companies they run, founded or own.

The company scrape reads a company and finds its people. This is the other
direction, and it exists because searching a person's name used to do something
worse than nothing — the top Wikidata hit for "Larry Page" is the man, and he was
written into the graph as a company.

Wikidata is mocked here (the shapes are copied from real responses, including
Larry Page's actual link list); `fetch_person_companies` and
`fetch_person_details_for` are exercised against the live API in their own right
during development. What these tests pin is what reaches the graph — a Person
with their dates, HAS_ROLE and OWNS edges, and *not* the building, the programme
or the software Wikidata also says he founded.
"""
import pytest

pytestmark = pytest.mark.integration


#: Larry Page's real link list, trimmed. The last three are why the filter exists:
#: a building, a programme and a piece of software, all recorded as "founded by".
LINKS = [
    {"qid": "Q95", "name": "Google", "country": "US", "roles": ["Founder"],
     "instances": ["Q4830453"], "is_company": True},
    {"qid": "Q20800404", "name": "Alphabet Inc.", "country": "US",
     "roles": ["Founder", "owner", "Board member"],
     "instances": ["Q4830453", "Q891723"], "is_company": True},
    {"qid": "Q90921713", "name": "H211, LLC", "country": "US", "roles": ["Founder"],
     "instances": ["Q149789"], "is_company": True},
    {"qid": "Q7540126", "name": "Googleplex", "country": "US", "roles": ["Founder"],
     "instances": ["Q1497364"], "is_company": False},
    {"qid": "Q914359", "name": "Google Photos", "country": None, "roles": ["Founder"],
     "instances": ["Q914359"], "is_company": False},
]

DETAIL = {
    "full_name": "Larry Page", "description": "American computer scientist",
    "birth_date": "1973-03-26", "death_date": None, "birth_place": "East Lansing",
    "nationalities": ["US"], "aliases": ["Lawrence Edward Page"], "is_human": True,
}


@pytest.fixture
def wikidata(it_db, monkeypatch):
    """A runner wired to canned Wikidata answers."""
    from app.config import settings
    from app.scraper import runner

    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_WIKIDATA_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)
    monkeypatch.setattr(runner, "get_source_enabled", lambda _n: True)
    monkeypatch.setattr(runner, "search_entity", lambda q, limit=3: [{"id": "Q4934", "label": q}])
    monkeypatch.setattr("app.scraper.wikidata.fetch_person_details_for", lambda qid: DETAIL)
    monkeypatch.setattr("app.scraper.wikidata.fetch_person_companies",
                        lambda qid, limit=60: list(LINKS))
    return runner


def _graph():
    from app.database import db
    with db.get_session() as s:
        return {
            "people": [r["n"] for r in s.run("MATCH (p:Person) RETURN p.full_name AS n")],
            "companies": [r["n"] for r in s.run("MATCH (e:Entity) RETURN e.name AS n")],
            "roles": sorted((r["role"], r["e"]) for r in s.run(
                "MATCH (:Person)-[r:HAS_ROLE]->(e:Entity) RETURN r.role AS role, e.name AS e")),
            "owns": [r["e"] for r in s.run(
                "MATCH (:Person)-[:OWNS]->(e:Entity) RETURN e.name AS e")],
        }


class TestWhatReachesTheGraph:
    def test_the_person_is_written_with_their_detail(self, wikidata):
        from app.database import db

        wikidata.run_scrape_person("Larry Page")
        with db.get_session() as s:
            p = s.run("MATCH (p:Person {wikidata_id:'Q4934'}) RETURN p.full_name AS n, "
                      "p.birth_date AS b, p.search_text AS st").single()
        assert p["n"] == "Larry Page" and p["b"] == "1973-03-26"
        assert "Lawrence" in p["st"], "aliases must be searchable"

    def test_roles_and_ownership_become_edges(self, wikidata):
        wikidata.run_scrape_person("Larry Page")
        g = _graph()
        assert ("Founder", "Google") in g["roles"]
        assert ("Board member", "Alphabet Inc.") in g["roles"]
        assert g["owns"] == ["Alphabet Inc."], "P127 is ownership, not a job title"

    def test_a_building_a_programme_and_an_app_are_not_companies(self, wikidata):
        # Wikidata's "founded by" is loose. Writing these would invent three
        # companies that do not exist and hang a founder role off each.
        wikidata.run_scrape_person("Larry Page")
        companies = _graph()["companies"]
        assert sorted(companies) == ["Alphabet Inc.", "Google", "H211, LLC"]
        assert "Googleplex" not in companies and "Google Photos" not in companies

    def test_it_is_idempotent(self, wikidata):
        wikidata.run_scrape_person("Larry Page")
        first = _graph()
        wikidata.run_scrape_person("Larry Page")
        assert _graph() == first, "a second scrape must not duplicate anything"

    def test_the_person_is_stamped_so_freshness_can_see_them(self, wikidata):
        from app.database import db

        wikidata.run_scrape_person("Larry Page")
        with db.get_session() as s:
            p = s.run("MATCH (p:Person {wikidata_id:'Q4934'}) RETURN p.last_scraped_at AS ls, "
                      "p.on_demand_scraped AS od").single()
        assert p["ls"] and p["od"] is True


class TestWhenItIsNotAPerson:
    def test_a_company_target_is_refused(self, wikidata, monkeypatch):
        # The mirror of the original bug: this path must not write a company into
        # the person shape either.
        monkeypatch.setattr("app.scraper.wikidata.fetch_person_details_for", lambda qid: None)
        out = wikidata.run_scrape_person("Google")
        assert out["status"] == "not_a_person"
        assert _graph()["people"] == []

    def test_an_item_that_does_not_confirm_it_is_human_is_refused(self, wikidata, monkeypatch):
        # Detail came back, but nothing in it says "person". The wrapper already
        # returns None for those; the runner checks anyway, because the cost of
        # trusting it is a company written into the person shape.
        for flag in (False, None):
            monkeypatch.setattr("app.scraper.wikidata.fetch_person_details_for",
                                lambda qid, f=flag: {**DETAIL, "is_human": f})
            assert wikidata.run_scrape_person("Google")["status"] == "not_a_person"
            assert _graph()["people"] == []

    def test_nothing_found_is_reported_plainly(self, wikidata, monkeypatch):
        monkeypatch.setattr(wikidata, "search_entity", lambda q, limit=3: [])
        assert wikidata.run_scrape_person("zzz nobody")["status"] == "no_results"


class TestThroughEnsure:
    """`/scraper/ensure` has to route a person question to the person path."""

    def test_a_name_with_no_company_reaches_the_person_scrape(self, wikidata, monkeypatch):
        from app.scraper import ondemand
        from app.scraper.scraper_registry import ScraperSpec, register

        monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
        register(ScraperSpec("faux", lambda q, d, c=None: {"status": "no_results", "total": 0},
                             lambda: True, kind="instant"))

        out = ondemand.ensure_scrape("Larry Page", depth=1, force=True)
        assert out["kind"] == "person" and out["person_id"]
        assert out["profile"]["person"]["full_name"] == "Larry Page"
        assert {h["entity"]["name"] for h in out["profile"]["holdings"]} == {"Alphabet Inc."}

    def test_a_person_already_known_is_served_without_scraping_again(self, wikidata, monkeypatch):
        from app.scraper import ondemand

        ondemand.ensure_scrape("Larry Page", depth=1, force=True)   # first: scrapes
        calls: list = []
        monkeypatch.setattr(wikidata, "run_scrape_person",
                            lambda q, c=None: calls.append(q) or {"status": "ok"})

        out = ondemand.ensure_scrape("Larry Page", depth=1, force=False)
        assert out["kind"] == "person" and out["reason"] == "fresh" and calls == []


class TestWhenTheNameIsNotTheKind:
    """The reported bug, end to end.

    Wikidata's hits for "Steve Jobs" are the 2015 film, the book, then the man.
    Taking the first wrote the film into the graph as a company — and because
    that counted as a successful scrape, the person was never touched.
    """

    HITS = [
        {"id": "Q18754959", "label": "Steve Jobs"},     # the film
        {"id": "Q16460065", "label": "Steve Jobs"},     # the book
        {"id": "Q19837", "label": "Steve Jobs"},        # the man
    ]
    FACTS = {
        "Q18754959": {"instances": ["Q11424"], "is_human": False, "is_company": False},
        "Q16460065": {"instances": ["Q3331189"], "is_human": False, "is_company": False},
        "Q19837": {"instances": ["Q5"], "is_human": True, "is_company": False},
    }

    @pytest.fixture
    def ambiguous(self, wikidata, monkeypatch):
        monkeypatch.setattr(wikidata, "search_entity", lambda q, limit=3: list(self.HITS))
        monkeypatch.setattr("app.scraper.wikidata.classify_candidates", lambda qids: self.FACTS)
        monkeypatch.setattr("app.scraper.wikidata.fetch_person_details_for",
                            lambda qid: {**DETAIL, "full_name": "Steve Jobs"} if qid == "Q19837" else None)
        monkeypatch.setattr("app.scraper.wikidata.fetch_person_companies",
                            lambda qid, limit=60: [LINKS[0]] if qid == "Q19837" else [])
        return wikidata

    def test_the_company_path_refuses_the_film(self, ambiguous):
        from app.database import db

        out = ambiguous.run_scrape("Steve Jobs", depth=0)
        assert out["status"] == "not_a_company"
        with db.get_session() as s:
            assert s.run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"] == 0

    def test_the_person_path_finds_the_man_third_in_the_list(self, ambiguous):
        from app.database import db

        out = ambiguous.run_scrape_person("Steve Jobs")
        assert out["status"] == "ok" and out["qid"] == "Q19837"
        with db.get_session() as s:
            p = s.run("MATCH (p:Person) RETURN p.full_name AS n, p.wikidata_id AS q").single()
        assert (p["n"], p["q"]) == ("Steve Jobs", "Q19837")

    def test_ensure_ends_up_with_the_person_and_no_film(self, ambiguous, monkeypatch):
        from app.database import db
        from app.scraper import ondemand
        from app.scraper.scraper_registry import ScraperSpec, register

        monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
        register(ScraperSpec("wikidata", lambda q, d, c=None: ambiguous.run_scrape(q, d, c),
                             lambda: True, kind="instant", depth_aware=True))

        out = ondemand.ensure_scrape("Steve Jobs", depth=1, force=True)
        assert out["kind"] == "person"
        assert out["profile"]["person"]["full_name"] == "Steve Jobs"
        with db.get_session() as s:
            names = [r["n"] for r in s.run("MATCH (e:Entity) RETURN e.name AS n")]
        assert "Steve Jobs" not in names, "the film was written as a company again"
