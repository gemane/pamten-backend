"""The stored-type heal (app.property_types): only the numeric declared
properties, only the rows whose stored type differs, rewritten by @rid as
parameters of the declared shape."""
from app import property_types as m


class TestHeal:
    def _fake(self, mismatched, declared=None):
        """`declared` overrides the catalog's type for (type, prop); by default
        the server declares every convertible property as the code does."""
        issued = []
        catalog = {(t, p): k for t, p, k in m.convertible()}
        catalog.update(declared or {})

        def run_sql(sql, params=None, timeout=None):
            issued.append(sql)
            if sql == "SELECT name, properties FROM schema:types":
                return [{"name": t, "properties": [{"name": p, "type": k} for (tt, p), k in catalog.items()
                                                   if tt == t and k is not None]}
                        for t in {t for t, _ in catalog}]
            for (vtype, prop), rows in mismatched.items():
                if sql.startswith(f"SELECT @rid AS rid, {prop} AS v FROM {vtype} "):
                    return rows
            return []
        scripts = []
        return run_sql, issued, scripts, lambda s, p: scripts.append((s, p)) or []

    def test_only_numeric_declared_properties_are_scanned(self):
        kinds = {k for _, _, k in m.convertible()}
        assert kinds <= {"DOUBLE", "LONG", "INTEGER"} and ("OWNS", "stake_percent", "DOUBLE") in m.convertible()
        assert not any(p in ("is_nominee", "aliases", "stale") for _, p, _ in m.convertible())

    def test_each_mismatched_row_is_rewritten_as_a_parameter_of_the_declared_shape(self, monkeypatch):
        run_sql, issued, scripts, run_script = self._fake({
            ("OWNS", "stake_percent"): [{"rid": "#9:0", "v": 20}, {"rid": "#9:1", "v": 35}],
            ("OWNS", "shares"): [{"rid": "#9:2", "v": 150000000}],
        })
        monkeypatch.setattr(m, "run_sql", run_sql)
        monkeypatch.setattr(m, "run_sqlscript", run_script)
        res = m.heal(apply=True)
        assert res["converted"] == {"OWNS.stake_percent": 2, "OWNS.shares": 1} and res["misdeclared"] == {}
        assert any(s == "SELECT @rid AS rid, stake_percent AS v FROM OWNS WHERE stake_percent IS NOT NULL "
                        "AND stake_percent.type() <> 'DOUBLE'" for s in issued)
        (s1, p1), (s2, p2) = scripts
        # null first: ArcadeDB skips a write whose value equals the stored one
        # (20.0 == 20) and the old type would stay
        assert s1.splitlines() == ["UPDATE #9:0 SET stake_percent = null;", "UPDATE #9:0 SET stake_percent = :v0;",
                                   "UPDATE #9:1 SET stake_percent = null;", "UPDATE #9:1 SET stake_percent = :v1;"]
        assert p1 == {"v0": 20.0, "v1": 35.0} and all(isinstance(v, float) for v in p1.values())
        assert s2.splitlines() == ["UPDATE #9:2 SET shares = null;", "UPDATE #9:2 SET shares = :v0;"]
        assert p2 == {"v0": 150000000.0} and isinstance(p2["v0"], float)   # a count is a DOUBLE

    def test_a_dry_run_counts_and_writes_nothing(self, monkeypatch):
        run_sql, issued, scripts, run_script = self._fake({("Claim", "shares"): [{"rid": "#3:0", "v": 1.0}]})
        monkeypatch.setattr(m, "run_sql", run_sql)
        monkeypatch.setattr(m, "run_sqlscript", run_script)
        res = m.heal(apply=False)
        assert res == {"converted": {"Claim.shares": 1}, "misdeclared": {}, "applied": False} and scripts == []

    def test_a_property_the_server_declares_differently_is_skipped_and_reported(self, monkeypatch):
        # a LONG declaration coerces the DOUBLE rewrite straight back: the heal
        # would report the same rows rewritten on every run
        run_sql, issued, scripts, run_script = self._fake(
            {("OWNS", "shares_outstanding"): [{"rid": "#9:0", "v": 47}], ("OWNS", "shares"): [{"rid": "#9:1", "v": 1}]},
            declared={("OWNS", "shares_outstanding"): "LONG", ("Claim", "voting_shares"): None})
        monkeypatch.setattr(m, "run_sql", run_sql)
        monkeypatch.setattr(m, "run_sqlscript", run_script)
        res = m.heal(apply=True)
        assert res["converted"] == {"OWNS.shares": 1}
        assert res["misdeclared"] == {"OWNS.shares_outstanding": {"server": "LONG", "code": "DOUBLE"},
                                      "Claim.voting_shares": {"server": None, "code": "DOUBLE"}}
        assert not any("shares_outstanding AS v FROM OWNS" in s for s in issued)   # never scanned
        assert [s for s, _ in scripts] == ["UPDATE #9:1 SET shares = null;\nUPDATE #9:1 SET shares = :v0;"]
