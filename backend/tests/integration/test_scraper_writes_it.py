"""
Real-ArcadeDB tests for the scraper's entity writes.

Two production bugs motivated this file, and the mocked suite passed through both
because a fake session never parses the Cypher it is handed:

1. A `--` comment inside the SET block of `_upsert_entity` broke **every** Wikidata
   entity write — "Syntax error at line 3:21 - no viable alternative at input
   '--'". ArcadeDB's Cypher has no `--` comment.
2. The SEC name fallback matched on a bare string prefix, so scraping "Alphabet"
   resolved onto a French company called "ALPHA" and stamped Alphabet Inc's CIK
   and its BlackRock / Vanguard / Fidelity holders onto it.
"""
import pytest

pytestmark = pytest.mark.integration


# ── The Cypher actually parses ────────────────────────────────────────────────

def test_wikidata_upsert_creates_and_updates_for_real(it_db):
    from app.scraper.runner import _upsert_entity

    eid = _upsert_entity("Alphabet Inc.", "company", "US", 2015, None,
                         "Holding company", "Q20800404",
                         lei="5493006MHB84DD0ZWV18", sec_cik="0001652044")
    rows = it_db.run_sql(f"SELECT FROM Entity WHERE id = '{eid}'")
    assert rows, "create path did not write a node"
    node = {k: v for k, v in rows[0].items() if not k.startswith("@")}
    assert node["wikidata_id"] == "Q20800404"
    assert node["lei_id"] == "5493006MHB84DD0ZWV18"
    assert node["sec_cik"] == "0001652044"

    # Second call takes the UPDATE branch — the one the bad comment sat in.
    again = _upsert_entity("Alphabet Inc.", "company", "US", 2015, 1.0,
                           "Holding company", "Q20800404")
    assert again == eid
    rows = it_db.run_sql(f"SELECT FROM Entity WHERE id = '{eid}'")
    node = {k: v for k, v in rows[0].items() if not k.startswith("@")}
    assert node["lei_id"] == "5493006MHB84DD0ZWV18", "update dropped the identifier"


def test_wikidata_upsert_does_not_overwrite_an_existing_identifier(it_db):
    from app.scraper.runner import _upsert_entity

    it_db.run_command(
        "CREATE (e:Entity {id:'lei:REG', name:'Registered Co', name_normalized:'registered co', "
        "type:'company', lei_id:'REGISTEREDLEI0000001', name_credibility:92})")
    _upsert_entity("Registered Co", "company", "US", None, None, None, "Q1",
                   lei="WIKIDATASAYSOTHER001")
    rows = it_db.run_sql("SELECT lei_id FROM Entity WHERE id = 'lei:REG'")
    assert rows[0]["lei_id"] == "REGISTEREDLEI0000001", "a crowd-edited LEI overwrote the register's"


# ── SEC must not adopt a company whose name is merely a prefix ────────────────

def test_sec_does_not_attach_to_a_prefix_named_company(it_db):
    from app.scraper.runner import _upsert_entity_by_name

    it_db.run_command(
        "CREATE (e:Entity {id:'lei:FR', name:'ALPHA', name_normalized:'alpha', "
        "type:'company', country:'FR', lei_id:'969500NIKSAFC3BMLO66'})")

    eid = _upsert_entity_by_name("Alphabet Inc.", cik="0001652044")
    assert eid != "lei:FR", "SEC data was written onto the prefix-named company"

    rows = it_db.run_sql("SELECT sec_cik FROM Entity WHERE id = 'lei:FR'")
    assert rows[0].get("sec_cik") is None, "the French company was stamped with Alphabet's CIK"


def test_sec_still_matches_a_genuine_truncated_name(it_db):
    # The fallback exists for EDGAR's shortened filer names; a prefix ending on a
    # word boundary is still a match.
    from app.scraper.runner import _upsert_entity_by_name

    it_db.run_command(
        "CREATE (e:Entity {id:'ent-apple', name:'Apple', name_normalized:'apple', "
        "type:'company'})")
    assert _upsert_entity_by_name("Apple Computer", cik="0000320193") == "ent-apple"


def test_sec_will_not_hijack_a_node_holding_another_filers_cik(it_db):
    from app.scraper.runner import _upsert_entity_by_name

    it_db.run_command(
        "CREATE (e:Entity {id:'ent-other', name:'Apple', name_normalized:'apple', "
        "type:'company', sec_cik:'9999999999'})")
    assert _upsert_entity_by_name("Apple Computer", cik="0000320193") != "ent-other"


def test_the_website_persists_and_the_first_writer_wins(it_db):
    """website is fill-if-missing across sources — a site rarely changes, and a
    register value must not be clobbered by a later crowd-edited one."""
    from app.scraper.runner import _upsert_entity

    eid = _upsert_entity("Site Co", "company", "US", None, None, None, "Q77",
                         website="https://site.test")
    row = it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e.website AS w",
                            {"id": eid})[0]
    assert row["w"] == "https://site.test"

    _upsert_entity("Site Co", "company", "US", None, None, None, "Q77",
                   website="https://other.test")
    row = it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e.website AS w",
                            {"id": eid})[0]
    assert row["w"] == "https://site.test"

    # …and the profile payload carries it with no serializer changes.
    from app.routers.search import get_full_profile
    assert get_full_profile(eid)["entity"]["website"] == "https://site.test"


def test_the_by_name_writer_fills_the_website_on_all_paths(it_db):
    from app.scraper.graph_writer import _upsert_entity_by_name

    # CREATE branch
    eid = _upsert_entity_by_name("Fresh Filer Inc", cik="0009999901",
                                 website="https://fresh.test")
    w = it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e.website AS w",
                          {"id": eid})[0]["w"]
    assert w == "https://fresh.test"

    # UPDATE branch: fill a gap on an existing node, never clobber
    it_db.run_command("CREATE (:Entity {id:'bare', name:'Bare Co', "
                      "name_normalized:'bare co', sec_cik:'0009999902'})")
    _upsert_entity_by_name("Bare Co", cik="0009999902", website="https://bare.test")
    w = it_db.run_command("MATCH (e:Entity {id:'bare'}) RETURN e.website AS w")[0]["w"]
    assert w == "https://bare.test"
    _upsert_entity_by_name("Bare Co", cik="0009999902", website="https://usurper.test")
    w = it_db.run_command("MATCH (e:Entity {id:'bare'}) RETURN e.website AS w")[0]["w"]
    assert w == "https://bare.test"


def test_the_logo_url_persists_fills_if_missing_and_reaches_the_profile(it_db):
    """logo_url follows the website's rules: fill-if-missing, display-only,
    and the generic profile wire carries it with no serializer changes."""
    from app.scraper.runner import _upsert_entity

    logo = ("https://upload.wikimedia.org/wikipedia/commons/thumb/"
            "b/bd/Tesla_Motors.svg/250px-Tesla_Motors.svg.png")
    eid = _upsert_entity("Logo Co", "company", "US", None, None, None, "Q88",
                         logo_url=logo)
    row = it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e.logo_url AS l",
                            {"id": eid})[0]
    assert row["l"] == logo

    _upsert_entity("Logo Co", "company", "US", None, None, None, "Q88",
                   logo_url="https://upload.wikimedia.org/usurper.png")
    row = it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e.logo_url AS l",
                            {"id": eid})[0]
    assert row["l"] == logo

    from app.routers.search import get_full_profile
    assert get_full_profile(eid)["entity"]["logo_url"] == logo
