"""
GLEIF relationship history applied to a real ArcadeDB: the lei_id lookup and
the edge query (SQL list parameters, expand(inE())), the closed edge beside
the current one, the start filled on an undated current edge, a second run
writing nothing — and time travel showing the old parent in its years.
"""
import pytest

from app.routers.search import get_full_profile
from app.scraper import gleif_rr_history as h

pytestmark = pytest.mark.integration


def _company(it_db, eid, lei):
    it_db.run_command("CREATE (:Entity {id: $id, name: $id, type: 'company', lei_id: $lei})",
                      {"id": eid, "lei": lei})


def _seed(it_db):
    # the child was merged into its Companies House node: found by lei_id
    _company(it_db, "gb-coh:1", "CHILD0000000000000001")
    _company(it_db, "lei:NEWPARENT00000000001", "NEWPARENT00000000001")
    _company(it_db, "lei:OLDPARENT00000000001", "OLDPARENT00000000001")
    it_db.run_command(
        "MATCH (a:Entity {id: 'lei:NEWPARENT00000000001'}), (b:Entity {id: 'gb-coh:1'}) "
        "CREATE (a)-[:OWNS {source_id: 'gleif', filing_type: 'RR', direct_or_indirect: 'direct'}]->(b)")


def _intervals(tmp_path):
    hi = h.History()
    old = ("OLDPARENT00000000001", "CHILD0000000000000001")
    new = ("NEWPARENT00000000001", "CHILD0000000000000001")
    gone = ("NOTINGRAPH0000000001", "CHILD0000000000000001")
    hi.observe("2018-02-09", {old: ("direct", None), gone: ("indirect", None)})
    hi.observe("2020-05-01", {old: ("direct", None)})
    hi.observe("2020-06-01", {new: ("direct", None)})
    hi.observe("2026-10-05", {new: ("direct", None)})
    path = str(tmp_path / "hist.jsonl.gz")
    h.write_intervals(path, hi)
    return path


def _owns_into_child(it_db):
    return it_db.run_command(
        "MATCH (a:Entity)-[r:OWNS]->(:Entity {id: 'gb-coh:1'}) "
        "RETURN a.id AS parent, r.since AS since, r.since_basis AS basis, r.until AS until, "
        "r.until_reason AS reason, r.source_date AS sdate ORDER BY parent")


def test_history_lands_beside_today_and_time_travel_sees_it(it_db, tmp_path):
    _seed(it_db)
    path = _intervals(tmp_path)

    res = h.apply_history(path, "gleif", 92)
    assert (res["created"], res["since_filled"]) == (1, 1)
    assert res["skipped"]["unresolved"] == 1                      # NOTINGRAPH…
    rows = {r["parent"]: r for r in _owns_into_child(it_db)}
    old = rows["lei:OLDPARENT00000000001"]
    assert (old["since"], old["basis"], old["until"], old["reason"], old["sdate"]) == \
        ("2018-02-09", "gleif_first_seen", "2020-06-01", "gleif_dropped", "2020-05-01")
    new = rows["lei:NEWPARENT00000000001"]
    assert (new["since"], new["basis"], new["until"]) == ("2020-06-01", "gleif_first_seen", None)

    again = h.apply_history(path, "gleif", 92)
    assert (again["created"], again["since_filled"]) == (0, 0)
    assert len(_owns_into_child(it_db)) == 2

    def owners(as_of=None):
        return {o["owner"]["id"] for o in get_full_profile("gb-coh:1", as_of=as_of)["owners"]}
    assert owners() == {"lei:NEWPARENT00000000001"}
    # 2019: the old parent; the new one only "at least since 2020" — a lower
    # bound, so present (dimmed by the client), never hidden
    assert "lei:OLDPARENT00000000001" in owners("2019-12-31")
    assert owners("2021-12-31") == {"lei:NEWPARENT00000000001"}


def test_a_dry_run_writes_nothing(it_db, tmp_path):
    _seed(it_db)
    res = h.apply_history(_intervals(tmp_path), "gleif", 92, dry_run=True)
    assert res["created"] == 1 and res["dry_run"] is True
    assert len(_owns_into_child(it_db)) == 1


def test_a_start_written_meanwhile_is_never_overwritten(it_db, tmp_path, monkeypatch):
    # the plan saw the edge undated; by the write it has a date — keep that
    _seed(it_db)
    it_db.run_command("MATCH (:Entity)-[r:OWNS]->(:Entity {id: 'gb-coh:1'}) SET r.since = '2005-01-01'")
    stale = h._rr_edges

    def as_planned(ids):
        edges = stale(ids)
        for rows in edges.values():
            for r in rows:
                r["since"] = None
        return edges
    monkeypatch.setattr(h, "_rr_edges", as_planned)
    assert h.apply_history(_intervals(tmp_path), "gleif", 92)["since_filled"] == 1
    rows = {r["parent"]: r for r in _owns_into_child(it_db)}
    assert rows["lei:NEWPARENT00000000001"]["since"] == "2005-01-01"
    assert rows["lei:NEWPARENT00000000001"]["basis"] is None


def _activision(it_db, tmp_path):
    """Microsoft → Activision as the importer writes it from GLEIF's record
    ("since 2001-07-03"), and an archive in which King.com named Activision
    its ultimate parent until 2026-07."""
    for eid in ("lei:MSFT0000000000000001", "lei:ATVI0000000000000001", "lei:KING0000000000000001"):
        _company(it_db, eid, eid[4:])
    it_db.run_command(
        "MATCH (a:Entity {id: 'lei:MSFT0000000000000001'}), (b:Entity {id: 'lei:ATVI0000000000000001'}) "
        "CREATE (a)-[:OWNS {source_id: 'gleif', filing_type: 'RR', direct_or_indirect: 'direct', "
        "since: '2001-07-03', credibility_score: 92}]->(b)")
    from app.claims import KIND_OWNS, record_claim
    record_claim(kind=KIND_OWNS, from_id="lei:MSFT0000000000000001", to_id="lei:ATVI0000000000000001",
                 source_id="gleif", ownership_type="controlling", since="2001-07-03",
                 credibility_score=92, filing_type="RR")
    hi = h.History()
    king = ("ATVI0000000000000001", "KING0000000000000001")
    msft = ("MSFT0000000000000001", "ATVI0000000000000001")
    hi.observe("2021-06-01", {king: ("indirect", "2021-05-11")})
    hi.observe("2026-07-01", {king: ("indirect", "2021-05-11")})
    hi.observe("2026-08-01", {msft: ("direct", "2001-07-03")})
    hi.observe("2026-10-05", {msft: ("direct", "2001-07-03")})
    path = str(tmp_path / "atvi.jsonl.gz")
    h.write_intervals(path, hi)
    return path


def _msft_edge(it_db):
    return it_db.run_command(
        "MATCH (:Entity {id: 'lei:MSFT0000000000000001'})-[r:OWNS]->(:Entity {id: 'lei:ATVI0000000000000001'}) "
        "RETURN r.since AS since, r.since_basis AS basis, r.since_not_before AS floor")


def test_a_start_the_archive_refutes_is_corrected_and_the_claim_keeps_it(it_db, tmp_path):
    path = _activision(it_db, tmp_path)
    assert h.apply_history(path, "gleif", 92)["starts_corrected"] == 1
    assert _msft_edge(it_db) == [{"since": "2026-08-01", "basis": "gleif_first_seen", "floor": "2026-07-01"}]
    claim = it_db.run_command("MATCH (c:Claim {from_id: 'lei:MSFT0000000000000001', "
                              "to_id: 'lei:ATVI0000000000000001'}) RETURN c.since AS since")
    assert claim[0]["since"] == "2001-07-03"                 # what GLEIF says, kept
    assert h.apply_history(path, "gleif", 92)["starts_corrected"] == 0


def test_the_daily_delta_does_not_bring_the_refuted_start_back(it_db, tmp_path):
    from app.scraper.gleif_incremental import _owns_edge_upsert
    h.apply_history(_activision(it_db, tmp_path), "gleif", 92)
    # GLEIF touches Activision's records again, both still stating 2001 /
    # 2018: the direct one would win `since`, the ultimate one `ultimate_since`
    for marker, since in (("direct", "2018-06-15"), ("indirect", "2001-07-03")):
        _owns_edge_upsert("lei:MSFT0000000000000001", "lei:ATVI0000000000000001", "ATVI0000000000000001",
                          marker, "gleif", 92, since=since, recorded="2026-11-01")
    row = it_db.run_command(
        "MATCH (:Entity {id: 'lei:MSFT0000000000000001'})-[r:OWNS]->(:Entity {id: 'lei:ATVI0000000000000001'}) "
        "RETURN r.since AS since, r.ultimate_since AS u")
    assert row == [{"since": "2026-08-01", "u": None}]


def test_a_start_written_meanwhile_is_not_corrected_over(it_db, tmp_path, monkeypatch):
    # the plan saw GLEIF's 2001; by the write another source set 2023-10-13
    path = _activision(it_db, tmp_path)
    it_db.run_command(
        "MATCH (:Entity {id: 'lei:MSFT0000000000000001'})-[r:OWNS]->(:Entity {id: 'lei:ATVI0000000000000001'}) "
        "SET r.since = '2023-10-13'")
    stale = h._rr_edges

    def as_planned(ids):
        edges = stale(ids)
        for rows in edges.values():
            for r in rows:
                r["since"] = "2001-07-03"
        return edges
    monkeypatch.setattr(h, "_rr_edges", as_planned)
    assert h.apply_history(path, "gleif", 92)["starts_corrected"] == 1
    assert _msft_edge(it_db)[0]["since"] == "2023-10-13"
