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
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


# ── The Cypher actually parses ────────────────────────────────────────────────

def test_wikidata_upsert_creates_and_updates_for_real(it_db):
    from unittest.mock import patch

    from app.scraper.runner import _upsert_entity

    # no register knows the item's ids: a `wd:` node, which carries none of
    # them (2026-10-29 — a hard id comes from its register only)
    with patch("app.scraper.register_pointer.write_from_pointer", return_value=None):
        eid = _upsert_entity("Alphabet Inc.", 2015, None,
                             "Holding company", "Q20800404",
                             lei="5493006MHB84DD0ZWV18", sec_cik="0001652044")
    assert eid == "wd:Q20800404"
    rows = it_db.run_sql(f"SELECT FROM Entity WHERE id = '{eid}'")
    assert rows, "create path did not write a node"
    node = {k: v for k, v in rows[0].items() if not k.startswith("@")}
    assert node["wikidata_id"] == "Q20800404"
    assert node.get("lei_id") is None and node.get("sec_cik") is None

    # Second call takes the UPDATE branch — the one the bad comment sat in.
    again = _upsert_entity("Alphabet Inc.", 2015, 1.0,
                           "Holding company", "Q20800404",
                           lei="5493006MHB84DD0ZWV18", sec_cik="0001652044")
    assert again == eid
    rows = it_db.run_sql(f"SELECT FROM Entity WHERE id = '{eid}'")
    node = {k: v for k, v in rows[0].items() if not k.startswith("@")}
    assert node["revenue"] == 1.0
    assert node.get("lei_id") is None and node.get("sec_cik") is None, "the update wrote Wikidata's ids"


def test_a_node_whose_register_id_the_item_contradicts_gets_nothing(it_db):
    """BlackRock (2026-10-28): Q219635 found the listed company by name, and
    its LEI is not the one the item states — another legal entity."""
    from app.scraper.runner import _upsert_entity

    it_db.run_command(
        "CREATE (e:Entity {id:'lei:REG', name:'Registered Co', name_normalized:'registered co', "
        "type:'company', lei_id:'REGISTEREDLEI0000001', name_credibility:92})")
    # no register knows the item's ids: no twin of the name either (findings item 30)
    with patch("app.scraper.register_pointer.write_from_pointer", return_value=None):
        assert _upsert_entity("Registered Co", None, None, "a description", "Q1",
                              lei="WIKIDATASAYSOTHER001", sec_cik="0000000001") is None
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity")[0]["n"] == 1
    row = it_db.run_sql("SELECT FROM Entity WHERE id = 'lei:REG'")[0]
    assert row["lei_id"] == "REGISTEREDLEI0000001" and row.get("sec_cik") is None
    assert row.get("wikidata_id") is None and row.get("description") is None


def test_wikidata_writes_no_address_and_no_type(it_db):
    """Unilever (2026-10-27): the Wikidata update put NL — the first of the two
    countries its group item Q157062 states — over the GB of the register node
    for UNILEVER PLC. Wikidata writes no address and no type, on update or
    create."""
    from app.scraper.runner import _upsert_entity

    it_db.run_command(
        "CREATE (e:Entity {id:'gb-coh:00041424', name:'UNILEVER PLC', name_normalized:'unilever', "
        "type:'holding', country:'GB', countries:['GB'], hq_city:'Port Sunlight', hq_country:'GB', "
        "hq_lat:53.35, hq_lng:-2.99, lei_id:'549300MKFYEKVRWML317', name_credibility:97})")
    eid = _upsert_entity("Unilever", 1929, None, "British consumer goods company", "Q157062",
                         lei="549300MKFYEKVRWML317", source_id="wikidata")
    assert eid == "gb-coh:00041424"
    row = it_db.run_sql("SELECT FROM Entity WHERE id = 'gb-coh:00041424'")[0]
    assert (row["type"], row["country"], row["countries"]) == ("holding", "GB", ["GB"])
    assert (row["hq_city"], row["hq_country"], row["hq_lat"], row["hq_lng"]) == ("Port Sunlight", "GB", 53.35, -2.99)
    assert "hq_locations" not in row
    assert row["wikidata_id"] == "Q157062" and row["founded"] == 1929      # the rest still enriches

    new_id = _upsert_entity("Only In Wikidata", None, None, None, "Q999001", source_id="wikidata")
    node = it_db.run_sql("SELECT FROM Entity WHERE id = :id", {"id": new_id})[0]
    assert node["type"] == "company"
    for absent in ("country", "countries", "hq_city", "hq_country", "hq_lat", "hq_lng", "hq_locations"):
        assert node.get(absent) is None, absent


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

    eid = _upsert_entity("Site Co", None, None, None, "Q77",
                         website="https://site.test")
    row = it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e.website AS w",
                            {"id": eid})[0]
    assert row["w"] == "https://site.test"

    _upsert_entity("Site Co", None, None, None, "Q77",
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
    eid = _upsert_entity("Logo Co", None, None, None, "Q88",
                         logo_url=logo)
    row = it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e.logo_url AS l",
                            {"id": eid})[0]
    assert row["l"] == logo

    _upsert_entity("Logo Co", None, None, None, "Q88",
                   logo_url="https://upload.wikimedia.org/usurper.png")
    row = it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e.logo_url AS l",
                            {"id": eid})[0]
    assert row["l"] == logo

    from app.routers.search import get_full_profile
    assert get_full_profile(eid)["entity"]["logo_url"] == logo
