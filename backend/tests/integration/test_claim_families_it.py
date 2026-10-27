"""Real-ArcadeDB: SEC EDGAR's filing families are separate claims for one pair
(2026-10-26) — the Sources panel shows each, an amendment updates its own, the
Exhibit 21 withdrawal takes only the list's claim, and the key heal re-keys a
database written before.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _claims(it_db):
    return {r["filing_type"]: r for r in it_db.run_sql(
        "SELECT filing_type, stake_percent, claim_key, source_url FROM Claim WHERE kind = 'owns'")}


@pytest.fixture
def graph(it_db):
    it_db.run_command("CREATE (:Source {id:'sec-edgar', name:'SEC EDGAR', url:'https://www.sec.gov', credibility_score:98})")
    it_db.run_command("CREATE (:Entity {id:'musk', name:'Elon Musk Holdings', name_normalized:'elon musk holdings', type:'company'})")
    it_db.run_command("CREATE (:Entity {id:'tsla', name:'Tesla', name_normalized:'tesla', type:'company'})")
    return it_db


def test_a_13g_and_a_form_4_about_one_pair_are_two_claims_and_two_panel_lines(graph):
    from app.scraper.sec_writer import _upsert_owns_sec
    from app.routers.sources import get_sources_for_entity
    _upsert_owns_sec("musk", "tsla", "sec-edgar", "minority", "2026-01-10", 19.9,
                     filing_type="Form 4", source_url="https://sec.example.test/form4", read_from="field")
    _upsert_owns_sec("musk", "tsla", "sec-edgar", "minority", "2026-02-01", 28.4,
                     filing_type="13G/A", source_url="https://sec.example.test/13ga", read_from="form")
    c = _claims(graph)
    assert set(c) == {"Form 4", "13G/A"} and c["Form 4"]["stake_percent"] == 19.9 and c["13G/A"]["stake_percent"] == 28.4
    # the register's two statements, side by side on the panel
    lines = sorted((s["filing_type"], s["url"]) for s in get_sources_for_entity("tsla") if s["id"] == "sec-edgar")
    assert lines == [("13G/A", "https://sec.example.test/13ga"), ("Form 4", "https://sec.example.test/form4")]
    # an amendment of the same family updates its own claim, never a third row
    _upsert_owns_sec("musk", "tsla", "sec-edgar", "minority", "2026-03-01", 27.0,
                     filing_type="13G/A", source_url="https://sec.example.test/13ga-2", read_from="form")
    c = _claims(graph)
    assert set(c) == {"Form 4", "13G/A"} and c["13G/A"]["stake_percent"] == 27.0
    assert c["Form 4"]["stake_percent"] == 19.9                       # untouched by the other family


def test_the_exhibit_21_withdrawal_takes_only_the_lists_claim(graph):
    from app.scraper.sec_writer import _upsert_owns_sec, detach_owns_sec
    graph.run_command("CREATE (:Entity {id:'sub', name:'Sub Inc', name_normalized:'sub inc', type:'company'})")
    _upsert_owns_sec("tsla", "sub", "sec-edgar", "controlling", "2025-12-31", 100.0,
                     filing_type="EX-21", source_url="https://sec.example.test/ex21", read_from="table")
    _upsert_owns_sec("tsla", "sub", "sec-edgar", "majority", "2026-01-05", 60.0,
                     filing_type="13D", source_url="https://sec.example.test/13d", read_from="form")
    assert detach_owns_sec("tsla", "sub", "sec-edgar", "EX-21") == "reassigned"
    c = _claims(graph)
    assert set(c) == {"13D"}
    edge = graph.run_command("MATCH (:Entity {id:'tsla'})-[r:OWNS]->(:Entity {id:'sub'}) RETURN r.stake_percent AS s, r.filing_type AS ft")
    assert [(e["s"], e["ft"]) for e in edge] == [(60.0, "13D")]        # re-answered from the 13D
