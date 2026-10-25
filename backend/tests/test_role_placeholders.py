"""`heal_placeholder_roles`: Form 4's "See Remarks" title box reached the graph
as a role; the heal deletes those seats so a re-scrape writes them back with
the role the remarks state. Pure-logic check of what it issues."""
from app import roles


class _Db:
    def __init__(self, edges, claims):
        self.calls = []
        self._edges, self._claims = edges, claims

    def run_sql(self, sql, params=None, timeout=None):
        self.calls.append(sql)
        if sql.startswith("SELECT @out.full_name"):
            return self._edges
        if sql.startswith("SELECT count(*)"):
            return [{"n": self._claims}]
        return []


def _patch(monkeypatch, edges, claims):
    import app.db.arcadedb as arcadedb
    db = _Db(edges, claims)
    monkeypatch.setattr(arcadedb, "run_sql", db.run_sql)
    return db


def test_the_dry_run_counts_names_the_companies_and_deletes_nothing(monkeypatch):
    db = _patch(monkeypatch, [{"person": "Charles C Chang", "company": "BERKSHIRE HATHAWAY INC."},
                              {"person": "Francisco Valim", "company": "GRUPO TELEVISA, S.A.B."},
                              {"person": "Michael J O'sullivan", "company": "BERKSHIRE HATHAWAY INC."}], 3)
    res = roles.heal_placeholder_roles()
    assert res == {"edges": 3, "claims": 3, "applied": False,
                   "companies": ["BERKSHIRE HATHAWAY INC.", "GRUPO TELEVISA, S.A.B."]}
    assert not any(c.startswith("DELETE") for c in db.calls)


def test_apply_deletes_the_edges_and_the_role_claims_case_blind(monkeypatch):
    db = _patch(monkeypatch, [{"person": "x", "company": "Y"}], 1)
    res = roles.heal_placeholder_roles(apply=True)
    assert res["applied"] is True and res["edges"] == 1
    deletes = [c for c in db.calls if c.startswith("DELETE")]
    assert len(deletes) == 2
    assert deletes[0].startswith("DELETE FROM HAS_ROLE WHERE")
    assert deletes[1].startswith("DELETE FROM Claim WHERE kind = 'role' AND")
    for d in deletes:                        # "See Remarks" and "See remarks" alike; a null role left alone
        assert "role.toLowerCase() LIKE 'see remarks%'" in d and "role IS NOT NULL" in d


def test_a_clean_graph_issues_no_delete_and_names_no_company(monkeypatch):
    db = _patch(monkeypatch, [], 0)
    assert roles.heal_placeholder_roles(apply=True) == {"edges": 0, "claims": 0, "companies": [], "applied": True}
    # the deletes still run (they match nothing) — no special case to get wrong
    assert len([c for c in db.calls if c.startswith("DELETE")]) == 2
