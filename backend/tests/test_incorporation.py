"""The founded / incorporated_on split (app.incorporation, 2026-10-24) and the
heal for a database written before it."""
from app import incorporation as m


class TestDecide:
    def test_a_register_only_node_is_renamed_and_loses_the_copied_year(self):
        assert m._decide({"founded_date": "1896-07-20", "founded": 1896}) == ("1896-07-20", True)

    def test_a_wikidata_node_keeps_its_founded_even_when_it_matches(self):
        assert m._decide({"founded_date": "1896-07-20", "founded": 1896, "wikidata_id": "Q1"}) == ("1896-07-20", False)

    def test_a_different_year_is_a_statement_of_its_own(self):
        assert m._decide({"founded_date": "1896-07-20", "founded": 1690}) == ("1896-07-20", False)

    def test_already_renamed_nodes_carry_nothing_over(self):
        assert m._decide({"incorporated_on": "2013-03-05", "founded": 2013}) == (None, True)
        assert m._decide({"incorporated_on": "2013-03-05", "founded": 2013, "wikidata_id": "Q2"}) == (None, False)
        assert m._decide({"incorporated_on": "2013-03-05"}) == (None, False)

    def test_nothing_known_nothing_done(self):
        assert m._decide({"founded": 1990}) == (None, False)
        assert m._decide({}) == (None, False)


class TestHeal:
    PAGE = [{"id": "a", "rid": "#1:0", "founded_date": "1896-07-20", "founded": 1896},               # rename + clear
            {"id": "b", "rid": "#1:1", "founded_date": "1990-01-01", "founded": 1850, "wikidata_id": "Q"},  # rename only
            {"id": "c", "rid": "#1:2", "incorporated_on": "2013-03-05", "founded": 2013},          # clear only
            {"id": "d", "rid": "#1:3", "founded": 1999},                                           # untouched
            {"id": "e", "rid": "#1:4", "founded_date": "2001-02-03", "incorporated_on": "2001-02-03"}]  # drop the old field only

    def test_one_statement_per_touched_node_by_rid(self, monkeypatch):
        scripts = []
        monkeypatch.setattr("app.db.paging.iter_id_pages", lambda label, **k: iter([self.PAGE]))
        monkeypatch.setattr(m, "run_sqlscript", lambda s, p: scripts.append((s, p)) or [])
        res = m.heal(apply=True)
        assert (res["scanned"], res["renamed"], res["dropped_old_field"], res["cleared_founded"], res["written"]) == (5, 2, 3, 2, 4)
        (script, params), = scripts
        assert script.splitlines() == [
            "UPDATE #1:0 SET incorporated_on = :v0 REMOVE founded_date, founded;",
            "UPDATE #1:1 SET incorporated_on = :v1 REMOVE founded_date;",
            "UPDATE #1:2 REMOVE founded;",
            "UPDATE #1:4 REMOVE founded_date;",
        ]
        assert params == {"v0": "1896-07-20", "v1": "1990-01-01"}

    def test_a_dry_run_counts_and_writes_nothing(self, monkeypatch):
        monkeypatch.setattr("app.db.paging.iter_id_pages", lambda label, **k: iter([self.PAGE]))
        monkeypatch.setattr(m, "run_sqlscript", lambda s, p: (_ for _ in ()).throw(AssertionError("wrote")))
        res = m.heal(apply=False)
        assert res["renamed"] == 2 and res["written"] == 0 and res["applied"] is False
