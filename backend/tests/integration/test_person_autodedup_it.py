"""
Real-ArcadeDB integration test for the auto-dedup step run after a scrape:
deduplicate_high_confidence() must merge ONLY high-confidence, non-distinct
groups and leave medium/low ones for manual review.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _high_and_medium(it_db):
    # HIGH: same name token set (order flipped) + shared company → auto-merge.
    it_db.run_command("CREATE (:Person {id:'p1', full_name:'Warren E Buffett', wikidata_id:'Q1'})")
    it_db.run_command("CREATE (:Person {id:'p2', full_name:'Buffett Warren E'})")
    it_db.run_command("CREATE (:Entity {id:'brk', name:'Berkshire Hathaway', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'p1'}),(e:Entity{id:'brk'}) CREATE (p)-[:HAS_ROLE {role:'CEO'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'p2'}),(e:Entity{id:'brk'}) CREATE (p)-[:OWNS {}]->(e)")

    # MEDIUM: nickname variant (surname + company, different given names) → review only.
    it_db.run_command("CREATE (:Person {id:'k1', full_name:'Rob Kapito', first_name:'Rob', last_name:'Kapito', wikidata_id:'Q2'})")
    it_db.run_command("CREATE (:Person {id:'k2', full_name:'Robert Kapito', first_name:'Robert', last_name:'Kapito'})")
    it_db.run_command("CREATE (:Entity {id:'blk', name:'BlackRock', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'k1'}),(e:Entity{id:'blk'}) CREATE (p)-[:HAS_ROLE {role:'Founder'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'k2'}),(e:Entity{id:'blk'}) CREATE (p)-[:OWNS {}]->(e)")


def test_autodedup_merges_high_confidence_only(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _high_and_medium(it_db)

    res = deduplicate_high_confidence(apply=True)

    # high-confidence pair merged: the SEC-order node is gone, the Wikidata one kept
    assert it_db.run_command("MATCH (p:Person {id:'p2'}) RETURN p.id AS id") == []
    assert it_db.run_command("MATCH (p:Person {id:'p1'}) RETURN p.id AS id")[0]["id"] == "p1"
    assert res["merged_count"] == 1
    assert res["merged"][0]["keep_id"] == "p1"

    # medium variant untouched, surfaced for review
    assert it_db.run_command("MATCH (p:Person {id:'k2'}) RETURN p.id AS id")[0]["id"] == "k2"
    review_ids = {m["id"] for g in res["needs_review"] for m in g["members"]}
    assert {"k1", "k2"} <= review_ids


def test_autodedup_dry_run_reports_without_merging(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _high_and_medium(it_db)

    res = deduplicate_high_confidence(apply=False)

    assert res["applied"] is False
    assert res["merged_count"] == 1                                   # reported…
    assert it_db.run_command("MATCH (p:Person {id:'p2'}) RETURN p.id AS id")[0]["id"] == "p2"  # …but NOT merged


# ── The two the scoped scan could never see (reported 2026-08-28) ────────────
# Both survived every scrape while the periodic full scan found them instantly:
# the scoped candidate set is fetched by EXACT string, but groups are matched on
# a normalised key, so a seed could not retrieve the node it would then match.

def _alphabet_pair(it_db):
    it_db.run_command("CREATE (:Entity {id:'goog', name:'Alphabet Inc.', type:'company'})")
    it_db.run_command(
        "CREATE (:Person {id:'larry', full_name:'Larry Page', wikidata_id:'Q4934', "
        "alias:['Lawrence Page', 'Page Lawrence']})")
    it_db.run_command("CREATE (:Person {id:'sec-larry', full_name:'Page Lawrence'})")
    it_db.run_command(
        "CREATE (:Person {id:'eric', full_name:'Eric Schmidt', first_name:'Eric', "
        "last_name:'Schmidt', wikidata_id:'Q92747'})")
    # SEC spelling: parse_full_name puts the middle initial in last_name
    it_db.run_command(
        "CREATE (:Person {id:'sec-eric', full_name:'Eric E. Schmidt', first_name:'Eric', "
        "last_name:'E. Schmidt'})")
    for pid in ("larry", "sec-larry", "eric", "sec-eric"):
        it_db.run_command(
            "MATCH (p:Person {id:$p}),(e:Entity {id:'goog'}) CREATE (p)-[:OWNS {}]->(e)",
            {"p": pid})


def test_the_scoped_scan_reaches_a_node_recorded_under_an_alias(it_db):
    """SEC writes "Page Lawrence"; the Wikidata node carries it only as an ALIAS.
    _candidate_persons promised to match aliases and only ever queried full_name."""
    from app.routers.persons import deduplicate_high_confidence
    _alphabet_pair(it_db)

    res = deduplicate_high_confidence(apply=True, seed_ids=["sec-larry"])
    assert res["merged_count"] == 1
    left = it_db.run_command(
        "MATCH (p:Person) WHERE p.full_name IN ['Larry Page','Page Lawrence'] "
        "RETURN count(p) AS n")[0]["n"]
    assert left == 1


def test_the_scoped_scan_reaches_a_middle_initial_spelling(it_db):
    """"Eric E. Schmidt" vs "Eric Schmidt": neither an exact name nor an alias
    match, and the initial also split the surname bucket ("eschmidt")."""
    from app.routers.persons import deduplicate_high_confidence
    _alphabet_pair(it_db)

    res = deduplicate_high_confidence(apply=True, seed_ids=["sec-eric"])
    assert res["merged_count"] == 1
    left = it_db.run_command(
        "MATCH (p:Person) WHERE p.full_name IN ['Eric Schmidt','Eric E. Schmidt'] "
        "RETURN count(p) AS n")[0]["n"]
    assert left == 1


def test_two_people_who_share_only_an_initial_are_not_merged(it_db):
    """The guard on dropping initials: reducing "J. Smith" to ("smith",) would
    match every Smith in the graph. Run through the FULL scan — the scoped one
    would not fetch the second Smith as a candidate anyway, so it would pass
    whatever the key logic did."""
    from app.routers.persons import deduplicate_high_confidence
    it_db.run_command("CREATE (:Entity {id:'co', name:'Some Co', type:'company'})")
    it_db.run_command("CREATE (:Person {id:'js', full_name:'J. Smith'})")
    it_db.run_command("CREATE (:Person {id:'as', full_name:'A. Smith'})")
    for pid in ("js", "as"):
        it_db.run_command(
            "MATCH (p:Person {id:$p}),(e:Entity {id:'co'}) CREATE (p)-[:OWNS {}]->(e)",
            {"p": pid})

    res = deduplicate_high_confidence(apply=True)      # full scan, no seeds
    assert res["merged_count"] == 0
    n = it_db.run_command("MATCH (p:Person) WHERE p.full_name ENDS WITH 'Smith' "
                          "RETURN count(p) AS n")[0]["n"]
    assert n == 2


def test_a_middle_initial_does_not_split_the_surname_bucket(it_db):
    """`parse_full_name` splits on the first space, so "Eric E. Schmidt" parses
    to last_name "E. Schmidt". Keying the surname on the whole field put him in
    an "eschmidt" bucket that nothing else could ever land in — so even the
    surname+company variant path could not pair him with "Eric Schmidt"."""
    from app.routers.persons import _surname_key
    assert _surname_key("E. Schmidt", "Eric E. Schmidt") == _surname_key("Schmidt", "Eric Schmidt")
    assert _surname_key("Edward Page", "Lawrence Edward Page") == "page"
    assert _surname_key(None, "Lawrence Page") == "page"      # unparsed fallback
