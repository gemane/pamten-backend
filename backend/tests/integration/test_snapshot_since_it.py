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
