"""
The quality report, against a real ArcadeDB.

The report is the instrument the source-mix strategy is steered by, so its
numbers have to be right in the ways that matter: a corroborated relationship
counted once, a contradiction actually detected, a source's edges attributed to
that source. Every figure here is aggregation over real rows — exactly the kind
of code a mocked session lets lie.
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def graph(it_db):
    """Two sources, three companies, a corroborated edge and a lone one."""
    it_db.run_command("CREATE (s:Source {id:'src-sec', name:'SEC EDGAR'})")
    it_db.run_command("CREATE (s:Source {id:'src-wd', name:'Wikidata'})")
    for cid, name in (("co-a", "Alpha"), ("co-b", "Beta"), ("co-c", "Gamma")):
        it_db.run_command("CREATE (e:Entity {id:$id, name:$n})", {"id": cid, "n": name})
    # A: SEC edge with a stake, corroborated by a Wikidata claim.
    it_db.run_command(
        "MATCH (a {id:'co-a'}), (b {id:'co-b'}) CREATE (a)-[:OWNS {stake_percent:7.5, "
        "ownership_type:'minority', source_id:'src-sec', "
        "source_url:'https://www.sec.gov/f/1', last_scraped_at:'2026-08-01T00:00:00Z'}]->(b)")
    for src in ("src-sec", "src-wd"):
        it_db.run_command(
            "CREATE (c:Claim {claim_key:$k, kind:'owns', from_id:'co-a', to_id:'co-b', "
            "source_id:$s})", {"k": f"owns|co-a|co-b|{src}", "s": src})
    # B: a Wikidata-only edge, stakeless, with only its own claim.
    it_db.run_command(
        "MATCH (a {id:'co-a'}), (b {id:'co-c'}) CREATE (a)-[:OWNS {stake_percent:null, "
        "ownership_type:'unknown', source_id:'src-wd', "
        "source_url:'https://www.wikidata.org/wiki/Q1', "
        "last_scraped_at:'2020-01-01T00:00:00Z'}]->(b)")
    it_db.run_command(
        "CREATE (c:Claim {claim_key:'owns|co-a|co-c|src-wd', kind:'owns', "
        "from_id:'co-a', to_id:'co-c', source_id:'src-wd'})")
    return it_db


class TestTheFiguresAreRight:
    def test_edges_land_under_their_own_source(self, graph):
        from app.quality import quality_report
        by = quality_report()["owns_by_source"]
        assert by["SEC EDGAR"]["edges"] == 1 and by["SEC EDGAR"]["with_stake"] == 1
        assert by["Wikidata"]["edges"] == 1 and by["Wikidata"]["with_stake"] == 0

    def test_freshness_is_windowed(self, graph):
        # The SEC edge was confirmed this month; the Wikidata one in 2020. If the
        # windows did not discriminate, staleness would be invisible — which is
        # the state the graph was in before the report existed.
        from app.quality import quality_report
        by = quality_report()["owns_by_source"]
        assert by["SEC EDGAR"]["confirmed_365d"] == 1
        assert by["Wikidata"]["confirmed_365d"] == 0

    def test_a_two_source_relationship_counts_as_corroborated_once(self, graph):
        from app.quality import quality_report
        c = quality_report()["corroboration"]
        assert c["relationships_with_claims"] == 2
        assert c["corroborated"] == 1
        assert c["by_source_count"] == {1: 1, 2: 1}

    def test_direction_matters_to_corroboration(self, graph):
        # A claim in the OPPOSITE direction is a different assertion — "B owns A"
        # corroborates nothing about "A owns B".
        #
        # The reverse claim comes from a source NOT already asserting the forward
        # direction, on the UNcorroborated pair: a direction-blind grouping then
        # merges them into a false corroboration (2), while the correct one keeps
        # them apart (1). The first version reversed a source that already
        # asserted forward, which no grouping could tell apart — the mutant
        # survived and the harness, comparing summary strings with their
        # runtimes in them, reported it killed anyway.
        graph.run_command(
            "CREATE (c:Claim {claim_key:'owns|co-c|co-a|src-sec', kind:'owns', "
            "from_id:'co-c', to_id:'co-a', source_id:'src-sec'})")
        from app.quality import quality_report
        c = quality_report()["corroboration"]
        assert c["corroborated"] == 1, "a reverse-direction claim was merged in"
        assert c["relationships_with_claims"] == 3

    def test_kind_matters_to_corroboration(self, graph):
        # A role claim about the same pair is not ownership corroboration.
        graph.run_command(
            "CREATE (c:Claim {claim_key:'role|co-a|co-c|src-sec', kind:'role', "
            "from_id:'co-a', to_id:'co-c', source_id:'src-sec'})")
        from app.quality import quality_report
        assert quality_report()["corroboration"]["corroborated"] == 1


class TestTheContradictionGauges:
    """Each of these was a live bug this week. The gauge exists so a regression is
    an alarm on a report someone reads, rather than a UI oddity someone may not."""

    def test_all_zero_on_a_healthy_graph(self, graph):
        from app.quality import quality_report
        assert quality_report()["contradictions"] == {
            "stake_with_unknown_type": 0, "self_owning_edges": 0,
            "provenance_mismatches": 0}

    def test_a_stake_beside_unknown_is_counted(self, graph):
        graph.run_command(
            "MATCH (a {id:'co-b'}), (b {id:'co-c'}) CREATE (a)-[:OWNS "
            "{stake_percent:6.12, ownership_type:'unknown', source_id:'src-wd'}]->(b)")
        from app.quality import quality_report
        assert quality_report()["contradictions"]["stake_with_unknown_type"] == 1

    def test_a_self_loop_is_counted(self, graph):
        graph.run_command(
            "MATCH (a {id:'co-b'}) CREATE (a)-[:OWNS {source_id:'src-sec'}]->(a)")
        from app.quality import quality_report
        assert quality_report()["contradictions"]["self_owning_edges"] == 1

    def test_a_wrong_hosted_link_is_counted(self, graph):
        # The #261 shape: attributed to SEC, linking to Wikidata.
        graph.run_command(
            "MATCH (a {id:'co-b'}), (b {id:'co-c'}) CREATE (a)-[:OWNS "
            "{source_id:'src-sec', source_url:'https://www.wikidata.org/wiki/Q9'}]->(b)")
        from app.quality import quality_report
        assert quality_report()["contradictions"]["provenance_mismatches"] == 1

    def test_a_missing_link_is_not_a_mismatch(self, graph):
        # After #261's repair some edges legitimately carry no URL at all — the
        # honest state, not a contradiction.
        graph.run_command(
            "MATCH (a {id:'co-b'}), (b {id:'co-c'}) CREATE (a)-[:OWNS "
            "{source_id:'src-sec', source_url:null}]->(b)")
        from app.quality import quality_report
        assert quality_report()["contradictions"]["provenance_mismatches"] == 0


class TestIdentity:
    def test_official_wikidata_only_and_bare_are_disjoint(self, graph):
        graph.run_command("MATCH (e {id:'co-a'}) SET e.lei_id = 'L1'")
        graph.run_command("MATCH (e {id:'co-b'}) SET e.wikidata_id = 'Q2'")
        # co-c keeps no id at all.
        from app.quality import quality_report
        i = quality_report()["identity"]
        assert i["entities"] == 3
        assert i["with_official_id"] == 1
        assert i["wikidata_only"] == 1
        assert i["no_id_at_all"] == 1

    def test_a_voting_group_is_not_counted_at_all(self, graph):
        # A filing group is an agreement between parties, not an organisation:
        # it can never hold an LEI, so counting it would make the graph look
        # permanently less register-identified than it is.
        graph.run_command("MATCH (e {id:'co-a'}) SET e.lei_id = 'L1'")
        graph.run_command("CREATE (g:Entity {id:'grp', name:'Voting group — Held Co', "
                          "type:'voting_group'})")
        from app.quality import quality_report
        i = quality_report()["identity"]
        assert i["entities"] == 3, "the group was counted among the entities"
        assert i["no_id_at_all"] == 2

    def test_an_entity_with_both_counts_as_official(self, graph):
        # Wikidata-only means ONLY — a register-identified company that also has a
        # QID is register-backed, and counting it as community would overstate the
        # dependence the strategy is trying to measure.
        graph.run_command("MATCH (e {id:'co-a'}) SET e.lei_id = 'L1', e.wikidata_id = 'Q1'")
        from app.quality import quality_report
        i = quality_report()["identity"]
        assert i["with_official_id"] == 1
        assert i["wikidata_only"] == 0


def test_the_terminal_rendering_carries_the_same_numbers(graph):
    from app.quality import format_report, quality_report
    out = format_report(quality_report())
    assert "SEC EDGAR" in out and "Wikidata" in out
    assert "1 of 2 claimed relationships" in out
    assert "REGRESSION" not in out, "a healthy graph must not raise the alarm"


def test_the_alarm_fires_on_any_contradiction(graph):
    graph.run_command("MATCH (a {id:'co-b'}) CREATE (a)-[:OWNS {source_id:'src-sec'}]->(a)")
    from app.quality import format_report, quality_report
    assert "REGRESSION" in format_report(quality_report())


class TestTheProfileCarriesCorroboration:
    """Phase 1 of the quality strategy: the Claim data, surfaced per relationship
    on the profiles the panel actually renders. `corroborations` is the count of
    distinct sources; `asserted_by` names them."""

    @pytest.fixture
    def profiled(self, graph):
        graph.run_command("MATCH (e {id:'co-a'}) SET e.name_normalized = 'alpha'")
        return graph

    def test_a_two_source_edge_says_so(self, profiled):
        from app.routers.search import get_full_profile
        prof = get_full_profile("co-b")
        owner = next(o for o in prof["owners"] if o["owner"]["id"] == "co-a")
        assert owner["relationship"]["corroborations"] == 2
        assert owner["relationship"]["asserted_by"] == ["SEC EDGAR", "Wikidata"]

    def test_a_lone_source_edge_says_that_too(self, profiled):
        from app.routers.search import get_full_profile
        prof = get_full_profile("co-c")
        owner = next(o for o in prof["owners"] if o["owner"]["id"] == "co-a")
        assert owner["relationship"]["corroborations"] == 1
        assert owner["relationship"]["asserted_by"] == ["Wikidata"]

    def test_the_direction_is_the_subsidiary_view_too(self, profiled):
        # The same edge seen from the owner's side: co-a's subsidiaries list.
        from app.routers.search import get_full_profile
        prof = get_full_profile("co-a")
        sub = next(x for x in prof["subsidiaries"] if x["entity"]["id"] == "co-b")
        assert sub["relationship"]["corroborations"] == 2

    def test_an_edge_with_no_claims_reports_zero_not_nothing(self, profiled):
        # Edges predate the claims table; absent and unknown are different, and
        # the UI must not have to guess which it is looking at.
        profiled.run_command("CREATE (e:Entity {id:'co-d', name:'Delta'})")
        profiled.run_command(
            "MATCH (a {id:'co-d'}), (b {id:'co-b'}) CREATE (a)-[:OWNS "
            "{source_id:'src-sec'}]->(b)")
        from app.routers.search import get_full_profile
        prof = get_full_profile("co-b")
        owner = next(o for o in prof["owners"] if o["owner"]["id"] == "co-d")
        assert owner["relationship"]["corroborations"] == 0
        assert owner["relationship"]["asserted_by"] == []

    def test_a_role_is_corroborated_by_role_claims_not_ownership_ones(self, profiled):
        # The executives loop passes kind='role'. Passing 'owns' there would read
        # the wrong bucket — and with no executive in the fixture, nothing could
        # ever notice. This is the executive.
        profiled.run_command("CREATE (p:Person {id:'per-1', full_name:'Ann Chief'})")
        profiled.run_command(
            "MATCH (p:Person {id:'per-1'}), (c {id:'co-b'}) "
            "CREATE (p)-[:HAS_ROLE {role:'CEO', source_id:'src-sec'}]->(c)")
        for src in ("src-sec", "src-wd"):
            profiled.run_command(
                "CREATE (c:Claim {claim_key:$k, kind:'role', from_id:'per-1', "
                "to_id:'co-b', source_id:$s})",
                {"k": f"role|per-1|co-b|{src}", "s": src})

        from app.routers.search import get_full_profile
        prof = get_full_profile("co-b")
        ex = next(e for e in prof["executives"] if e["person"]["id"] == "per-1")
        assert ex["role"]["corroborations"] == 2
        assert ex["role"]["asserted_by"] == ["SEC EDGAR", "Wikidata"]

    def test_the_same_source_twice_is_one_corroboration(self, profiled):
        # Two claims from one source (a re-scrape under a differently-keyed claim)
        # must not read as independent confirmation.
        profiled.run_command(
            "CREATE (c:Claim {claim_key:'owns|co-a|co-c|src-wd|dup', kind:'owns', "
            "from_id:'co-a', to_id:'co-c', source_id:'src-wd'})")
        from app.routers.search import get_full_profile
        prof = get_full_profile("co-c")
        owner = next(o for o in prof["owners"] if o["owner"]["id"] == "co-a")
        assert owner["relationship"]["corroborations"] == 1
