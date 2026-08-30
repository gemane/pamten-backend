"""
The edge schema, against a real ArcadeDB.

The merge paths RECREATE edges from a property list, and hand-written lists
fell behind four separate times — 6 of 25 properties in the worst block. The
lists now come from `edge_schema`, and these tests are parameterised over it:
a property added to the schema is asserted to survive a merge the same day,
with no test edit.
"""
import pytest

from app.scraper.edge_schema import OWNS_PROPS, ROLE_PROPS, RELATED_TO_PROPS

pytestmark = pytest.mark.integration


#: A distinct, type-plausible value per property, so a survived merge can be
#: checked field by field and a crossed wire (one property written into
#: another's slot) cannot cancel out.
_SAMPLE = {
    "stake_percent": 8.05, "voting_power_pct": 51.9, "ownership_type": "minority",
    "since": "2016-10-10", "until": None, "until_reason": None,
    "source_id": "src-sec", "credibility_score": 98,
    "source_url": "https://sec.example.test/f1", "source_date": "2025-02-07",
    # ArcadeDB normalises timestamps on write ('T00:00:00Z' becomes 'T00:00Z'),
    # so the sample uses the normalised form — otherwise every comparison fails
    # against a value that DID survive. See the arcadedb gotchas memory.
    "last_scraped_at": "2026-08-28T00:00Z",
    "interest_types": ["ownership-of-shares-25-to-50-percent"],
    "direct_or_indirect": "direct", "psc_self_link": "/company/1/psc/2",
    "share_class": "Ordinary Shares", "shares": 159121937,
    "shares_outstanding": 1975847422, "voting_shares": 1020598157,
    "stale": False, "shortcut": False, "also_ultimate": True,
    "ultimate_since": "2017-01-01", "ultimate_until": None,
    "value_usd": 12345678.9, "file_date": "2025-02-07",
    "filing_type": "13G/A",
    "role": "CEO", "relation": "group_member",
}


@pytest.fixture
def graph(it_db):
    it_db.run_command("CREATE (e:Entity {id:'dead', name:'Old Node'})")
    it_db.run_command("CREATE (e:Entity {id:'keep', name:'Survivor'})")
    it_db.run_command("CREATE (e:Entity {id:'target', name:'Held Co'})")
    return it_db


def _create_edge(it_db, kind: str, props: tuple, frm="dead", to="target",
                 from_label="Entity"):
    clause = ", ".join(f"{p}: ${p}" for p in props)
    it_db.run_command(
        f"MATCH (a:{from_label} {{id:'{frm}'}}), (b:Entity {{id:'{to}'}}) "
        f"CREATE (a)-[:{kind} {{{clause}}}]->(b)",
        {p: _SAMPLE[p] for p in props})


class TestEveryPropertySurvivesAnEntityMerge:
    @pytest.mark.parametrize("prop", OWNS_PROPS)
    def test_owns(self, graph, prop):
        from app.scraper.maintenance import _migrate_entity_edges
        _create_edge(graph, "OWNS", OWNS_PROPS)
        _migrate_entity_edges("dead", "keep")
        r = graph.run_command(
            f"MATCH (a {{id:'keep'}})-[r:OWNS]->(b {{id:'target'}}) "
            f"RETURN r.{prop} AS v")[0]
        assert r.get("v") == _SAMPLE[prop], f"{prop} did not survive the merge"

    @pytest.mark.parametrize("prop", ROLE_PROPS)
    def test_role(self, graph, prop):
        from app.scraper.maintenance import _migrate_entity_edges
        graph.run_command("CREATE (p:Person {id:'boss', full_name:'A Boss'})")
        _create_edge(graph, "HAS_ROLE", ROLE_PROPS, frm="boss", to="dead",
                     from_label="Person")
        _migrate_entity_edges("dead", "keep")
        r = graph.run_command(
            f"MATCH (p {{id:'boss'}})-[r:HAS_ROLE]->(b {{id:'keep'}}) "
            f"RETURN r.{prop} AS v")[0]
        assert r.get("v") == _SAMPLE[prop]

    @pytest.mark.parametrize("prop", RELATED_TO_PROPS)
    def test_related_to(self, graph, prop):
        from app.scraper.maintenance import _migrate_entity_edges
        _create_edge(graph, "RELATED_TO", RELATED_TO_PROPS)
        _migrate_entity_edges("dead", "keep")
        r = graph.run_command(
            f"MATCH (a {{id:'keep'}})-[r:RELATED_TO]->(b {{id:'target'}}) "
            f"RETURN r.{prop} AS v")[0]
        assert r.get("v") == _SAMPLE[prop]


class TestEveryPropertySurvivesAPersonMerge:
    """The block the entity path's docstring did not know about — it named 6 of
    25 properties and runs on every auto-dedup."""

    @pytest.mark.parametrize("prop", OWNS_PROPS)
    def test_owns(self, graph, prop):
        from app.scraper.maintenance import _migrate_person_edges
        graph.run_command("CREATE (p:Person {id:'p-dead', full_name:'Dupe'})")
        graph.run_command("CREATE (p:Person {id:'p-keep', full_name:'Real'})")
        _create_edge(graph, "OWNS", OWNS_PROPS, frm="p-dead", from_label="Person")
        _migrate_person_edges("p-dead", "p-keep")
        r = graph.run_command(
            f"MATCH (p {{id:'p-keep'}})-[r:OWNS]->(b {{id:'target'}}) "
            f"RETURN r.{prop} AS v")[0]
        assert r.get("v") == _SAMPLE[prop], f"{prop} stripped by the person merge"

    def test_a_person_keeps_group_membership(self, graph):
        # Lemann, Sicupira and Telles are all Person group members; a person
        # merge used to sever them from their bloc.
        from app.scraper.maintenance import _migrate_person_edges
        graph.run_command("CREATE (p:Person {id:'p-dead', full_name:'Dupe'})")
        graph.run_command("CREATE (p:Person {id:'p-keep', full_name:'Real'})")
        _create_edge(graph, "RELATED_TO", RELATED_TO_PROPS, frm="p-dead",
                     from_label="Person")
        _migrate_person_edges("p-dead", "p-keep")
        rows = graph.run_command(
            "MATCH (p {id:'p-keep'})-[r:RELATED_TO]->(b {id:'target'}) "
            "RETURN r.relation AS rel")
        assert [r["rel"] for r in rows] == ["group_member"]


class TestClaimsFollowTheSurvivor:
    def test_a_merged_nodes_claims_are_rekeyed(self, graph):
        # A claim's key hashes its endpoints, so this cannot be an UPDATE; the
        # old behaviour left every claim pointing at the dead id, and the
        # merged company showed as uncorroborated however many sources agreed.
        from app.claims import migrate_claims, claim_key
        graph.run_command(
            "CREATE (c:Claim {claim_key:$k, kind:'owns', from_id:'dead', "
            "to_id:'target', source_id:'src-sec', stake_percent:8.05})",
            {"k": claim_key("owns", "dead", "target", "src-sec")})

        moved = migrate_claims("dead", "keep")
        assert moved == 1
        rows = graph.run_command("MATCH (c:Claim) RETURN c.from_id AS f, "
                                 "c.claim_key AS k, c.stake_percent AS st")
        assert len(rows) == 1
        assert rows[0]["f"] == "keep"
        assert rows[0]["k"] == claim_key("owns", "keep", "target", "src-sec")
        assert rows[0]["st"] == 8.05

    def test_an_existing_claim_on_the_survivor_wins(self, graph):
        # Same (kind, pair, source) on both nodes: they describe one assertion,
        # so the merge must end with one claim, not a duplicate pair.
        from app.claims import migrate_claims, claim_key
        for node in ("dead", "keep"):
            graph.run_command(
                "CREATE (c:Claim {claim_key:$k, kind:'owns', from_id:$f, "
                "to_id:'target', source_id:'src-sec'})",
                {"k": claim_key("owns", node, "target", "src-sec"), "f": node})
        migrate_claims("dead", "keep")
        rows = graph.run_command("MATCH (c:Claim) RETURN c.from_id AS f")
        assert [r["f"] for r in rows] == ["keep"]

    def test_the_merge_paths_actually_call_it(self, graph):
        # Direct calls prove migrate_claims works; this proves the merges USE
        # it — the wiring a refactor can silently drop.
        from app.claims import claim_key
        from app.scraper.maintenance import _migrate_entity_edges, _migrate_person_edges
        graph.run_command(
            "CREATE (c:Claim {claim_key:$k, kind:'owns', from_id:'dead', "
            "to_id:'target', source_id:'src-sec'})",
            {"k": claim_key("owns", "dead", "target", "src-sec")})
        _migrate_entity_edges("dead", "keep")
        assert graph.run_command(
            "MATCH (c:Claim) WHERE c.from_id = 'dead' RETURN c") == [], \
            "the entity merge left claims on the dead node"

        graph.run_command("CREATE (p:Person {id:'p-dead', full_name:'D'})")
        graph.run_command("CREATE (p:Person {id:'p-keep', full_name:'K'})")
        graph.run_command(
            "CREATE (c:Claim {claim_key:$k, kind:'owns', from_id:'p-dead', "
            "to_id:'target', source_id:'src-sec'})",
            {"k": claim_key("owns", "p-dead", "target", "src-sec")})
        _migrate_person_edges("p-dead", "p-keep")
        assert graph.run_command(
            "MATCH (c:Claim) WHERE c.from_id = 'p-dead' RETURN c") == [], \
            "the person merge left claims on the dead node"

    def test_corroboration_is_visible_after_a_merge(self, graph):
        # The user-facing consequence: the profile's corroboration count must
        # not reset to zero because the owner was deduplicated.
        from app.claims import migrate_claims, claim_key
        from app.routers.search import _corroborations_for
        graph.run_command("CREATE (s:Source {id:'src-sec', name:'SEC EDGAR'})")
        graph.run_command("CREATE (s:Source {id:'src-wd', name:'Wikidata'})")
        for src in ("src-sec", "src-wd"):
            graph.run_command(
                "CREATE (c:Claim {claim_key:$k, kind:'owns', from_id:'dead', "
                "to_id:'target', source_id:$s})",
                {"k": claim_key("owns", "dead", "target", src), "s": src})
        migrate_claims("dead", "keep")
        assert sorted(_corroborations_for("target")[("keep", "target", "owns")]) == \
            ["SEC EDGAR", "Wikidata"]


def test_a_reconfirmed_sec_edge_recovers_from_stale(graph):
    # `_upsert_owns_sec` refreshed the edge but never cleared `stale`; its
    # Wikidata sibling does, with a comment explaining why. A re-confirmed
    # register edge stayed dimmed forever.
    from app.scraper.runner import _upsert_owns_sec
    graph.run_command(
        "MATCH (a {id:'dead'}), (b {id:'target'}) CREATE (a)-[:OWNS "
        "{source_id:'src-sec', stale:true, stake_percent:5.0}]->(b)")
    _upsert_owns_sec("dead", "target", "src-sec", "minority", "2026-08-01", 5.1)
    r = graph.run_command(
        "MATCH (a {id:'dead'})-[r:OWNS]->(b {id:'target'}) RETURN r.stale AS s")[0]
    assert r["s"] is False
