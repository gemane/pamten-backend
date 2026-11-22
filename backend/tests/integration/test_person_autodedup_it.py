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


def test_snr_against_jnr_is_the_same_verdict(it_db):
    """Findings item 147: the UK register's filers write "Snr" / "Jnr"; read as
    no mark, a father and son whose records share a name at one company merge."""
    from app.routers.persons import deduplicate_high_confidence
    _berkshire(it_db)
    it_db.run_command("CREATE (:Person {id:'snr', full_name:'Thomas Murphy', aliases: ['Thomas Murphy Snr']})")
    it_db.run_command("CREATE (:Person {id:'jnr', full_name:'Thomas Murphy', aliases: ['Thomas Murphy (Jnr)']})")
    _seat(it_db, "snr")
    _seat(it_db, "jnr")

    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 0
    (group,) = [g for g in res["needs_review"] if {m["id"] for m in g["members"]} == {"snr", "jnr"}]
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
    # the one without a date waits for a human beside the merged two, as a
    # doubt (the review list is the scan AFTER the merges, findings item 145)
    left = [g for g in res["needs_review"] if "choff:c" in {m["id"] for m in g["members"]}]
    assert len(left) == 1 and len(left[0]["members"]) == 2 and left[0]["likely_distinct"] is False


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
    assert it_db.run_command("MATCH (ml:MergeLog) RETURN ml.reason AS r")[0]["r"].endswith(
        "same birth month, a rare name")


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


# ── a birth month is evidence for a rare name only; evidence links two people,
#    not a group (findings item 124) ──────────────────────────────────────────

def _works_at(it_db, company, *pids):
    it_db.run_command(f"CREATE (:Entity {{id: '{company}', name: '{company}', type: 'company'}})")
    for pid in pids:
        it_db.run_command(f"MATCH (p:Person {{id: '{pid}'}}), (e:Entity {{id: '{company}'}}) "
                          "CREATE (p)-[:HAS_ROLE {role: 'Director'}]->(e)")


def _david_smiths(it_db, months):
    """One David Smith per birth month, and a second one born in the first."""
    _people(it_db, *[(f"chpsc:ds{i}", "David Smith", m) for i, m in enumerate(months)],
            ("chpsc:twin", "David Smith", months[0]))


@pytest.mark.parametrize("months, merged", [
    (["1961-03", "1958-07", "1974-11"], 1),                       # three birth months: a rare name
    (["1961-03", "1958-07", "1974-11", "1980-01"], 0),            # four: a namesake is to be expected
])
def test_a_shared_birth_month_merges_only_under_a_rare_name(it_db, months, merged):
    """The full UK register has 331 birth months of "David Smith"; two of them
    born in the same one are two people more often than the old rule allowed."""
    from app.routers.persons import deduplicate_high_confidence
    _david_smiths(it_db, months)
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == merged
    assert ("chpsc:twin" in _alive(it_db) and "chpsc:ds0" in _alive(it_db)) is (merged == 0)
    if not merged:
        pair = [g for g in res["needs_review"] if {m["id"] for m in g["members"]} == {"chpsc:ds0", "chpsc:twin"}]
        assert len(pair) == 1 and pair[0]["confidence"] != "high"
        assert "a birth month alone does not tell namesakes apart — the name spans 4 birth months" in pair[0]["reason"]


def test_under_a_common_name_the_month_counts_beside_a_shared_company(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _david_smiths(it_db, ["1961-03", "1958-07", "1974-11", "1980-01"])
    _works_at(it_db, "acme", "chpsc:ds0", "chpsc:twin")
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 1 and len(_alive(it_db)) == 4
    assert "share a company" in it_db.run_command("MATCH (ml:MergeLog) RETURN ml.reason AS r")[0]["r"]


def test_only_the_two_a_company_links_merge_not_the_third_of_the_month(it_db):
    """Three David Smiths born in March 1961, two of them at one company: the
    company is a fact about those two."""
    from app.routers.persons import deduplicate_high_confidence
    _david_smiths(it_db, ["1961-03", "1958-07", "1974-11", "1980-01"])
    _people(it_db, ("chpsc:third", "David Smith", "1961-03"))
    _works_at(it_db, "acme", "chpsc:ds0", "chpsc:twin")
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 1
    assert "chpsc:third" in _alive(it_db) and len(_alive(it_db)) == 5


def test_a_full_birth_date_still_stands_alone_under_a_common_name(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _people(it_db, ("wd:a", "David Smith", "1961-03-04"), ("p-b", "David Smith", "1961-03-04"),
            *[(f"chpsc:o{i}", "David Smith", m) for i, m in enumerate(["1958-07", "1974-11", "1980-01", "1990-05"])])
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 1 and len(_alive(it_db)) == 5


def test_a_day_and_the_registers_month_do_not_merge_under_a_common_name(it_db):
    """Wikidata's David Smith of 4 March 1961 and the register's of March 1961:
    the register side states a month, and the name is common."""
    from app.routers.persons import deduplicate_high_confidence
    _people(it_db, ("wd:a", "David Smith", "1961-03-04"), ("chpsc:b", "David Smith", "1961-03"),
            *[(f"chpsc:o{i}", "David Smith", m) for i, m in enumerate(["1958-07", "1974-11", "1980-01"])])
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 0 and len(_alive(it_db)) == 5


def test_one_shared_company_does_not_merge_the_whole_group_of_a_name(it_db):
    """No birth dates at all: two spellings of one person at one company, and a
    third bearer of the name elsewhere. One pair sharing a company used to
    make the whole group "high"."""
    from app.routers.persons import deduplicate_high_confidence
    _people(it_db, ("sec:1", "Monica C. Lozano", None), ("p-2", "Monica C Lozano", None),
            ("p-3", "Lozano Monica C", None))
    _works_at(it_db, "acme", "sec:1", "p-2")
    _works_at(it_db, "other", "p-3")
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 1 and "p-3" in _alive(it_db) and len(_alive(it_db)) == 2
    # the third waits beside the merged two, and not as "high" (the review list
    # is the scan AFTER the merges, findings item 145)
    left = [g for g in res["needs_review"] if "p-3" in {m["id"] for m in g["members"]}]
    assert len(left) == 1 and len(left[0]["members"]) == 2 and left[0]["confidence"] != "high"


def test_a_chain_of_shared_companies_links_all_of_them(it_db):
    """A and B at one company, B and C at another: B is the same person in
    both, so the three are one."""
    from app.routers.persons import deduplicate_high_confidence
    _people(it_db, ("sec:1", "Monica C. Lozano", None), ("p-2", "Monica C Lozano", None),
            ("p-3", "Lozano Monica C", None))
    _works_at(it_db, "acme", "sec:1", "p-2")
    _works_at(it_db, "other", "p-2", "p-3")
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 2 and len(_alive(it_db)) == 1


def test_the_scoped_scan_counts_the_names_months_among_the_candidates(it_db):
    """Seeded from one David Smith, the scan fetches the others of the name —
    and sees that the name is common."""
    from app.routers.persons import deduplicate_high_confidence
    _david_smiths(it_db, ["1961-03", "1958-07", "1974-11", "1980-01"])
    res = deduplicate_high_confidence(apply=True, seed_ids=["chpsc:twin"])
    assert res["merged_count"] == 0 and len(_alive(it_db)) == 5


def test_a_full_scan_is_refused_on_a_graph_too_large_to_load(it_db):
    from unittest.mock import patch

    from app.config import settings
    from app.routers.persons import ScanTooLarge, deduplicate_high_confidence, scan_duplicate_groups
    _people(it_db, ("p-a", "Jane Mary Smith", "1979-10-04"), ("p-b", "Jane Mary Smith", "1979-10-04"))
    with patch.object(settings, "PERSON_FULL_SCAN_MAX", 1):
        with pytest.raises(ScanTooLarge, match="2 persons.*--scraped-only"):
            scan_duplicate_groups()
        with pytest.raises(ScanTooLarge):
            deduplicate_high_confidence(apply=True)
        # scoped, the same graph is scanned
        assert deduplicate_high_confidence(apply=True, seed_ids=["p-a"])["merged_count"] == 1
    assert len(_alive(it_db)) == 1


def test_a_day_and_its_month_count_as_one_birth_month_of_the_name(it_db):
    """Three birth months, one of them stated once as a day and once as the
    month: still three, still a rare name — Masayoshi Son's case under a name
    two more people carry."""
    from app.routers.persons import deduplicate_high_confidence
    _people(it_db, ("wd:a", "Tadashi Yanai", "1949-02-07"), ("chpsc:b", "Tadashi Yanai", "1949-02"),
            ("chpsc:c", "Tadashi Yanai", "1961-05"), ("chpsc:d", "Tadashi Yanai", "1975-09"))
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 1 and len(_alive(it_db)) == 3


# ── a merge changes what the scan saw (findings item 145) ────────────────────

def _masseys(it_db):
    """Dev, rebuild 2026-11-21b: two officer records of "Robert Christopher
    Massey" (a rare name, one birth month) and a "Robert Massey" of the same
    month who shares a company with ONE of them — the one with fewer seats,
    which the name merge folds into the other."""
    _people(it_db, ("choff:kept", "Robert Christopher Massey", "1985-11"),
            ("choff:folded", "Robert Christopher Massey", "1985-11"),
            ("choff:short", "Robert Massey", "1985-11"))
    for pid, first in (("choff:kept", "Robert Christopher"), ("choff:folded", "Robert Christopher"),
                       ("choff:short", "Robert")):
        it_db.run_command("MATCH (p:Person {id: $id}) SET p.first_name = $f, p.last_name = 'Massey'",
                          {"id": pid, "f": first})
    _works_at(it_db, "brightside", "choff:kept")
    _works_at(it_db, "brightside-two", "choff:kept")
    _works_at(it_db, "anacap", "choff:folded", "choff:short")


def test_a_pair_whose_record_was_just_merged_away_is_merged_by_the_next_pass(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _masseys(it_db)
    res = deduplicate_high_confidence(apply=True)
    assert _alive(it_db) == ["choff:kept"]
    assert res["merged_count"] == 2 and res["merging_passes"] == 2 and res["passes"] == 3
    assert res["review_count"] == 0
    seats = {r["e"] for r in it_db.run_command(
        "MATCH (p:Person {id: 'choff:kept'})-[:HAS_ROLE]->(e:Entity) RETURN e.id AS e")}
    assert seats == {"brightside", "brightside-two", "anacap"}
    # each merge is logged with the evidence of ITS pair: the name for the first,
    # the shared company for the second
    reasons = sorted(r["r"] for r in it_db.run_command("MATCH (ml:MergeLog) RETURN ml.reason AS r"))
    assert any("a rare name" in r for r in reasons) and any("shared company" in r for r in reasons)


def test_a_second_pass_judges_the_pair_again_and_may_refuse_it(it_db):
    """No forward is followed: the survivor is another record than the one the
    scan paired. Here the kept record and the short name carry two different
    Wikidata items — the folded record carried none, so its pair was rated
    high — and the pair waits for a human instead of merging on the folded
    record's evidence."""
    from app.routers.persons import deduplicate_high_confidence
    _masseys(it_db)
    it_db.run_command("MATCH (p:Person {id: 'choff:kept'}) SET p.wikidata_id = 'Q111'")
    it_db.run_command("MATCH (p:Person {id: 'choff:short'}) SET p.wikidata_id = 'Q222'")
    res = deduplicate_high_confidence(apply=True)
    assert _alive(it_db) == ["choff:kept", "choff:short"] and res["merged_count"] == 1
    pair = [g for g in res["needs_review"] if {m["id"] for m in g["members"]} == {"choff:kept", "choff:short"}]
    assert len(pair) == 1 and pair[0]["likely_distinct"] is True


def test_the_scoped_run_goes_on_from_the_survivor(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _masseys(it_db)
    # seeded with the pair only: the survivor of the first pass is not among the
    # seeds, and nothing but its id leads the second pass to it
    res = deduplicate_high_confidence(apply=True, seed_ids=["choff:folded", "choff:short"])
    assert _alive(it_db) == ["choff:kept"] and res["merged_count"] == 2


def test_a_dry_run_is_one_pass_and_merges_nothing(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _masseys(it_db)
    res = deduplicate_high_confidence(apply=False)
    assert res["passes"] == 1 and _alive(it_db) == ["choff:folded", "choff:kept", "choff:short"]


def test_a_run_that_merges_nothing_scans_once(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _people(it_db, ("p-a", "Ada Lovelace", "1815-12"), ("p-b", "Grace Hopper", "1906-12"))
    res = deduplicate_high_confidence(apply=True)
    assert res["passes"] == 1 and res["merging_passes"] == 0


def test_the_passes_are_capped(it_db, monkeypatch):
    from app.routers import persons
    scans = []
    group = {"confidence": "high", "likely_distinct": False, "reason": "r", "suggested_keep_id": "a",
             "members": [{"id": "a", "full_name": "A"}, {"id": "b", "full_name": "B"}]}
    monkeypatch.setattr(persons, "scan_duplicate_groups", lambda seed_ids=None: scans.append(1) or [group])
    monkeypatch.setattr(persons, "merge_person_records", lambda keep, dup, reason=None: keep)
    res = persons.deduplicate_high_confidence(apply=True)
    assert len(scans) == persons.DEDUP_MAX_PASSES == res["passes"]
