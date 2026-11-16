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
        "aliases: ['Lawrence Page', 'Page Lawrence']})")
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


# ── Father and son by name alone (2026-10-25) ────────────────────────────────
# The post-scrape auto-merge folded Berkshire's 2022 director "Thomas S.
# Murphy, Jr." into his father "Thomas Murphy" (dead since 2022): the son's
# "Thomas Murphy" alias matched, Berkshire corroborated, and nothing said the
# two Wikidata items — or the "Sr." and "Jr." — were two people.

def _berkshire(it_db):
    it_db.run_command("CREATE (:Entity {id:'brk', name:'Berkshire Hathaway', type:'company'})")


def _seat(it_db, pid):
    it_db.run_command("MATCH (p:Person {id:$p}),(e:Entity{id:'brk'}) CREATE (p)-[:HAS_ROLE {role:'Board Member'}]->(e)",
                      {"p": pid})


def test_two_wikidata_items_are_two_people(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _berkshire(it_db)
    it_db.run_command("CREATE (:Person {id:'sr', full_name:'Thomas Murphy', wikidata_id:'Q3525537', "
                      "aliases: ['Thomas S. Murphy']})")
    it_db.run_command("CREATE (:Person {id:'jr', full_name:'Thomas S. Murphy, Jr.', wikidata_id:'Q124988340', "
                      "aliases: ['Thomas Murphy', 'Tom Murphy']})")
    _seat(it_db, "sr")
    _seat(it_db, "jr")

    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 0
    assert it_db.run_command("MATCH (p:Person {id:'jr'}) RETURN p.id AS id")[0]["id"] == "jr"
    (group,) = [g for g in res["needs_review"] if {m["id"] for m in g["members"]} == {"sr", "jr"}]
    assert group["likely_distinct"] is True and "DIFFERENT Wikidata items" in group["reason"]


def test_sr_against_jr_is_definitive_without_any_id(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _berkshire(it_db)
    it_db.run_command("CREATE (:Person {id:'sr', full_name:'Thomas Murphy', aliases: ['Thomas Murphy, Sr.']})")
    it_db.run_command("CREATE (:Person {id:'jr', full_name:'Thomas Murphy', aliases: ['Thomas Murphy, Jr.']})")
    _seat(it_db, "sr")
    _seat(it_db, "jr")

    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 0
    (group,) = [g for g in res["needs_review"] if {m["id"] for m in g["members"]} == {"sr", "jr"}]
    assert group["likely_distinct"] is True and "generational marks" in group["reason"]


def test_a_mark_on_one_side_only_waits_for_a_human(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _berkshire(it_db)
    it_db.run_command("CREATE (:Person {id:'plain', full_name:'Thomas Murphy', wikidata_id:'Q3525537'})")
    it_db.run_command("CREATE (:Person {id:'jr', full_name:'Thomas S Murphy Jr', aliases: ['Thomas Murphy']})")
    _seat(it_db, "plain")
    _seat(it_db, "jr")

    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 0
    (group,) = [g for g in res["needs_review"] if {m["id"] for m in g["members"]} == {"plain", "jr"}]
    assert group["confidence"] == "medium" and group["likely_distinct"] is False
    assert "rule out a relative" in group["reason"]


def test_the_same_mark_on_both_sides_still_merges(it_db):
    """SEC's "Gates William H Iii" and the Wikidata node whose alias is
    "William H. Gates III": one mark, one person — the merge that must keep
    working."""
    from app.routers.persons import deduplicate_high_confidence
    it_db.run_command("CREATE (:Entity {id:'msft', name:'Microsoft', type:'company'})")
    it_db.run_command("CREATE (:Person {id:'bg', full_name:'Bill Gates', wikidata_id:'Q5284', aliases: ['William H. Gates III']})")
    it_db.run_command("CREATE (:Person {id:'sec', full_name:'Gates William H Iii'})")
    for pid in ("bg", "sec"):
        it_db.run_command("MATCH (p:Person {id:$p}),(e:Entity{id:'msft'}) CREATE (p)-[:HAS_ROLE {role:'Director'}]->(e)",
                          {"p": pid})
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 1 and res["merged"][0]["keep_id"] == "bg"


# ── The direction the variants cannot take (findings item 55, 2026-11-05) ────
# `_name_lookup_variants` only SHORTENS a name, so a seed written under the fuller
# spelling found its twin and a seed written under the shorter one never did. The
# 2026-11-04 rebuild wrote News Corp's insiders from EDGAR first and the Wikidata
# part two hours later in its own scope: "Lachlan Murdoch" stayed beside "Lachlan
# K. Murdoch", "Robert James Thomson" beside "Robert J Thomson" — pairs the full
# scan rated high at once. The fetch goes through the FULL_TEXT index on
# `search_text`, which every written person carries (`_person_search_text`).

def _person_with_search_text(it_db, pid, full_name, aliases=(), wikidata_id=None, sec_cik=None):
    it_db.run_command(
        "CREATE (:Person {id:$id, full_name:$n, aliases:$aliases, wikidata_id:$wid, sec_cik:$cik, "
        "search_text:$st})",
        {"id": pid, "n": full_name, "aliases": list(aliases), "wid": wikidata_id, "cik": sec_cik,
         "st": " ".join([full_name, *aliases])})


def _news_corp(it_db, *pids):
    it_db.run_command("CREATE (:Entity {id:'nws', name:'News Corp', type:'company'})")
    for pid in pids:
        it_db.run_command("MATCH (p:Person {id:$p}),(e:Entity {id:'nws'}) CREATE (p)-[:HAS_ROLE {role:'Director'}]->(e)",
                          {"p": pid})


def test_the_shorter_spelling_reaches_the_longer_one(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _person_with_search_text(it_db, "sec-lachlan", "Lachlan K. Murdoch", sec_cik="0001242818")
    _person_with_search_text(it_db, "wd-lachlan", "Lachlan Murdoch", wikidata_id="Q3215817")
    _news_corp(it_db, "sec-lachlan", "wd-lachlan")

    res = deduplicate_high_confidence(apply=True, seed_ids=["wd-lachlan"])     # the Wikidata-only scope
    assert res["merged_count"] == 1
    left = it_db.run_command("MATCH (p:Person) WHERE p.full_name CONTAINS 'Murdoch' RETURN p.id AS id, p.sec_cik AS cik")
    assert len(left) == 1 and left[0]["cik"] == "0001242818"                  # one person, the CIK kept


def test_an_alias_reaches_the_longer_spelling_too(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _person_with_search_text(it_db, "sec-thomson", "Robert J Thomson", sec_cik="0001579380")
    _person_with_search_text(it_db, "wd-thomson", "Robert James Thomson", aliases=["Robert Thomson"],
                             wikidata_id="Q7346054")
    _news_corp(it_db, "sec-thomson", "wd-thomson")

    res = deduplicate_high_confidence(apply=True, seed_ids=["wd-thomson"])
    assert res["merged_count"] == 1
    assert it_db.run_command("MATCH (p:Person) WHERE p.full_name CONTAINS 'Thomson' RETURN count(p) AS n")[0]["n"] == 1


def test_the_wider_fetch_still_keeps_relatives_apart(it_db):
    """The fetch only puts more pairs in front of the scorer; its guards decide.
    A Wikidata "Thomas Murphy" whose item states one CIK, beside an EDGAR
    "Thomas S. Murphy" filing under another, is a father and son — fetched,
    grouped, and left alone."""
    from app.routers.persons import deduplicate_high_confidence
    _person_with_search_text(it_db, "sec-sr", "Thomas S. Murphy", sec_cik="0000001111")
    _person_with_search_text(it_db, "wd-tm", "Thomas Murphy", wikidata_id="Q3525537", sec_cik="0000002222")
    _news_corp(it_db, "sec-sr", "wd-tm")

    res = deduplicate_high_confidence(apply=True, seed_ids=["wd-tm"])
    assert res["merged_count"] == 0
    assert it_db.run_command("MATCH (p:Person) WHERE p.full_name CONTAINS 'Murphy' RETURN count(p) AS n")[0]["n"] == 2
    (group,) = [g for g in res["needs_review"] if {m["id"] for m in g["members"]} == {"sec-sr", "wd-tm"}]
    assert group["likely_distinct"] is True and "DIFFERENT SEC CIKs" in group["reason"]


def test_an_initial_alone_fetches_no_namesakes(it_db):
    """"J. Smith" has one substantive word; fetching every Smith would hand the
    scorer the whole graph for nothing (its own `_name_key` keeps the initial
    for the same reason). Scoped from that seed, the other Smith is never seen."""
    from app.routers.persons import deduplicate_high_confidence
    _person_with_search_text(it_db, "js", "J. Smith")
    _person_with_search_text(it_db, "jsmith", "John Smith")
    _news_corp(it_db, "js", "jsmith")

    res = deduplicate_high_confidence(apply=True, seed_ids=["js"])
    assert res["merged_count"] == 0 and res["review_count"] == 0


# ── the birth date as the deciding signal (findings item 122) ───────────────

def _people(it_db, *rows):
    for pid, name, birth in rows:
        birth_sql = f", birth_date: '{birth}'" if birth else ""
        it_db.run_command(f"CREATE (:Person {{id: '{pid}', full_name: '{name}'{birth_sql}}})")


def _alive(it_db):
    return sorted(r["id"] for r in it_db.run_command("MATCH (p:Person) RETURN p.id AS id"))


def test_a_person_without_a_birth_date_does_not_merge_along(it_db):
    """Two register officers share name and birth month; a third carries the
    name and no date. The whole group merged — the third on its name alone."""
    from app.routers.persons import deduplicate_high_confidence
    _people(it_db, ("choff:a", "Paul Edelstyn", "1966-02"), ("p-b", "Paul Edelstyn", "1966-02-14"),
            ("choff:c", "Paul Edelstyn", None))
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 1
    assert len(_alive(it_db)) == 2 and "choff:c" in _alive(it_db)
    # the three wait for a human as a doubt — a month and a day in it are no conflict
    whole = [g for g in res["needs_review"] if len(g["members"]) == 3]
    assert len(whole) == 1 and whole[0]["likely_distinct"] is False


def test_a_day_and_the_month_it_lies_in_are_one_birth_date(it_db):
    """Wikidata's 1957-08-11 and the register's 1957-08 read as two dates kept
    Masayoshi Son apart as "likely distinct"; the person keeps the day."""
    from app.routers.persons import deduplicate_high_confidence
    it_db.run_command("CREATE (:Person {id: 'choff:son', full_name: 'Masayoshi Son', birth_date: '1957-08'})")
    it_db.run_command("CREATE (:Person {id: 'p-son', full_name: 'Masayoshi Son', birth_date: '1957-08-11'})")
    # the register's node is the better connected one, so it is the one kept
    it_db.run_command("CREATE (:Entity {id: 'arm', name: 'Arm', type: 'company'})")
    it_db.run_command("MATCH (p:Person {id: 'choff:son'}), (e:Entity {id: 'arm'}) CREATE (p)-[:HAS_ROLE {role: 'Director'}]->(e)")
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 1 and _alive(it_db) == ["choff:son"]
    assert it_db.run_command("MATCH (p:Person {id: 'choff:son'}) RETURN p.birth_date AS b")[0]["b"] == "1957-08-11"
    log = it_db.run_command("MATCH (ml:MergeLog) RETURN ml.reason AS reason, ml.dup_id AS dup")
    assert [r["dup"] for r in log] == ["p-son"] and "same birth month" in log[0]["reason"]


def test_two_dates_that_disagree_stay_two_people(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _people(it_db, ("p-a", "Vishal Sikka", "1967-06-01"), ("choff:b", "Vishal Sikka", "1967-05"))
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 0 and len(_alive(it_db)) == 2
    assert res["needs_review"][0]["likely_distinct"] is True


def test_a_year_alone_that_agrees_merges_nobody(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _people(it_db, ("p-a", "John Turner", "1957-00-00"), ("p-b", "John Turner", "1957-00-00"))
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 0 and len(_alive(it_db)) == 2
    assert res["needs_review"][0]["likely_distinct"] is False


def test_the_two_who_share_a_month_merge_beside_a_namesake_born_in_another(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _people(it_db, ("choff:a", "Mark Stephen Jones", "1959-11"), ("choff:b", "Mark Stephen Jones", "1959-11"),
            ("choff:c", "Mark Stephen Jones", "1971-03"))
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 1
    assert len(_alive(it_db)) == 2 and "choff:c" in _alive(it_db)
    assert it_db.run_command("MATCH (ml:MergeLog) RETURN ml.reason AS r")[0]["r"].endswith("same birth month")


def test_two_full_dates_are_logged_as_a_date_and_a_manual_merge_as_manual(it_db):
    from app.routers.persons import deduplicate_high_confidence, merge_person_records
    _people(it_db, ("p-a", "Jane Mary Smith", "1979-10-04"), ("p-b", "Jane Mary Smith", "1979-10-04"),
            ("p-c", "Omar Khan", None), ("p-d", "O. Khan", None))
    deduplicate_high_confidence(apply=True)
    merge_person_records("p-c", "p-d")
    reasons = {r["dup"]: r["reason"] for r in it_db.run_command(
        "MATCH (ml:MergeLog) RETURN ml.dup_id AS dup, ml.reason AS reason")}
    assert reasons["p-d"] == "manual"
    auto = next(v for k, v in reasons.items() if k != "p-d")
    assert auto.endswith("same birth date")
