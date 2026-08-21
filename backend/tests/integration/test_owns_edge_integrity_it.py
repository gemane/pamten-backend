"""
Three ways an OWNS edge went wrong, all found from one bug report.

A user asked why Larry Page's badge on Alphabet was grey with a percentage in it
while Sergey Brin's, at almost the same stake, was orange. It was: Page's edge
carried 6.12% typed `unknown`, Brin's 6.16% typed `minority`. Pulling that thread
turned up two more, both in code that *recreates* an edge:

* nine companies owning themselves — Apple, Microsoft and Alphabet each "holding"
  7.48%, which is Vanguard's stake in each, so a filer's holding was landing on
  the issuer;
* the proxy-statement writer deleting an edge and rebuilding it from a hardcoded
  property list, silently dropping everything not on that list.

Against a real ArcadeDB because all three live in write paths, and a mocked
session accepts a self-loop or a lossy recreate as cheerfully as the real thing.
"""
import pytest

pytestmark = pytest.mark.integration


def _company(it_db, cid, name):
    it_db.run_command("CREATE (e:Entity {id:$id, name:$n})", {"id": cid, "n": name})


def _edge(it_db, oid, cid):
    rows = it_db.run_command(
        "MATCH (a {id:$o})-[r:OWNS]->(b {id:$c}) "
        "RETURN r.stake_percent AS stake, r.ownership_type AS ot, "
        "r.voting_power_pct AS vpp, r.source_url AS url, "
        "r.direct_or_indirect AS doi, r.psc_self_link AS pscl, "
        "r.interest_types AS its, r.credibility_score AS cred",
        {"o": oid, "c": cid})
    return rows[0] if rows else None


class TestACompanyCannotOwnItself:
    """Nine of these were live. The repeated 7.48% across Apple, Microsoft and
    Alphabet is Vanguard's stake in each, so the filer and the issuer were
    resolving to one node — a resolution bug showing up as a write."""

    def test_the_sec_writer_refuses_one(self, it_db):
        from app.scraper.runner import _upsert_owns_sec

        _company(it_db, "self-a", "Ouroboros PLC")
        _upsert_owns_sec("self-a", "self-a", "src", "minority", "2026-01-01", 7.48)

        assert it_db.run_command(
            "MATCH (a)-[r:OWNS]->(b) WHERE a.id = b.id RETURN count(r) AS n")[0]["n"] == 0

    def test_the_generic_writer_refuses_one(self, it_db):
        from app.scraper.runner import _upsert_owns

        _company(it_db, "self-b", "Ouroboros PLC")
        _upsert_owns("self-b", "self-b", "src")

        assert it_db.run_command(
            "MATCH (a)-[r:OWNS]->(b) WHERE a.id = b.id RETURN count(r) AS n")[0]["n"] == 0

    def test_a_genuine_edge_between_two_companies_still_writes(self, it_db):
        # The guard must not be so eager it stops the normal case.
        from app.scraper.runner import _upsert_owns_sec

        _company(it_db, "owner-x", "Vanguard")
        _company(it_db, "owned-y", "Alphabet")
        _upsert_owns_sec("owner-x", "owned-y", "src", "minority", "2026-01-01", 7.48)

        assert _edge(it_db, "owner-x", "owned-y")["stake"] == 7.48


class TestAStakeIsNeverStoredAsUnknown:
    def test_the_sec_writer_derives_a_missing_type(self, it_db):
        # The reported bug's shape: a percentage arriving with `unknown` beside it.
        from app.scraper.runner import _upsert_owns_sec

        _company(it_db, "page", "Larry Page Holdings")
        _company(it_db, "alphabet", "Alphabet Inc.")
        _upsert_owns_sec("page", "alphabet", "src", "unknown", "2026-01-01", 6.12)

        assert _edge(it_db, "page", "alphabet")["ot"] == "minority"

    def test_it_does_not_downgrade_a_real_type(self, it_db):
        # A 75% PSC that can also appoint directors is `controlling`. Re-deriving
        # from the percentage would call it `majority` and lose the appointment.
        from app.scraper.runner import _upsert_owns_sec

        _company(it_db, "psc-owner", "Holder Ltd")
        _company(it_db, "psc-co", "Held Ltd")
        _upsert_owns_sec("psc-owner", "psc-co", "src", "controlling", "2026-01-01", 75)

        assert _edge(it_db, "psc-owner", "psc-co")["ot"] == "controlling"


class TestTheProxyWriterKeepsTheEdgeItFound:
    """It used to DELETE the edge and CREATE a new one from seven named
    properties. Everything else on the edge went with it — and the list has only
    grown since, so each new property quietly joined the casualties."""

    def _seed(self, it_db):
        _company(it_db, "px-co", "Alphabet Inc.")
        it_db.run_command("CREATE (p:Person {id:'px-person', full_name:'Sergey Brin'})")
        it_db.run_command(
            "MATCH (p:Person {id:'px-person'}), (c {id:'px-co'}) "
            "CREATE (p)-[:OWNS {stake_percent:6.16, ownership_type:'minority', "
            "source_id:'sec', source_url:'https://sec.example.test/filing', "
            "credibility_score:98, direct_or_indirect:'direct', "
            "interest_types:['shareholding'], psc_self_link:'/link/x', until:null}]->(c)")

    def _run(self, monkeypatch):
        """Drive the writer with a canned proxy, so the test needs no network —
        and no BeautifulSoup, which the parser imports and this environment lacks."""
        import sys
        import types

        stub = types.ModuleType("app.scraper.proxy_statement")
        stub.fetch_proxy_ownership = lambda company: {
            "owners": [{"name": "Sergey Brin", "voting_power_pct": 51.0}]}
        monkeypatch.setitem(sys.modules, "app.scraper.proxy_statement", stub)

        from app.scraper.proxy_write import write_proxy_ownership
        return write_proxy_ownership("Alphabet Inc.", entity_id="px-co")

    def test_the_voting_percentage_is_added(self, it_db, monkeypatch):
        self._seed(it_db)
        self._run(monkeypatch)
        assert _edge(it_db, "px-person", "px-co")["vpp"] == 51.0

    def test_and_nothing_else_is_lost(self, it_db, monkeypatch):
        self._seed(it_db)
        self._run(monkeypatch)
        e = _edge(it_db, "px-person", "px-co")
        assert e["url"] == "https://sec.example.test/filing", "provenance was dropped"
        assert e["cred"] == 98
        assert e["doi"] == "direct", "GLEIF's direct/ultimate marker was dropped"
        assert e["pscl"] == "/link/x", "the PSC refresh key was dropped"
        assert e["its"] == ["shareholding"]
        assert e["stake"] == 6.16 and e["ot"] == "minority"

    def test_it_reconciles_a_stake_left_typed_unknown(self, it_db, monkeypatch):
        # The proxy writer touches edges other sources built, so it is one of the
        # places the contradiction can be cleared. Seeded with the broken pair
        # deliberately: with a healthy edge, a writer that stopped re-deriving
        # would look identical.
        _company(it_db, "px-co", "Alphabet Inc.")
        it_db.run_command("CREATE (p:Person {id:'px-person', full_name:'Sergey Brin'})")
        it_db.run_command(
            "MATCH (p:Person {id:'px-person'}), (c {id:'px-co'}) "
            "CREATE (p)-[:OWNS {stake_percent:6.12, ownership_type:'unknown', until:null}]->(c)")

        self._run(monkeypatch)
        assert _edge(it_db, "px-person", "px-co")["ot"] == "minority"

    def test_it_does_not_duplicate_the_edge(self, it_db, monkeypatch):
        self._seed(it_db)
        self._run(monkeypatch)
        assert it_db.run_command(
            "MATCH (p {id:'px-person'})-[r:OWNS]->(c {id:'px-co'}) "
            "RETURN count(r) AS n")[0]["n"] == 1


class TestWhatAPeerSendsIsReconciledToo:
    """Federation writes OWNS edges from a peer's snapshot, and had its own version
    of the same two bugs."""

    def _snapshot(self, ownerships):
        return {"format": "owlgraph-federation", "version": 1,
                "entities": [{"name": "Alphabet Inc.", "type": "company", "wikidata_id": "Q20800404"}],
                "persons": [{"full_name": "Larry Page", "wikidata_id": "Q4934"}],
                "ownerships": ownerships}

    def test_a_stake_arriving_on_an_unknown_edge_retypes_it(self, it_db):
        """`COALESCE(r.ownership_type, $otype)` reads as "keep what we have, else
        take theirs" — but 'unknown' is a VALUE, so it was always kept, while the
        null stake beside it was filled in from the peer. The edge then held a
        percentage typed 'unknown': exactly the grey badge that was reported."""
        from app.routers.federation import import_snapshot

        it_db.run_command("CREATE (:Entity {id:'fed-co', name:'Alphabet Inc.', "
                          "name_normalized:'alphabet', type:'company', wikidata_id:'Q20800404'})")
        it_db.run_command("CREATE (:Person {id:'fed-p', full_name:'Larry Page', wikidata_id:'Q4934'})")
        it_db.run_command("MATCH (p:Person{id:'fed-p'}),(c:Entity{id:'fed-co'}) "
                          "CREATE (p)-[:OWNS {stake_percent:null, ownership_type:'unknown'}]->(c)")

        import_snapshot(self._snapshot([{
            "owner": {"kind": "person", "full_name": "Larry Page", "wikidata_id": "Q4934"},
            "owned": {"name": "Alphabet Inc.", "wikidata_id": "Q20800404"},
            "stake_percent": 6.12, "ownership_type": "minority"}]), "Peer: Types", 70)

        edge = it_db.run_command(
            "MATCH (:Person{wikidata_id:'Q4934'})-[r:OWNS]->(:Entity{wikidata_id:'Q20800404'}) "
            "RETURN r.stake_percent AS s, r.ownership_type AS ot")[0]
        assert edge["s"] == 6.12
        assert edge["ot"] == "minority", "the stake arrived but the type stayed unknown"

    def test_a_peer_asserting_a_self_loop_is_refused(self, it_db):
        # A peer claiming a company owns itself has resolved two names to one node.
        # Importing it would plant their resolution bug in our graph.
        from app.routers.federation import import_snapshot

        counts = import_snapshot(self._snapshot([{
            "owner": {"kind": "entity", "name": "Alphabet Inc.", "wikidata_id": "Q20800404"},
            "owned": {"name": "Alphabet Inc.", "wikidata_id": "Q20800404"},
            "stake_percent": 7.48, "ownership_type": "minority"}]), "Peer: Loop", 70)

        assert counts["ownerships"] == 0
        assert it_db.run_command(
            "MATCH (a)-[r:OWNS]->(b) WHERE a.id = b.id RETURN count(r) AS n")[0]["n"] == 0
