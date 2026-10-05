"""
GLEIF relationships for time travel — full import and daily delta, against a
real ArcadeDB.

The audit's GLEIF findings: ended relationships were dropped by the full
import, re-acquisitions reopened the old edge as one long period, a missing
end date became the run's date, closing re-stamped already-closed edges,
ACTIVE records cleared an end the full import had written, the pair-fold
used the direct start only, and no RR edge had an evidence date.
"""
import json
import zipfile

import pytest

pytestmark = pytest.mark.integration

PARENT, CHILD = "PARENTLEI00000000002", "CHILDLEI000000000001"


def _w(v):
    return {"$": v}


def _rel(status="ACTIVE", start=None, end=None, rtype="IS_DIRECTLY_CONSOLIDATED_BY",
         updated=None, parent=PARENT, child=CHILD):
    rel = {"StartNode": {"NodeID": _w(child), "NodeIDType": _w("LEI")},
           "EndNode": {"NodeID": _w(parent), "NodeIDType": _w("LEI")},
           "RelationshipType": _w(rtype), "RelationshipStatus": _w(status)}
    if start or end:
        period = {"PeriodType": _w("RELATIONSHIP_PERIOD")}
        if start:
            period["StartDate"] = _w(start)
        if end:
            period["EndDate"] = _w(end)
        rel["RelationshipPeriods"] = {"RelationshipPeriod": period}
    rec = {"RelationshipRecord": {"Relationship": rel}}
    if updated:
        rec["RelationshipRecord"]["Registration"] = {"LastUpdateDate": _w(updated)}
    return rec


def _zip(tmp_path, name, rels):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name.replace(".zip", ""), json.dumps({"relations": rels}))
    return str(path)


def _edges(db):
    rows = db.run_command(
        "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) RETURN r.direct_or_indirect AS m, "
        "r.since AS since, r.until AS until, r.until_reason AS reason, r.source_date AS sdate",
        {"p": f"lei:{PARENT}", "c": f"lei:{CHILD}"})
    return sorted(rows, key=lambda r: (r.get("since") or "", r.get("until") or ""))


class TestFullImport:
    def test_an_ended_relationship_is_a_closed_edge_beside_the_current_one(self, it_db, tmp_path):
        from app.scraper.gleif_rr import import_rr_cdf
        res = import_rr_cdf(_zip(tmp_path, "rr.json.zip", [
            _rel("INACTIVE", start="2010-01-01T00:00:00Z", end="2015-06-30T00:00:00Z",
                 updated="2015-07-01T00:00:00Z"),
            _rel("ACTIVE", start="2019-03-01T00:00:00Z", updated="2026-01-04T00:00:00Z")]),
            "gleif-src", 92)
        assert res["ended"] == 1
        assert [(e["since"], e["until"], e["sdate"]) for e in _edges(it_db)] == [
            ("2010-01-01", "2015-06-30", "2015-07-01"), ("2019-03-01", None, "2026-01-04")]
        # the pair's claim keeps the CURRENT relationship, not the old period
        claim = it_db.run_sql("SELECT since, until FROM Claim WHERE kind = 'owns'")[0]
        assert (claim.get("since"), claim.get("until")) == ("2019-03-01", None)

    def test_an_inactive_record_restating_the_current_period_is_not_a_second_edge(self, it_db, tmp_path):
        from app.scraper.gleif_rr import import_rr_cdf
        import_rr_cdf(_zip(tmp_path, "rr.json.zip", [
            _rel("ACTIVE", start="2019-03-01T00:00:00Z"),
            _rel("INACTIVE", start="2019-03-01T00:00:00Z", end="2025-01-01T00:00:00Z")]),
            "gleif-src", 92)
        assert len(_edges(it_db)) == 1

    def test_no_end_date_means_ended_by_the_last_update(self, it_db, tmp_path):
        from app.scraper.gleif_rr import import_rr_cdf
        import_rr_cdf(_zip(tmp_path, "rr.json.zip", [
            _rel("INACTIVE", start="2010-01-01T00:00:00Z", updated="2021-11-05T08:00:00Z")]),
            "gleif-src", 92)
        e = _edges(it_db)[0]
        assert (e["until"], e["reason"]) == ("2021-11-05", "gleif_inactive")

    def test_a_pair_stated_both_ways_starts_at_the_earlier(self, it_db, tmp_path):
        from app.scraper.gleif_rr import import_rr_cdf
        import_rr_cdf(_zip(tmp_path, "rr.json.zip", [
            _rel(start="2020-01-01T00:00:00Z"),
            _rel(start="2005-01-01T00:00:00Z", rtype="IS_ULTIMATELY_CONSOLIDATED_BY")]),
            "gleif-src", 92)
        assert [(e["m"], e["since"]) for e in _edges(it_db)] == [("direct", "2005-01-01")]


class TestDelta:
    def _apply(self, tmp_path, name, rels):
        from app.scraper.gleif_incremental import import_rr_delta
        return import_rr_delta(_zip(tmp_path, name, rels), "gleif-src", 92)

    def test_a_relationship_begun_again_is_a_new_period(self, it_db, tmp_path):
        self._apply(tmp_path, "a.json.zip", [_rel(start="2010-01-01")])
        self._apply(tmp_path, "b.json.zip", [_rel("INACTIVE", end="2019-01-01")])
        self._apply(tmp_path, "c.json.zip", [_rel(start="2024-02-01", updated="2024-02-03T00:00:00Z")])
        assert [(e["since"], e["until"]) for e in _edges(it_db)] == [
            ("2010-01-01", "2019-01-01"), ("2024-02-01", None)]
        # and a later refresh of the current one leaves the old period alone
        self._apply(tmp_path, "d.json.zip", [_rel(start="2024-02-01", updated="2025-01-01T00:00:00Z")])
        assert [(e["since"], e["until"]) for e in _edges(it_db)] == [
            ("2010-01-01", "2019-01-01"), ("2024-02-01", None)]

    def test_the_same_period_turned_active_again_is_reopened(self, it_db, tmp_path):
        self._apply(tmp_path, "a.json.zip", [_rel(start="2010-01-01")])
        self._apply(tmp_path, "b.json.zip", [_rel("INACTIVE", end="2019-01-01")])
        self._apply(tmp_path, "c.json.zip", [_rel(start="2010-01-01")])      # a correction
        assert [(e["since"], e["until"]) for e in _edges(it_db)] == [("2010-01-01", None)]

    def test_no_end_date_is_never_the_run_date_and_a_re_run_does_not_move_it(self, it_db, tmp_path):
        self._apply(tmp_path, "a.json.zip", [_rel(start="2010-01-01")])
        self._apply(tmp_path, "b.json.zip", [_rel("INACTIVE", updated="2022-03-04T10:00:00Z")])
        e = _edges(it_db)[0]
        assert (e["until"], e["reason"]) == ("2022-03-04", "gleif_inactive")
        self._apply(tmp_path, "c.json.zip", [_rel("INACTIVE", updated="2023-08-09T10:00:00Z")])
        assert _edges(it_db)[0]["until"] == "2022-03-04"

    def test_an_active_record_with_an_end_closes_an_existing_edge_with_it(self, it_db, tmp_path):
        self._apply(tmp_path, "a.json.zip", [_rel(start="2010-01-01")])
        self._apply(tmp_path, "b.json.zip", [_rel(start="2010-01-01", end="2023-12-31")])
        assert _edges(it_db)[0]["until"] == "2023-12-31"

    def test_an_active_record_keeps_its_stated_end_and_its_record_date(self, it_db, tmp_path):
        self._apply(tmp_path, "a.json.zip", [_rel(start="2010-01-01", end="2023-12-31",
                                                  updated="2024-01-05T00:00:00Z")])
        e = _edges(it_db)[0]
        assert (e["until"], e["sdate"]) == ("2023-12-31", "2024-01-05")


def test_a_bulk_write_never_blanks_or_postpones_a_founding_date(it_db):
    from app.scraper.bulk_import import _BatchWriter
    it_db.run_command("CREATE (:Entity {id:'lei:X', name:'X', founded:1975, founded_date:'1975-04-04'})")
    for props in ({"founded": None, "founded_date": None, "name": "X Corp"},     # nothing stated
                  {"founded": 1993, "founded_date": "1993-06-01"}):              # a re-registration
        b = _BatchWriter()
        b.entity("lei:X", props)
        b.flush()
    row = it_db.run_command("MATCH (e:Entity {id:'lei:X'}) RETURN e.founded AS y, "
                            "e.founded_date AS d, e.name AS n")[0]
    assert (row["y"], row["d"], row["n"]) == (1975, "1975-04-04", "X Corp")
    b = _BatchWriter()
    b.entity("lei:X", {"founded": 1960, "founded_date": "1960-01-02"})          # an earlier one wins
    b.flush()
    row = it_db.run_command("MATCH (e:Entity {id:'lei:X'}) RETURN e.founded AS y, e.founded_date AS d")[0]
    assert (row["y"], row["d"]) == (1960, "1960-01-02")
    b = _BatchWriter()
    b.entity("lei:NEW", {"name": "New", "founded": 2001, "founded_date": "2001-02-03"})
    b.flush()
    assert it_db.run_command("MATCH (e:Entity {id:'lei:NEW'}) RETURN e.founded AS y")[0]["y"] == 2001
