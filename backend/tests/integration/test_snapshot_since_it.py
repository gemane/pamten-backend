"""List-style SEC filings carry an as-of date, not a start date.

News Corp's FY2026 Exhibit 21 dated all 200 of its subsidiaries 2026 in the
timeline: the writer stored the filing date as `since`. Against a real ArcadeDB
because both halves are writes — the writer's new flag, and the repair of the
edges and claims already written the old way.
"""
import pytest

pytestmark = pytest.mark.integration


def _companies(it_db, *ids):
    for i in ids:
        it_db.run_command("CREATE (:Entity {id: $id, name: $id})", {"id": i})


def _edge(it_db, a, b):
    rows = it_db.run_command(
        "MATCH (:Entity {id:$a})-[r:OWNS]->(:Entity {id:$b}) "
        "RETURN r.since AS since, r.source_date AS asof, r.filing_type AS ft", {"a": a, "b": b})
    return rows[0] if rows else None


class TestTheWriter:
    def test_a_list_filing_keeps_the_date_as_of_only(self, it_db):
        from app.scraper.sec_writer import _upsert_owns_sec
        _companies(it_db, "newscorp", "dowjones")
        _upsert_owns_sec(owner_id="newscorp", owned_id="dowjones", source_id="sec",
                         ownership_type="controlling", file_date="2026-08-07",
                         stake_percent=None, filing_type="EX-21",
                         filing_dates_the_stake=False)
        e = _edge(it_db, "newscorp", "dowjones")
        assert e["since"] is None and e["asof"] == "2026-08-07"
        claim = it_db.run_sql("SELECT since, source_date FROM Claim WHERE from_id = 'newscorp'")
        assert claim and claim[0].get("since") is None and claim[0]["source_date"] == "2026-08-07"

    def test_a_13d_still_dates_the_stake(self, it_db):
        from app.scraper.sec_writer import _upsert_owns_sec
        _companies(it_db, "fund", "target")
        _upsert_owns_sec(owner_id="fund", owned_id="target", source_id="sec",
                         ownership_type="minority", file_date="2026-03-02",
                         stake_percent=6.1, filing_type="SC 13D")
        assert _edge(it_db, "fund", "target")["since"] == "2026-03-02"


class TestTheRepair:
    def _seed(self, it_db):
        _companies(it_db, "nc", "s1", "s2", "s3", "f1")
        rows = [
            ("nc", "s1", "EX-21", "2026-08-07", "2026-08-07"),   # invented → cleared
            ("nc", "s2", "EX-21", "2014-08-14", "2026-08-07"),   # a real earlier start → kept
            ("f1", "s3", "13F", "2026-06-30", "2026-06-30"),     # invented → cleared
            ("f1", "nc", "SC 13D", "2026-03-02", "2026-03-02"),  # a 13D dates its stake → kept
        ]
        for a, b, ft, since, asof in rows:
            it_db.run_command(
                "MATCH (x:Entity {id:$a}), (y:Entity {id:$b}) "
                "CREATE (x)-[:OWNS {filing_type:$ft, since:$since, source_date:$asof}]->(y)",
                {"a": a, "b": b, "ft": ft, "since": since, "asof": asof})
            it_db.run_sql(
                "INSERT INTO Claim SET claim_key = :k, kind = 'owns', from_id = :a, to_id = :b, "
                "filing_type = :ft, since = :since, source_date = :asof",
                {"k": f"owns|{a}|{b}", "a": a, "b": b, "ft": ft, "since": since, "asof": asof})

    def test_dry_run_counts_and_changes_nothing(self, it_db):
        from app.scraper.maintenance import clear_snapshot_since
        self._seed(it_db)
        assert clear_snapshot_since() == {"edges": 2, "claims": 2, "applied": False}
        assert _edge(it_db, "nc", "s1")["since"] == "2026-08-07"

    def test_apply_clears_only_the_invented_dates(self, it_db):
        from app.scraper.maintenance import clear_snapshot_since
        self._seed(it_db)
        assert clear_snapshot_since(apply=True)["applied"] is True
        assert _edge(it_db, "nc", "s1")["since"] is None
        assert _edge(it_db, "nc", "s1")["asof"] == "2026-08-07"      # the as-of date stays
        assert _edge(it_db, "nc", "s2")["since"] == "2014-08-14"
        assert _edge(it_db, "f1", "s3")["since"] is None
        assert _edge(it_db, "f1", "nc")["since"] == "2026-03-02"
        claims = {r["to_id"]: r.get("since") for r in
                  it_db.run_sql("SELECT to_id, since FROM Claim WHERE kind = 'owns'")}
        assert claims == {"s1": None, "s2": "2014-08-14", "s3": None, "nc": "2026-03-02"}
        # running it again finds nothing
        assert clear_snapshot_since() == {"edges": 0, "claims": 0, "applied": False}
