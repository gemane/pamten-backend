"""The labelled anchors return the same rows the unlabelled ones did.

The unit suite proves each query now names a label; this proves, against a real
ArcadeDB, that a labelled anchor still finds the node — a Person's voting group
through `(m:Person {id})`, a fund's 13F edge through `(a:Entity {id})` — and
that `node_label()` reads back the right label for either kind. The reason for
the labels is a full-import measurement (>400 s unlabelled, 10–70 ms labelled)
that no test database is big enough to repeat.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed_group(it_db):
    it_db.run_command("CREATE (:Entity {id:'issuer', name:'Issuer PLC'})")
    it_db.run_command("CREATE (:Person {id:'lemann', full_name:'Jorge Paulo Lemann', alias:[]})")
    it_db.run_command("CREATE (:Entity {id:'grp', name:'Voting group', type:'voting_group'})")
    it_db.run_command(
        "MATCH (p:Person {id:'lemann'}), (g:Entity {id:'grp'}) "
        "CREATE (p)-[:RELATED_TO {relation:'group_member', source_id:'sec'}]->(g)")
    it_db.run_command(
        "MATCH (e:Entity {id:'issuer'}), (g:Entity {id:'grp'}) "
        "CREATE (e)-[:RELATED_TO {relation:'group_member', source_id:'sec'}]->(g)")


class TestVotingGroupsThroughALabelledAnchor:
    def test_a_person_still_sees_the_bloc_it_votes_in(self, it_db):
        from app.database import db
        from app.routers.search import _voting_groups_of

        _seed_group(it_db)
        with db.get_session() as session:
            groups = _voting_groups_of(session, "lemann", set(), "Person")
        assert [g["group"]["id"] for g in groups] == ["grp"]

    def test_and_so_does_a_company(self, it_db):
        from app.database import db
        from app.routers.search import _voting_groups_of

        _seed_group(it_db)
        with db.get_session() as session:
            groups = _voting_groups_of(session, "issuer", set(), "Entity")
        assert [g["group"]["id"] for g in groups] == ["grp"]

    def test_the_wrong_label_finds_nothing_rather_than_scanning(self, it_db):
        """Which is why the caller must pass the right one — the profile knows."""
        from app.database import db
        from app.routers.search import _voting_groups_of

        _seed_group(it_db)
        with db.get_session() as session:
            assert _voting_groups_of(session, "lemann", set(), "Entity") == []


class TestNodeLabelAgainstTheRealCatalog:
    def test_it_tells_a_person_from_a_company(self, it_db):
        from app.database import db
        from app.db.anchors import node_label

        _seed_group(it_db)
        with db.get_session() as session:
            assert node_label("lemann", session) == "Person"
            assert node_label("issuer", session) == "Entity"
            assert node_label("nobody", session) is None
        assert node_label("lemann", query=it_db.run_query) == "Person"


class TestThe13FStaleMarkerWithLabelledFilers:
    def test_a_fund_and_a_person_both_go_stale(self, it_db):
        from app.scraper.sec_writer import mark_13f_stale

        it_db.run_command("CREATE (:Entity {id:'apple', name:'Apple Inc.'})")
        it_db.run_command("CREATE (:Entity {id:'vanguard', name:'Vanguard Group'})")
        it_db.run_command("CREATE (:Person {id:'cook', full_name:'Tim Cook', alias:[]})")
        for oid, label in (("vanguard", "Entity"), ("cook", "Person")):
            it_db.run_command(
                f"MATCH (a:{label} {{id:'{oid}'}}), (b:Entity {{id:'apple'}}) "
                "CREATE (a)-[:OWNS {filing_type:'13F', source_date:'2026-03-31', "
                "stake_percent:1.0}]->(b)")

        assert mark_13f_stale("apple", "2026-06-30") == 2

        rows = it_db.run_command(
            "MATCH (a)-[r:OWNS]->(b:Entity {id:'apple'}) RETURN a.id AS aid, r.stale AS stale")
        assert {r["aid"]: r["stale"] for r in rows} == {"vanguard": True, "cook": True}
