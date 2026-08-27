"""
Writing a 13D voting group, against a real ArcadeDB.

The identity rule lives in Cypher and in list properties that a mocked session
would accept whatever they contained, so this is where it is actually checked:
one node per agreement across amendments, members linked without disturbing what
they already are, and the bloc kept out of the disclosed-percentage sum.
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def graph(it_db):
    it_db.run_command("CREATE (s:Source {id:'sec', name:'SEC EDGAR'})")
    it_db.run_command("CREATE (s:Source {id:'wd', name:'Wikidata'})")
    it_db.run_command(
        "CREATE (e:Entity {id:'abi', name:'Anheuser-Busch InBev', type:'company'})")
    # Already in the graph from Wikidata, with its own edge and classification.
    it_db.run_command(
        "CREATE (e:Entity {id:'stichting', name:'Stichting Anheuser-Busch InBev', "
        "type:'nonprofit', wikidata_id:'Q123745618', "
        "name_normalized:'stichting anheuser busch inbev'})")
    it_db.run_command(
        "MATCH (a {id:'stichting'}), (b {id:'abi'}) "
        "CREATE (a)-[:OWNS {source_id:'wd', stake_percent:null}]->(b)")
    return it_db


def _roster(*parties):
    from app.scraper.runner import _member_key
    return [_member_key(n, c) for n, c in parties]


ABI_ROSTER = (("BRC S.a R.L.", "1301486"), ("Stichting Anheuser-Busch InBev", None),
              ("Eugenie Patri Sebastien S.A.", None), ("Rayvax", None),
              ("Jorge Paulo Lemann", None))


class TestGroupNodeIdentity:
    def test_a_group_is_created_once(self, graph):
        from app.scraper.runner import _upsert_voting_group
        gid = _upsert_voting_group("abi", "Anheuser-Busch InBev", _roster(*ABI_ROSTER), "sec")
        rows = graph.run_command(
            "MATCH (g:Entity) WHERE g.type = 'voting_group' "
            "RETURN g.id AS id, g.name AS name, g.type AS type")
        assert len(rows) == 1 and rows[0]["id"] == gid
        # Named for what it is and how many are in it — the panel already says
        # whose shares they are, and naming it after the company read as though
        # the group were a subsidiary of it.
        assert rows[0]["name"] == "Voting group · 5 parties"

    def test_a_later_amendment_updates_rather_than_duplicates(self, graph):
        # The group's OWNS edge is what scopes the search to this subject, so it
        # has to exist before the second call can find the first node.
        from app.scraper.runner import _upsert_voting_group, _upsert_owns_sec
        first = _upsert_voting_group("abi", "Anheuser-Busch InBev", _roster(*ABI_ROSTER), "sec")
        _upsert_owns_sec(first, "abi", "sec", "unknown", "2025-01-01", None,
                         voting_power_pct=52.3)
        # Filed by a different member, one party gone, one added.
        later = ABI_ROSTER[1:] + (("Fonds Baillet Latour CV", None),)
        second = _upsert_voting_group("abi", "Anheuser-Busch InBev", _roster(*later), "sec")
        assert second == first, "a changed filer and roster forked the group"
        assert len(graph.run_command(
            "MATCH (g:Entity) WHERE g.type = 'voting_group' RETURN g.id AS id")) == 1

    def test_the_roster_is_replaced_by_the_newer_one(self, graph):
        from app.scraper.runner import _upsert_voting_group, _upsert_owns_sec
        gid = _upsert_voting_group("abi", "ABI", _roster(*ABI_ROSTER), "sec")
        _upsert_owns_sec(gid, "abi", "sec", "unknown", "2025-01-01", None,
                         voting_power_pct=52.3)
        _upsert_voting_group("abi", "ABI", _roster(*ABI_ROSTER[:4]), "sec")
        rows = graph.run_command("MATCH (g:Entity) WHERE g.type = 'voting_group' "
                                 "RETURN g.member_keys AS member_keys")
        assert len(rows[0]["member_keys"]) == 4

    def test_a_second_bloc_over_one_company_is_its_own_node(self, graph):
        # AB InBev genuinely has two: the families' pact and the Altria voting
        # agreement, overlapping only on the Stichting.
        from app.scraper.runner import _upsert_voting_group, _upsert_owns_sec
        first = _upsert_voting_group("abi", "Anheuser-Busch InBev", _roster(*ABI_ROSTER), "sec")
        _upsert_owns_sec(first, "abi", "sec", "unknown", "2025-01-01", None,
                         voting_power_pct=52.3)
        altria = _roster(("Altria Group, Inc.", "764180"), ("Bevco Lux S.a.r.l.", None),
                         ("Stichting Anheuser-Busch InBev", None))
        second = _upsert_voting_group("abi", "Anheuser-Busch InBev", altria,
                                      "sec")
        assert second != first
        names = sorted(r["name"] for r in graph.run_command(
            "MATCH (g:Entity) WHERE g.type = 'voting_group' RETURN g.name AS name"))
        assert names == ["Voting group · 3 parties", "Voting group · 5 parties"]

    def test_the_same_roster_over_another_company_is_another_group(self, graph):
        from app.scraper.runner import _upsert_voting_group, _upsert_owns_sec
        graph.run_command("CREATE (e:Entity {id:'other', name:'Other Co', type:'company'})")
        a = _upsert_voting_group("abi", "ABI", _roster(*ABI_ROSTER), "sec")
        _upsert_owns_sec(a, "abi", "sec", "unknown", "2025-01-01", None, voting_power_pct=52.3)
        b = _upsert_voting_group("other", "Other Co", _roster(*ABI_ROSTER), "sec")
        assert a != b


class TestMembershipLeavesMembersAlone:
    def test_an_existing_member_keeps_its_own_edges_and_type(self, graph):
        # The Stichting arrived from Wikidata as a nonprofit with its own OWNS
        # edge. Joining a group must add a link and change nothing else.
        from app.scraper.runner import _upsert_voting_group, _upsert_group_membership
        gid = _upsert_voting_group("abi", "ABI", _roster(*ABI_ROSTER), "sec")
        _upsert_group_membership("stichting", gid, "Entity", "sec")

        node = graph.run_command("MATCH (e:Entity {id:'stichting'}) "
                                 "RETURN e.type AS type, e.wikidata_id AS wikidata_id")[0]
        assert node["type"] == "nonprofit"
        assert node["wikidata_id"] == "Q123745618"
        owns = graph.run_command(
            "MATCH (a {id:'stichting'})-[r:OWNS]->(b {id:'abi'}) RETURN r.source_id AS s")
        assert [r["s"] for r in owns] == ["wd"], "the Wikidata edge was disturbed"

    def test_membership_is_not_ownership(self, graph):
        from app.scraper.runner import _upsert_voting_group, _upsert_group_membership
        gid = _upsert_voting_group("abi", "ABI", _roster(*ABI_ROSTER), "sec")
        _upsert_group_membership("stichting", gid, "Entity", "sec")
        rows = graph.run_command(
            "MATCH (a {id:'stichting'})-[r:RELATED_TO]->(g {id:$g}) RETURN r.relation AS rel",
            {"g": gid})
        assert [r["rel"] for r in rows] == ["group_member"]
        assert graph.run_command(
            "MATCH (a {id:'stichting'})-[r:OWNS]->(g {id:$g}) RETURN r", {"g": gid}) == []

    def test_joining_twice_makes_one_edge(self, graph):
        from app.scraper.runner import _upsert_voting_group, _upsert_group_membership
        gid = _upsert_voting_group("abi", "ABI", _roster(*ABI_ROSTER), "sec")
        for _ in range(2):
            _upsert_group_membership("stichting", gid, "Entity", "sec")
        rows = graph.run_command(
            "MATCH (a {id:'stichting'})-[r:RELATED_TO]->(g {id:$g}) RETURN r", {"g": gid})
        assert len(rows) == 1


class TestTheProfileShowsTheParties:
    """Members join by RELATED_TO, so nothing in the owners query can see them.
    Without this the group's panel listed nobody — the section was there and
    empty, which is how it shipped the first time."""

    def test_the_parties_come_back_on_the_profile(self, graph):
        from app.scraper.runner import (_upsert_voting_group, _upsert_group_membership,
                                        _upsert_person_by_name)
        from app.routers.search import get_full_profile
        gid = _upsert_voting_group("abi", "ABI", _roster(*ABI_ROSTER), "sec")
        _upsert_group_membership("stichting", gid, "Entity", "sec")
        pid = _upsert_person_by_name("Jorge Paulo Lemann", source_id="sec")
        _upsert_group_membership(pid, gid, "Person", "sec")

        parties = get_full_profile(gid)["group_members"]
        assert len(parties) == 2
        by_kind = {p["kind"]: (p["party"].get("name") or p["party"].get("full_name"))
                   for p in parties}
        assert by_kind["entity"] == "Stichting Anheuser-Busch InBev"
        assert by_kind["person"] == "Jorge Paulo Lemann"

    def test_an_ordinary_company_has_no_parties(self, graph):
        # The query costs a round trip, so it only runs for a group — and a
        # company is not one even if something points a membership edge at it.
        from app.routers.search import get_full_profile
        graph.run_command(
            "MATCH (m {id:'stichting'}), (c {id:'abi'}) "
            "CREATE (m)-[:RELATED_TO {relation:'group_member'}]->(c)")
        assert get_full_profile("abi")["group_members"] == []

    def test_only_membership_edges_count(self, graph):
        # RELATED_TO also carries 'affiliate', which _upsert_affiliate writes for
        # 13F fund groups. An affiliate is not a party to the agreement.
        from app.scraper.runner import _upsert_voting_group, _upsert_group_membership
        from app.routers.search import get_full_profile
        gid = _upsert_voting_group("abi", "ABI", _roster(*ABI_ROSTER), "sec")
        _upsert_group_membership("stichting", gid, "Entity", "sec")
        graph.run_command("CREATE (e:Entity {id:'aff', name:'Some Affiliate'})")
        graph.run_command(
            "MATCH (a {id:'aff'}), (g {id:$g}) "
            "CREATE (a)-[:RELATED_TO {relation:'affiliate'}]->(g)", {"g": gid})

        parties = get_full_profile(gid)["group_members"]
        assert [p["party"]["name"] for p in parties] == ["Stichting Anheuser-Busch InBev"]


def test_the_bloc_does_not_enter_the_disclosed_total(graph):
    # The reason the group's OWNS edge carries a null stake: its members hold the
    # shares individually, and adding a bloc percentage to theirs would put the
    # company over 100% of itself — the bug this whole line of work started from.
    from app.scraper.runner import _upsert_voting_group, _upsert_owns_sec
    from app.routers.search import get_full_profile

    gid = _upsert_voting_group("abi", "Anheuser-Busch InBev", _roster(*ABI_ROSTER), "sec")
    _upsert_owns_sec(gid, "abi", "sec", "unknown", "2025-02-07", None,
                     voting_power_pct=52.3)
    graph.run_command("CREATE (e:Entity {id:'altria', name:'Altria', type:'company'})")
    graph.run_command(
        "MATCH (a {id:'altria'}), (b {id:'abi'}) CREATE (a)-[:OWNS "
        "{source_id:'sec', stake_percent:8.1, voting_power_pct:51.9}]->(b)")

    prof = get_full_profile("abi")
    assert prof["ownership"]["disclosed_pct"] == 8.1
    assert prof["ownership"]["exceeds_100"] is False
    group_row = next(o for o in prof["owners"] if o["owner"]["id"] == gid)
    # ArcadeDB does not store a null property, so the key is absent rather than
    # present-and-None. `_ownership_summary` reads it with .get(), which is why
    # the bloc contributes nothing either way — but assert the absence plainly
    # rather than letting a future non-null value slip through.
    assert group_row["relationship"].get("stake_percent") is None
    assert group_row["relationship"]["voting_power_pct"] == 52.3


class TestTheWholeScrapeWritesTheGroup:
    """Everything above tests the writers directly. This drives the actual
    ownership-filings loop, which is where the decisions live: whether the bloc
    becomes a group at all, whether the filer still gets an edge of its own, and
    whether the group's edge carries a stake it must not."""

    FILING = {
        "investor_name": "BRC S.a R.L.", "investor_cik": "0001301486",
        "form_type": "SCHEDULE 13D/A", "file_date": "2026-05-15",
        "stake_percent": None, "voting_power_pct": 52.3,
        "ownership_type": "unknown", "is_individual": False,
        "share_class": "Ordinary Shares", "source_url": "https://sec.gov/x-index.htm",
        "group_members": [
            {"name": "Stichting Anheuser-Busch InBev", "cik": None, "source": "xml",
             "type_code": "CO"},
            {"name": "Eugenie Patri Sebastien S.A.", "cik": None, "source": "xml",
             "type_code": "CO"},
            {"name": "Jorge Paulo Lemann", "cik": None, "source": "xml",
             "type_code": "IN"},
        ],
    }

    def _scrape(self, graph, filing):
        from unittest.mock import patch
        from app.scraper import runner, sec_edgar
        payload = {"name": "Anheuser-Busch InBev", "cik": "0001668717",
                   "ownership_filings": [filing], "executives": [], "holdings": [],
                   "former_names": [], "lei": None}
        with patch.object(sec_edgar, "scrape_company", return_value=payload), \
             patch.object(sec_edgar, "fetch_filer_country", return_value=None), \
             patch.object(sec_edgar, "fetch_filer_headquarters", return_value=None), \
             patch.object(runner, "get_source_enabled", return_value=True), \
             patch.object(runner.settings, "SCRAPER_ENABLED", True), \
             patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
            return runner.run_scrape_sec_edgar("Anheuser-Busch InBev")

    def test_the_bloc_becomes_a_group_and_the_filer_becomes_a_member(self, graph):
        self._scrape(graph, self.FILING)

        groups = graph.run_command(
            "MATCH (g:Entity) WHERE g.type = 'voting_group' RETURN g.id AS id, g.name AS name")
        assert len(groups) == 1
        gid = groups[0]["id"]

        # Four parties: the filer plus its three co-filers.
        members = graph.run_command(
            "MATCH (m)-[r:RELATED_TO]->(g {id:$g}) WHERE r.relation = 'group_member' "
            "RETURN COALESCE(m.name, m.full_name) AS n", {"g": gid})
        assert len(members) == 4
        assert "BRC S.a R.L." in {m["n"] for m in members}

        # And BRC must NOT also hold the bloc itself — that was the original bug.
        direct = graph.run_command(
            "MATCH (a:Entity)-[r:OWNS]->(b {id:$t}) WHERE a.name = 'BRC S.a R.L.' RETURN r",
            {"t": graph.run_command("MATCH (e:Entity) WHERE e.name = 'Anheuser-Busch InBev' "
                                    "RETURN e.id AS id")[0]["id"]})
        assert direct == [], "the filer kept a direct edge as well as joining the group"

    def test_the_group_edge_carries_voting_but_never_a_stake(self, graph):
        # A bloc percentage written as a stake would sum with the members' own
        # holdings and put the company over 100% of itself.
        self._scrape(graph, self.FILING)
        rows = graph.run_command(
            "MATCH (g:Entity)-[r:OWNS]->(b:Entity) WHERE g.type = 'voting_group' "
            "RETURN r.stake_percent AS stake, r.voting_power_pct AS vote")
        assert len(rows) == 1
        assert rows[0]["stake"] is None
        assert rows[0]["vote"] == 52.3

    def test_an_individual_member_becomes_a_person(self, graph):
        # Item 8's "IN" code says so; nothing has to guess from the name.
        self._scrape(graph, self.FILING)
        people = graph.run_command(
            "MATCH (p:Person) WHERE p.full_name = 'Jorge Paulo Lemann' RETURN p.id AS id")
        assert len(people) == 1

    def test_the_filers_earlier_bloc_edge_is_retired(self, graph):
        # Graphs scraped before groups existed have the bloc written straight
        # onto the filer. That row is not merely stale — it is this same filing —
        # so the company would show its group AND BRC each voting 52.3%.
        graph.run_command("CREATE (e:Entity {id:'brc', name:'BRC S.a R.L.', "
                          "type:'company', name_normalized:'brc rl', sec_cik:'0001301486'})")
        graph.run_command("MATCH (a {id:'brc'}), (b {id:'abi'}) CREATE (a)-[:OWNS "
                          "{source_id:'sec', stake_percent:null, voting_power_pct:52.3}]->(b)")
        self._scrape(graph, self.FILING)
        assert graph.run_command(
            "MATCH (a {id:'brc'})-[r:OWNS]->(b {id:'abi'}) RETURN r") == []

    def test_a_members_own_holding_survives(self, graph):
        # Only the stakeless bloc row is retired. A member that also reports a
        # real holding keeps it — and the Stichting's Wikidata edge is untouched.
        graph.run_command("CREATE (e:Entity {id:'brc', name:'BRC S.a R.L.', "
                          "type:'company', name_normalized:'brc rl', sec_cik:'0001301486'})")
        graph.run_command("MATCH (a {id:'brc'}), (b {id:'abi'}) CREATE (a)-[:OWNS "
                          "{source_id:'sec', stake_percent:3.2}]->(b)")
        self._scrape(graph, self.FILING)
        kept = graph.run_command(
            "MATCH (a {id:'brc'})-[r:OWNS]->(b {id:'abi'}) RETURN r.stake_percent AS s")
        assert [r["s"] for r in kept] == [3.2]
        assert len(graph.run_command(
            "MATCH (a {id:'stichting'})-[r:OWNS]->(b {id:'abi'}) RETURN r")) == 1

    def test_an_organisation_is_not_mistaken_for_a_person(self, graph):
        # The reason the filing's own Item 8 code is used instead of a name
        # heuristic: `is_person_name` returns True for BOTH "Stichting
        # Anheuser-Busch InBev" and "Fonds Baillet Latour CV", which are a Dutch
        # foundation and a Belgian one. Guessing would mint a Person for each and
        # leave the Stichting's real Entity node — already here from Wikidata —
        # unconnected to the group it belongs to.
        self._scrape(graph, self.FILING)

        assert graph.run_command(
            "MATCH (p:Person) WHERE p.full_name = 'Stichting Anheuser-Busch InBev' "
            "RETURN p.id AS id") == [], "a foundation was created as a person"
        linked = graph.run_command(
            "MATCH (m:Entity {id:'stichting'})-[r:RELATED_TO]->(g:Entity) "
            "WHERE g.type = 'voting_group' RETURN r.relation AS rel")
        assert [r["rel"] for r in linked] == ["group_member"], \
            "the existing Stichting node was not the one joined to the group"

    def test_a_13g_bloc_makes_no_group(self, graph):
        # State Street sharing voting power across its own subsidiaries is not a
        # governance bloc, and modelling it as one would be misleading.
        passive = {**self.FILING, "form_type": "SCHEDULE 13G/A",
                   "investor_name": "State Street Corporation"}
        self._scrape(graph, passive)
        assert graph.run_command(
            "MATCH (g:Entity) WHERE g.type = 'voting_group' RETURN g.id AS id") == []
        # It keeps an ordinary edge of its own instead.
        assert graph.run_command(
            "MATCH (a:Entity)-[r:OWNS]->(b:Entity) WHERE a.name = 'State Street Corporation' "
            "RETURN r.voting_power_pct AS v")[0]["v"] == 52.3
