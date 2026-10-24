"""Real-ArcadeDB test for the PSC first-year lower-bound pass
(companies_house_psc.mark_first_year_lower_bounds): a notification inside the
register's first year on a company incorporated BEFORE the register is when the
company first declared it (its first confirmation statement), not when the
control began — on the edge and on its claim. Pins the SQL the pass relies on:
the `IN [..]` id read, the `expand(inE('OWNS'))` adjacency with its WHERE, the
UPDATE by edge rid and the claim UPDATE by to_id + since.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed(it_db):
    # old: incorporated 1903; young: incorporated inside the first year (its PSC
    # was named at incorporation — a real start); blank: not enriched yet
    for cid, founded in (("gb-coh:00000001", "1903-08-28"), ("gb-coh:00000002", "2016-09-01"),
                         ("gb-coh:00000003", None)):
        f = f", incorporated_on:'{founded}'" if founded else ""
        it_db.run_command(f"CREATE (:Entity {{id:'{cid}', companies_house_id:'{cid[7:]}'{f}}})")
    it_db.run_command("CREATE (:Entity {id:'owner', name:'Owner Ltd'})")
    edges = (("gb-coh:00000001", "2016-07-04", None, "PSC"),        # first year, old company → bound
             ("gb-coh:00000001", "2016-04-06", "register_start", "PSC"),   # the first day: already a bound
             ("gb-coh:00000001", "2018-01-01", None, "PSC"),        # after the first year: a start
             ("gb-coh:00000001", "2016-07-04", None, "RR"),         # not the register's statement
             ("gb-coh:00000002", "2016-09-01", None, "PSC"),        # young company: a real start
             ("gb-coh:00000003", "2016-07-04", None, "PSC"))        # unknown incorporation: untouched
    for cid, since, basis, ft in edges:
        b = f"'{basis}'" if basis else "null"
        it_db.run_command(
            "MATCH (a:Entity {id:'owner'}), (c:Entity {id:$c}) "
            f"CREATE (a)-[:OWNS {{since:'{since}', since_basis:{b}, filing_type:'{ft}', "
            "source_id:'psc', until:null}]->(c)", {"c": cid})
        it_db.run_sql(
            "INSERT INTO Claim SET kind = 'owns', from_id = 'owner', to_id = :c, source_id = 'psc', "
            f"filing_type = '{ft}', since = '{since}', since_basis = {b}, "
            f"claim_key = 'k-{cid}-{since}-{ft}'", {"c": cid})


def _bases(it_db):
    rows = it_db.run_command("MATCH (a)-[r:OWNS]->(c) RETURN c.id AS cid, r.since AS s, "
                             "r.filing_type AS f, r.since_basis AS b")
    return sorted((r["cid"][7:], r["s"], r["f"], r["b"]) for r in rows)


def test_first_year_notifications_on_old_companies_become_lower_bounds(it_db):
    from app.scraper.companies_house_psc import mark_first_year_lower_bounds
    _seed(it_db)
    res = mark_first_year_lower_bounds(apply=False)
    assert res == {"companies_before_register": 1, "edges": 1, "claims": 1, "applied": False}
    assert all(b != "register_first_year" for *_, b in _bases(it_db)), "a dry run wrote"

    res = mark_first_year_lower_bounds()
    assert res == {"companies_before_register": 1, "edges": 1, "claims": 1, "applied": True}
    assert _bases(it_db) == [
        ("00000001", "2016-04-06", "PSC", "register_start"),
        ("00000001", "2016-07-04", "PSC", "register_first_year"),
        ("00000001", "2016-07-04", "RR", None),
        ("00000001", "2018-01-01", "PSC", None),
        ("00000002", "2016-09-01", "PSC", None),
        ("00000003", "2016-07-04", "PSC", None)]
    claims = it_db.run_sql("SELECT to_id, since, filing_type, since_basis FROM Claim "
                           "WHERE since_basis = 'register_first_year'")
    assert [(c["to_id"], c["since"], c["filing_type"]) for c in claims] == [
        ("gb-coh:00000001", "2016-07-04", "PSC")]
    # idempotent: nothing left to mark
    assert mark_first_year_lower_bounds()["edges"] == 0


def test_the_pass_can_be_scoped_to_the_companies_an_import_touched(it_db):
    from app.scraper.companies_house_psc import mark_first_year_lower_bounds
    _seed(it_db)
    res = mark_first_year_lower_bounds(company_ids=["gb-coh:00000002", "gb-coh:00000003"])
    assert (res["companies_before_register"], res["edges"]) == (0, 0)
    res = mark_first_year_lower_bounds(company_ids=["gb-coh:00000001"])
    assert (res["companies_before_register"], res["edges"], res["claims"]) == (1, 1, 1)
