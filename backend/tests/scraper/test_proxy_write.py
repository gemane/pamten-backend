"""The proxy writer records a claim per owner and never renames anyone
(findings item 46). It SET voting power on the edge with no claim behind it,
renamed "Brin Sergey" to "Sergey Brin", and looked the company up as
`(c:Company)`, a type that does not exist. No database here."""
from unittest.mock import patch

import pytest

from app.claims import KIND_OWNS, claim_props, key_for
from app.scraper import proxy_write

PROXY = {"filing_date": "2026-04-24",
         "filing_url": "https://www.sec.gov/Archives/edgar/data/1652044/000130817926000342/goog-20260424.htm",
         "share_class_structure": "multi_class",
         "owners": [{"name": "Sergey Brin", "voting_power_pct": 25.3, "below_one_percent": False},
                    {"name": "Sundar Pichai", "voting_power_pct": None, "below_one_percent": True}]}


def _run(entity_id="goog", resolved=None, proxy=PROXY, edge=True):
    queries, commands, claims, fetched = [], [], [], []

    def run_query(cypher, params=None):
        queries.append((cypher, params))
        if "(c:Entity {id: $id})" in cypher:
            return [{"id": "goog", "name": "Alphabet Inc.", "sec_cik": "0001652044"}]
        return [{"oid": "sb", "olabel": "Person", "matched_name": "Brin Sergey",
                 "stake": 6.0, "ownership_type": "minority"}] if edge else []

    def fetch(company, cik=None):
        fetched.append(cik)
        return proxy
    with patch.object(proxy_write, "run_query", run_query), \
         patch.object(proxy_write, "run_command", lambda c, p=None: commands.append((c, p))), \
         patch("app.claims.record_claim", lambda **k: claims.append(k)), \
         patch("app.scraper.graph_writer._ensure_source", return_value="sec-src"), \
         patch("app.scraper.proxy_statement.fetch_proxy_ownership", side_effect=fetch), \
         patch("app.routers.search.resolve_best_entity", return_value=resolved):
        out = proxy_write.write_proxy_ownership("Alphabet Inc.", entity_id)
    return out, queries, commands, claims, fetched


def test_each_written_figure_has_a_claim_with_its_provenance():
    out, _, _, claims, _ = _run()
    assert claims == [{
        "kind": KIND_OWNS, "from_id": "sb", "to_id": "goog", "source_id": "sec-src",
        "voting_power_pct": 25.3, "source_url": PROXY["filing_url"], "source_date": "2026-04-24",
        "credibility_score": claims[0]["credibility_score"], "filing_type": "DEF 14A",
        "read_from": "table"}]
    assert claims[0]["credibility_score"] >= 90
    assert [u["proxy_name"] for u in out["updated"]] == ["Sergey Brin"]


def test_the_edge_gets_the_figure_and_nobody_is_renamed():
    _, _, commands, _, _ = _run()
    assert len(commands) == 1
    cypher, params = commands[0]
    assert "SET r.voting_power_pct = $pct" in cypher and params["pct"] == 25.3
    assert "full_name" not in cypher


def test_below_one_percent_writes_nothing():
    out, _, commands, claims, _ = _run()
    assert out["skipped_no_pct"] == ["Sundar Pichai"]
    assert all(c["voting_power_pct"] == 25.3 for c in claims) and len(commands) == 1


def test_an_owner_without_an_edge_is_reported_not_created():
    out, _, commands, claims, _ = _run(edge=False)
    assert out["not_found_in_db"] == ["Sergey Brin"] and commands == [] and claims == []


def test_the_graphs_cik_reaches_the_reader():
    *_, fetched = _run()
    assert fetched == ["0001652044"]


def test_without_an_entity_id_the_shared_resolver_finds_the_company():
    out, queries, _, _, fetched = _run(entity_id=None,
                                       resolved={"id": "goog", "name": "Alphabet Inc.", "sec_cik": "1652044"})
    assert out["entity_id"] == "goog" and fetched == ["1652044"]
    assert not any(":Company" in q for q, _ in queries)


def test_a_company_not_in_the_graph_is_not_read_at_all():
    out, _, _, _, fetched = _run(entity_id=None, resolved=None)
    assert out["db_error"].startswith("Company not found in DB") and fetched == []


def test_a_failed_read_returns_the_error_with_the_company():
    out, _, commands, claims, _ = _run(proxy={"error": "Failed to fetch filing: 503", "owners": []})
    assert out["error"].startswith("Failed") and out["entity_id"] == "goog"
    assert commands == [] and claims == []


@pytest.mark.parametrize("other", ["13D/A", "Form 4", "SC 13G", None])
def test_the_proxy_claim_never_shares_a_key_with_the_pairs_other_claims(other):
    mine = claim_props(kind=KIND_OWNS, from_id="sb", to_id="goog", source_id="s", filing_type="DEF 14A")
    theirs = claim_props(kind=KIND_OWNS, from_id="sb", to_id="goog", source_id="s", filing_type=other)
    assert mine["claim_key"] != theirs["claim_key"]
    assert key_for(mine) == mine["claim_key"]       # a heal re-keys it to itself
