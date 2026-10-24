"""One fact, one field (app.field_twins): the merge helpers every writer uses
and the heals that bring an older database to the rule."""
from unittest.mock import patch

from app import field_twins as m


class TestMerge:
    def test_the_scalar_joins_the_list_once(self):
        assert m.merge_nationalities(["CA"], "US") == ["CA", "US"]
        assert m.merge_nationalities(["US", "CA"], "US") == ["US", "CA"]
        assert m.merge_nationalities([], "US") == ["US"]
        assert m.merge_nationalities(None, None) == []

    def test_blanks_and_whitespace_are_dropped(self):
        assert m.merge_nationalities(["", " GB "], "") == ["GB"]
        assert m.merge_countries(["US", None], None) == ["US"]

    def test_countries_is_the_same_fold(self):
        assert m.merge_countries(["GB"], "US") == ["GB", "US"]


def _pages(rows, label_filter=None):
    def fake(label, **kw):
        if label_filter and label != label_filter:
            return iter([])
        return iter([rows])
    return fake


class TestHealNationalities:
    ROWS = [
        {"id": "p1", "rid": "#1:0", "nationality": "US", "nationalities": []},          # the PSC/SEC shape
        {"id": "p2", "rid": "#1:1", "nationality": "US", "nationalities": ["US", "CA"]},  # the Wikidata shape
        {"id": "p3", "rid": "#1:2", "nationality": "", "nationalities": ["GB"]},        # an empty scalar
        {"id": "p4", "rid": "#1:3", "nationality": None, "nationalities": ["DE"]},        # already healed (SQL projects the absent property as null)
    ]

    def test_folds_the_scalar_and_drops_it_everywhere_it_exists(self):
        scripts = []
        with patch("app.db.paging.iter_id_pages", _pages(self.ROWS)), \
             patch.object(m, "run_sqlscript", lambda s, p: scripts.append((s, p))):
            res = m.heal_nationalities(apply=True)
        assert res == {"scanned": 4, "folded": 1, "dropped": 3, "applied": True}
        (script, params), = scripts
        assert script.splitlines() == [
            "UPDATE #1:0 SET nationalities = :n0 REMOVE nationality;",
            "UPDATE #1:1 SET nationalities = :n1 REMOVE nationality;",
            "UPDATE #1:2 SET nationalities = :n2 REMOVE nationality;",
        ]
        assert params == {"n0": ["US"], "n1": ["US", "CA"], "n2": ["GB"]}

    def test_a_dry_run_counts_and_writes_nothing(self):
        scripts = []
        with patch("app.db.paging.iter_id_pages", _pages(self.ROWS)), \
             patch.object(m, "run_sqlscript", lambda s, p: scripts.append((s, p))):
            res = m.heal_nationalities(apply=False)
        assert res["folded"] == 1 and res["dropped"] == 3 and scripts == []


class TestHealCountries:
    ROWS = [
        {"id": "e1", "rid": "#2:0", "country": "US", "countries": []},
        {"id": "e2", "rid": "#2:1", "country": "NL", "countries": ["NL", "GB"]},
        {"id": "e3", "rid": "#2:2", "country": "DE"},                 # no list: nothing to put it into
        {"id": "e4", "rid": "#2:3", "countries": ["FR"]},             # no country
    ]

    def test_only_a_list_missing_its_country_is_rewritten(self):
        scripts = []
        with patch("app.db.paging.iter_id_pages", _pages(self.ROWS)), \
             patch.object(m, "run_sqlscript", lambda s, p: scripts.append((s, p))):
            res = m.heal_countries(apply=True)
        assert res == {"scanned": 4, "fixed": 1, "applied": True}
        assert scripts == [("UPDATE #2:0 SET countries = :c0;", {"c0": ["US"]})]


class TestHealPersonSources:
    ROWS = [
        {"id": "chpsc:01234567:abc", "rid": "#3:0"},
        {"id": "chpsc:07654321:def", "rid": "#3:1", "source_id": "uk-psc"},
        {"id": "9f0c-uuid", "rid": "#3:2"},                          # not a PSC person: not ours to guess
    ]

    def test_only_source_less_psc_persons_are_stamped_with_the_register(self):
        issued = []
        with patch("app.db.paging.iter_id_pages", _pages(self.ROWS)), \
             patch.object(m, "run_sql", lambda s, p=None, **k: issued.append((s, p)) or []):
            res = m.heal_person_sources(apply=True)
        assert res["stamped"] == 1 and res["source_id"] == "uk-psc"
        assert issued == [("UPDATE [#3:0] SET source_id = :sid", {"sid": "uk-psc"})]


class TestHealStaleFlags:
    def test_pages_until_no_unset_flag_is_left(self):
        pages = [[{"rid": "#4:0"}, {"rid": "#4:1"}], []]
        issued = []

        def run_sql(sql, params=None, **k):
            issued.append(sql)
            if sql.startswith("SELECT @rid"):
                return pages.pop(0)
            return []
        with patch.object(m, "run_sql", run_sql):
            res = m.heal_stale_flags(apply=True)
        assert res == {"fixed": 2, "applied": True}
        assert "UPDATE [#4:0, #4:1] SET stale = false" in issued
        assert issued[0] == "SELECT @rid AS rid FROM OWNS WHERE stale IS NULL LIMIT 2000"

    def test_a_dry_run_only_counts(self):
        issued = []
        with patch.object(m, "run_sql", lambda s, p=None, **k: issued.append(s) or [{"n": 1274}]):
            res = m.heal_stale_flags(apply=False)
        assert res == {"fixed": 1274, "applied": False}
        assert issued == ["SELECT count(*) AS n FROM OWNS WHERE stale IS NULL"]


class TestWritersUseTheFold:
    """The rule is enforced where the data enters, not only by the heal."""

    def test_the_psc_person_carries_the_list_and_its_source(self):
        from app.scraper.companies_house_psc import psc_record
        rec = {"company_number": "01234567", "data": {
            "kind": "individual-person-with-significant-control",
            "name": "Mrs Jean Carol Randle", "nationality": "British",
            "name_elements": {"forename": "Jean", "surname": "Randle"},
            "links": {"self": "/company/01234567/persons-with-significant-control/individual/abc"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        mp = psc_record(rec, "uk-psc", 97)
        assert mp.owner_props["nationalities"] == ["GB"] and "nationality" not in mp.owner_props
        assert mp.owner_props["source_id"] == "uk-psc"
        assert mp.edge_props["stale"] is False

    def test_a_psc_person_without_a_nationality_has_an_empty_list(self):
        from app.scraper.companies_house_psc import psc_record
        rec = {"company_number": "01234567", "data": {
            "kind": "individual-person-with-significant-control", "name": "Mr Robert Power",
            "links": {"self": "/company/01234567/persons-with-significant-control/individual/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        assert psc_record(rec, "uk-psc", 97).owner_props["nationalities"] == []

    def test_no_writer_names_the_scalar_any_more(self):
        import pathlib
        import re
        app = pathlib.Path(__file__).resolve().parents[1] / "app"
        # a Cypher map or SET that writes the scalar (a reader's dict key, like
        # Wikidata's CEO row, is an input the runner folds into the list)
        writes = re.compile(r"""(?<![a-z_."'])nationality\s*:\s*[$']|\.nationality\s*=[^=]""")
        offenders = [str(p.relative_to(app)) for p in app.rglob("*.py")
                     if p.name != "field_twins.py" and writes.search(p.read_text())]
        assert offenders == [], offenders
