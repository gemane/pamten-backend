"""Nothing of a dissolved company stays open (app.dissolution, 2026-10-25):
its holdings, the holdings in it and its seats end on `dissolved_on`, the
founders and the edges dated after it excepted — the mirror of the death
rule (tests/test_seats_end_at_death.py)."""
from app import dissolution


class _Db:
    def __init__(self, entity, edges):
        self.entity, self.edges, self.updates = entity, edges, []

    def run_sql(self, sql, params=None, timeout=None):
        if sql.startswith("UPDATE"):
            self.updates.append((sql, params))
            return [{"count": 1}]
        if "FROM Entity WHERE id" in sql:
            return [self.entity] if self.entity else []
        if "FROM Entity WHERE dissolved_on IS NOT NULL" in sql:
            return [self.entity] if self.entity else []
        for key, rows in self.edges.items():
            if f"FROM {key[0]} WHERE until IS NULL AND {key[1]}.id" in sql:
                return rows
        return []


def _patch(monkeypatch, entity, edges):
    import app.db.arcadedb as arcadedb
    db = _Db(entity, edges)
    monkeypatch.setattr(arcadedb, "run_sql", db.run_sql)
    return db


GONE = {"id": "x", "name": "Gone Corp", "dissolved_on": "2023-03-15", "wikidata_id": "Q9"}
EDGES = {
    ("OWNS", "@out"): [{"rid": "#30:1", "since": "2010-01-01"},         # what it owned
                       {"rid": "#30:2", "since": "2024-01-01"}],        # dated after the end: the source's error
    ("OWNS", "@in"): [{"rid": "#30:3", "since": None}],                 # a holding in it
    ("HAS_ROLE", "@in"): [{"rid": "#31:1", "role": "CEO", "since": "2015-01-01"},
                          {"rid": "#31:2", "role": "Founder", "since": None}],
}


class TestEndAtDissolution:
    def test_closes_holdings_both_ways_and_seats_but_not_founders_or_later_edges(self, monkeypatch):
        db = _patch(monkeypatch, GONE, EDGES)
        assert dissolution.end_at_dissolution("x") == {"closed": 3, "founders_kept": 1, "after_dissolution": 1}
        assert [u[0] for u in db.updates] == [
            "UPDATE #30:1 SET until = :d, until_reason = :why, until_source_url = :u",
            "UPDATE #30:3 SET until = :d, until_reason = :why, until_source_url = :u",
            "UPDATE #31:1 SET until = :d, until_reason = :why, until_source_url = :u"]
        assert db.updates[0][1] == {"d": "2023-03-15", "why": "dissolution", "u": "https://www.wikidata.org/wiki/Q9"}

    def test_the_callers_record_is_cited_over_the_wikidata_item(self, monkeypatch):
        db = _patch(monkeypatch, GONE, {("OWNS", "@in"): EDGES[("OWNS", "@in")]})
        dissolution.end_at_dissolution("x", source_url="https://find-and-update.company-information.service.gov.uk/company/1")
        assert db.updates[0][1]["u"] == "https://find-and-update.company-information.service.gov.uk/company/1"

    def test_without_a_source_the_end_is_not_cited(self, monkeypatch):
        db = _patch(monkeypatch, {**GONE, "wikidata_id": None}, {("OWNS", "@in"): EDGES[("OWNS", "@in")]})
        dissolution.end_at_dissolution("x")
        assert db.updates[0][0] == "UPDATE #30:3 SET until = :d, until_reason = :why"

    def test_a_living_company_is_left_alone(self, monkeypatch):
        db = _patch(monkeypatch, {**GONE, "dissolved_on": None}, EDGES)
        assert dissolution.end_at_dissolution("x") == {"closed": 0, "founders_kept": 0, "after_dissolution": 0}
        assert db.updates == []

    def test_a_database_error_is_logged_not_raised(self, monkeypatch):
        import app.db.arcadedb as arcadedb

        def boom(*a, **k):
            raise RuntimeError("down")
        monkeypatch.setattr(arcadedb, "run_sql", boom)
        assert dissolution.end_at_dissolution("x")["closed"] == 0


class TestHeal:
    def test_a_dry_run_counts_and_names_without_writing(self, monkeypatch):
        db = _patch(monkeypatch, GONE, EDGES)
        res = dissolution.heal_ends_at_dissolution(apply=False)
        assert (res["companies"], res["closed"], res["founders_kept"], res["after_dissolution"]) == (1, 3, 1, 1)
        assert res["lines"] == ["Gone Corp († 2023-03-15): 3 closed, 1 founders kept, 1 dated after the dissolution"]
        assert db.updates == [] and res["applied"] is False

    def test_apply_closes_through_the_rule(self, monkeypatch):
        db = _patch(monkeypatch, GONE, EDGES)
        res = dissolution.heal_ends_at_dissolution(apply=True)
        assert res["closed"] == 3 and len(db.updates) == 3 and res["applied"] is True
