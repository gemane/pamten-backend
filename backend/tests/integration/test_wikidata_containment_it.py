"""
Phase 2 of the source-quality strategy: containing Wikidata's ownership writes.

Two mechanisms, both against a real ArcadeDB because both live in Cypher a mock
would accept regardless:

* **The freshness gate.** A source may refresh `last_scraped_at` on an edge at
  or below its own credibility, and no higher. Without it, a Wikidata visit
  re-confirming an SEC edge laundered a register fact's freshness through a
  community source — and the staleness pass reads that timestamp.
* **The staleness pass.** Wikidata has no retirement signal: a deleted statement
  just stops being seen, and its edge stood forever, indistinguishable from a
  confirmed fact. Community edges nothing has confirmed in 180 days are marked
  `stale` — dimmed by the UI, never deleted, never closed, because an
  unconfirmed community edge is weak evidence of removal.
"""
import pytest

pytestmark = pytest.mark.integration

WD_CRED = 80
SEC_CRED = 98
OLD = "2020-01-01T00:00:00Z"


@pytest.fixture
def graph(it_db):
    it_db.run_command("CREATE (e:Entity {id:'own-co', name:'Held Co'})")
    it_db.run_command("CREATE (e:Entity {id:'wd-owner', name:'Community Holdings'})")
    it_db.run_command("CREATE (e:Entity {id:'sec-owner', name:'Filing Capital'})")
    return it_db


def _edge(it_db, owner):
    rows = it_db.run_command(
        "MATCH (a {id:$o})-[r:OWNS]->(b {id:'own-co'}) "
        "RETURN r.last_scraped_at AS seen, r.stale AS stale, r.source_id AS sid",
        {"o": owner})
    return rows[0] if rows else None


def _seed_edge(it_db, owner, cred, seen=OLD, stale=None, source="src-x"):
    it_db.run_command(
        "MATCH (a {id:$o}), (b {id:'own-co'}) CREATE (a)-[:OWNS "
        "{credibility_score:$c, last_scraped_at:$s, stale:$st, "
        "source_id:$src, until:null}]->(b)",
        {"o": owner, "c": cred, "s": seen, "st": stale, "src": source})


class TestTheFreshnessGate:
    def test_wikidata_cannot_refresh_a_register_edge(self, graph):
        # The SEC edge's timestamp is what "last confirmed against the source"
        # means, and what staleness reads. Wikidata agreeing is a claim, not a
        # confirmation by the register.
        from app.scraper.runner import _upsert_owns

        _seed_edge(graph, "sec-owner", SEC_CRED)
        _upsert_owns("sec-owner", "own-co", "wd-src", credibility_score=WD_CRED)

        assert _edge(graph, "sec-owner")["seen"] == "2020-01-01T00:00Z"

    def test_but_its_corroboration_is_still_recorded(self, graph):
        # Containment must not cost the corroboration signal — a community source
        # agreeing with a register is exactly what the claim is for.
        from app.scraper.runner import _upsert_owns

        _seed_edge(graph, "sec-owner", SEC_CRED)
        _upsert_owns("sec-owner", "own-co", "wd-src", credibility_score=WD_CRED)

        claims = graph.run_command(
            "MATCH (c:Claim) WHERE c.from_id = 'sec-owner' AND c.to_id = 'own-co' "
            "RETURN c.source_id AS sid")
        assert {r["sid"] for r in claims} == {"wd-src"}

    def test_wikidata_refreshes_its_own_edge_and_clears_stale(self, graph):
        from app.scraper.runner import _upsert_owns

        _seed_edge(graph, "wd-owner", WD_CRED, stale=True)
        _upsert_owns("wd-owner", "own-co", "wd-src", credibility_score=WD_CRED)

        e = _edge(graph, "wd-owner")
        assert e["seen"] != "2020-01-01T00:00Z"
        assert e["stale"] is False

    def test_a_register_source_can_refresh_a_community_edge(self, graph):
        # The gate is a floor, not a wall: higher credibility confirming a lower
        # edge is a stronger statement than the edge itself.
        from app.scraper.runner import _upsert_owns

        _seed_edge(graph, "wd-owner", WD_CRED, stale=True)
        _upsert_owns("wd-owner", "own-co", "sec-src", credibility_score=SEC_CRED)

        e = _edge(graph, "wd-owner")
        assert e["seen"] != "2020-01-01T00:00Z" and e["stale"] is False


class TestTheStalenessPass:
    def test_an_old_community_edge_is_marked(self, graph):
        from app.scraper.maintenance import mark_stale_ownership

        _seed_edge(graph, "wd-owner", WD_CRED, seen=OLD)
        res = mark_stale_ownership(days=180)

        assert res["marked"] == 1
        assert _edge(graph, "wd-owner")["stale"] is True

    def test_marked_never_deleted_never_closed(self, graph):
        from app.scraper.maintenance import mark_stale_ownership

        _seed_edge(graph, "wd-owner", WD_CRED, seen=OLD)
        mark_stale_ownership(days=180)

        rows = graph.run_command(
            "MATCH (a {id:'wd-owner'})-[r:OWNS]->(b {id:'own-co'}) "
            "RETURN r.until AS until")
        assert len(rows) == 1 and rows[0]["until"] is None, \
            "staleness must not become a closure — nobody stated an end date"

    def test_a_fresh_community_edge_is_not_marked(self, graph):
        from app.scraper.maintenance import mark_stale_ownership
        from app.scraper.bulk_import import _now_iso

        _seed_edge(graph, "wd-owner", WD_CRED, seen=_now_iso())
        assert mark_stale_ownership(days=180)["marked"] == 0

    def test_an_old_register_edge_is_exempt(self, graph):
        # Registers retire facts properly (deltas, snapshot diffs, 0% amendments);
        # age alone says nothing about them.
        from app.scraper.maintenance import mark_stale_ownership

        _seed_edge(graph, "sec-owner", SEC_CRED, seen=OLD)
        mark_stale_ownership(days=180)
        assert not _edge(graph, "sec-owner")["stale"]

    def test_a_register_vouched_pair_is_exempt(self, graph):
        # The edge carries community attribution, but a register claim covers the
        # same pair — the fact is vouched for, whoever happens to own the edge.
        from app.scraper.maintenance import mark_stale_ownership

        _seed_edge(graph, "wd-owner", WD_CRED, seen=OLD)
        graph.run_command(
            "CREATE (c:Claim {claim_key:'owns|wd-owner|own-co|sec', kind:'owns', "
            "from_id:'wd-owner', to_id:'own-co', source_id:'sec', credibility_score:98})")
        mark_stale_ownership(days=180)
        assert not _edge(graph, "wd-owner")["stale"]

    def test_a_closed_edge_is_left_alone(self, graph):
        # History is not stale, it is history.
        from app.scraper.maintenance import mark_stale_ownership

        graph.run_command(
            "MATCH (a {id:'wd-owner'}), (b {id:'own-co'}) CREATE (a)-[:OWNS "
            "{credibility_score:80, last_scraped_at:$s, until:'2021-06-30', "
            "source_id:'wd-src'}]->(b)", {"s": OLD})
        assert mark_stale_ownership(days=180)["marked"] == 0

    def test_the_pass_clears_as_well_as_sets(self, graph):
        # Self-healing in both directions: a re-confirmed edge recovers on the
        # next run even if the write path's own clearing were bypassed.
        from app.scraper.maintenance import mark_stale_ownership
        from app.scraper.bulk_import import _now_iso

        _seed_edge(graph, "wd-owner", WD_CRED, seen=_now_iso(), stale=True)
        res = mark_stale_ownership(days=180)
        assert res["cleared"] == 1
        assert _edge(graph, "wd-owner")["stale"] is False

    def test_running_it_twice_changes_nothing(self, graph):
        from app.scraper.maintenance import mark_stale_ownership

        _seed_edge(graph, "wd-owner", WD_CRED, seen=OLD)
        mark_stale_ownership(days=180)
        res = mark_stale_ownership(days=180)
        assert res["marked"] == 0 and res["cleared"] == 0


def test_the_quality_report_counts_stale_edges(graph):
    from app.scraper.maintenance import mark_stale_ownership
    from app.quality import quality_report

    graph.run_command("CREATE (s:Source {id:'wd-src', name:'Wikidata'})")
    _seed_edge(graph, "wd-owner", WD_CRED, seen=OLD, source="wd-src")
    mark_stale_ownership(days=180)

    by = quality_report()["owns_by_source"]
    assert by["Wikidata"]["stale"] == 1
