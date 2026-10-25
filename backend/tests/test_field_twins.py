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


class TestHealFlags:
    """Every flag of `schema.FLAGS` is `false` where it was unset — `stale`
    first, then the five that joined it on 2026-10-25."""

    def test_pages_until_no_unset_flag_is_left(self):
        pages = {("OWNS", "stale"): [[{"rid": "#4:0"}, {"rid": "#4:1"}], []],
                 ("Entity", "is_nominee"): [[{"rid": "#2:7"}], []]}
        issued = []

        def run_sql(sql, params=None, **k):
            issued.append(sql)
            if sql.startswith("SELECT @rid"):
                for (label, col), pg in pages.items():
                    if f"FROM {label} WHERE {col} IS NULL" in sql:
                        return pg.pop(0)
                return []
            return []
        with patch.object(m, "run_sql", run_sql):
            res = m.heal_flags(apply=True)
        assert res["fixed"] == 3 and res["applied"] is True
        assert res["OWNS.stale"] == 2 and res["Entity.is_nominee"] == 1 and res["OWNS.shortcut"] == 0
        assert "UPDATE [#4:0, #4:1] SET stale = false" in issued
        assert "UPDATE [#2:7] SET is_nominee = false" in issued
        assert issued[0] == "SELECT @rid AS rid FROM OWNS WHERE stale IS NULL LIMIT 2000"
        assert "SELECT @rid AS rid FROM Person WHERE on_demand_scraped IS NULL LIMIT 2000" in issued

    def test_every_declared_flag_is_healed(self):
        from app.db.schema import FLAGS
        assert set(FLAGS) == {("OWNS", "stale"), ("OWNS", "shortcut"), ("OWNS", "also_ultimate"),
                              ("Entity", "is_nominee"), ("Entity", "on_demand_scraped"),
                              ("Person", "on_demand_scraped")}
        assert ("Entity", "active") not in FLAGS          # null = no register has said

    def test_a_dry_run_only_counts(self):
        issued = []
        with patch.object(m, "run_sql", lambda s, p=None, **k: issued.append(s) or [{"n": 1274}]):
            res = m.heal_flags(apply=False)
        assert res["fixed"] == 1274 * 6 and res["applied"] is False and res["OWNS.also_ultimate"] == 1274
        assert issued[0] == "SELECT count(*) AS n FROM OWNS WHERE stale IS NULL"
        assert not any(s.startswith("UPDATE") for s in issued)


class TestHealEmptyStrings:
    def test_an_empty_description_is_removed_a_real_one_kept(self):
        calls = []
        with patch.object(m, "run_sql", lambda s, p=None, **k: calls.append(s) or [{"n": 1378}]):
            res = m.heal_empty_strings(apply=True)
        assert res["Person.description"] == 1378
        assert "UPDATE Person REMOVE description WHERE description = ''" in calls

    def test_a_dry_run_only_counts(self):
        calls = []
        with patch.object(m, "run_sql", lambda s, p=None, **k: calls.append(s) or [{"n": 2}]):
            res = m.heal_empty_strings(apply=False)
        assert res["Person.description"] == 2
        assert not any(c.startswith("UPDATE") for c in calls)


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

    def test_a_psc_person_without_a_nationality_has_no_list(self):
        """Absent, not `[]`: the bulk writer sets only the keys in the bag, so
        a record that states no nationality writes no list (2026-10-25)."""
        from app.scraper.companies_house_psc import psc_record
        rec = {"company_number": "01234567", "data": {
            "kind": "individual-person-with-significant-control", "name": "Mr Robert Power",
            "links": {"self": "/company/01234567/persons-with-significant-control/individual/x"},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
        assert "nationalities" not in psc_record(rec, "uk-psc", 97).owner_props

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


class TestHealEmptyLists:
    def _fake(self, counts, calls):
        def run_sql(sql, params=None, timeout=None):
            calls.append(sql)
            if sql.startswith("UPDATE"):
                return [{"count": 1}]
            return [{"n": counts["Person"] if "FROM Person" in sql else counts["Entity"]}]
        return run_sql

    def test_an_empty_list_is_removed_only_where_it_exists(self):
        calls = []
        with patch.object(m, "run_sql", self._fake({"Person": 196, "Entity": 0}, calls)):
            res = m.heal_empty_lists(apply=True)
        assert res == {"applied": True, "Person.nationalities": 196, "Entity.countries": 0}
        updates = [c for c in calls if c.startswith("UPDATE")]
        assert updates == ["UPDATE Person REMOVE nationalities WHERE nationalities IS NOT NULL AND nationalities.size() = 0"]
        # `.size()` reads 0 on an absent property too: the filter must say IS NOT NULL
        assert all("IS NOT NULL AND" in c for c in calls)

    def test_the_dead_keys_are_the_dropped_declarations(self):
        from app.db import schema
        from app.scraper.edge_schema import OWNS_PROPS, RELATED_TO_PROPS
        declared = schema.all_properties()
        for label, col in m.DEAD_KEYS:
            assert (label, col) not in declared, f"{label}.{col} is still declared"
        assert "file_date" not in OWNS_PROPS and "credibility_score" not in RELATED_TO_PROPS

    def test_dead_keys_are_removed_from_every_row_and_counted_first(self):
        calls = []
        def run_sql(sql, params=None, timeout=None):
            calls.append(sql)
            return [{"n": 3}] if sql.startswith("SELECT") else [{"count": 1}]
        with patch.object(m, "run_sql", run_sql):
            res = m.heal_dead_keys(apply=True)
        assert res == {"applied": True, "OWNS.file_date": 3, "Person.updated_at": 3,
                       "RELATED_TO.credibility_score": 3}
        assert [c for c in calls if c.startswith("UPDATE")] == [
            "UPDATE OWNS REMOVE file_date", "UPDATE Person REMOVE updated_at",
            "UPDATE RELATED_TO REMOVE credibility_score"]
        with patch.object(m, "run_sql", run_sql):
            calls.clear()
            m.heal_dead_keys(apply=False)
        assert not [c for c in calls if c.startswith("UPDATE")]

    def test_undated_role_claims_are_dated_by_their_first_sighting(self):
        calls = []
        def run_sql(sql, params=None, timeout=None):
            calls.append(sql)
            return [{"n": 290}] if sql.startswith("SELECT") else [{"count": 290}]
        with patch.object(m, "run_sql", run_sql):
            assert m.heal_claim_dates(apply=True) == {"applied": True, "claims": 290}
        update = [c for c in calls if c.startswith("UPDATE")]
        assert len(update) == 1 and "source_date = first_seen_at.left(10)" in update[0]
        assert "kind = 'role' AND source_date IS NULL" in update[0]

    def test_a_dry_run_counts_and_writes_nothing(self):
        calls = []
        with patch.object(m, "run_sql", self._fake({"Person": 2, "Entity": 4}, calls)):
            res = m.heal_empty_lists(apply=False)
        assert res["Person.nationalities"] == 2 and res["Entity.countries"] == 4
        assert not any(c.startswith("UPDATE") for c in calls)

    def test_heal_all_runs_every_model_gap_heal(self):
        names = ["nationalities", "countries", "person_sources", "flags", "empty_lists"]
        with patch.object(m, "heal_nationalities", lambda a: "n"), patch.object(m, "heal_countries", lambda a: "c"), \
             patch.object(m, "heal_person_sources", lambda a: "p"), patch.object(m, "heal_flags", lambda a: "s"), \
             patch.object(m, "heal_empty_lists", lambda a: "e"), patch.object(m, "heal_empty_strings", lambda a: "es"), \
             patch.object(m, "heal_dead_keys", lambda a: "d"), patch.object(m, "heal_claim_dates", lambda a: "cd"), \
             patch.object(m, "heal_address_keys", lambda a: "ak"), patch.object(m, "heal_person_aliases", lambda a: "pa"), \
             patch("app.scraper.run_log.heal_placeholders", lambda a: "r"), \
             patch("app.roles.heal_seat_claim_filings", lambda a: "f"):
            res = m.heal_all(apply=False)
        assert list(res) == names + ["run_log", "seat_filings", "dead_keys", "claim_dates", "address_keys",
                                     "person_aliases", "empty_strings"]
        assert res["person_aliases"] == "pa" and res["empty_strings"] == "es" and res["flags"] == "s"
        assert res["run_log"] == "r" and res["seat_filings"] == "f"
        assert res["dead_keys"] == "d" and res["claim_dates"] == "cd"


class TestHealAddressKeys:
    """`registered_address` → `address_normalized`: GLEIF's rows carried the
    key, the Companies House rows the readable string (and company-data rows
    no `address` at all)."""
    def _run(self, rows, apply=True):
        scripts = []
        with patch("app.db.paging.iter_id_pages", lambda vtype, columns="", **kw: iter([rows])), \
             patch.object(m, "run_sqlscript", lambda script, params=None: scripts.append((script, params))):
            res = m.heal_address_keys(apply=apply)
        return res, scripts

    def test_a_gleif_row_keeps_its_key_under_the_new_name_and_loses_the_old(self):
        rows = [{"id": "lei:1", "rid": "#1:1", "address": "1 Example Street, London, EC1A 1BB, GB",
                 "registered_address": "1 example street london ec1a 1bb gb", "address_normalized": None}]
        res, scripts = self._run(rows)
        assert res == {"applied": True, "scanned": 1, "keyed": 1, "addresses_moved": 0, "removed": 1}
        script, params = scripts[0]
        assert script == "UPDATE #1:1 SET address_normalized = :address_normalized_0 REMOVE registered_address"
        assert params == {"address_normalized_0": "1 example street london ec1a 1bb gb"}

    def test_a_readable_value_found_only_in_the_old_field_moves_to_address(self):
        rows = [{"id": "gb-coh:1", "rid": "#1:2", "address": None,
                 "registered_address": "1 HIGH STREET, HARROGATE, HG1 1AA", "address_normalized": None}]
        res, scripts = self._run(rows)
        assert (res["keyed"], res["addresses_moved"], res["removed"]) == (1, 1, 1)
        script, params = scripts[0]
        assert "address = :address_0" in script and "REMOVE registered_address" in script
        assert params == {"address_0": "1 HIGH STREET, HARROGATE, HG1 1AA",
                          "address_normalized_0": "1 high street harrogate hg1 1aa"}

    def test_a_row_already_keyed_and_one_without_an_address_are_left_alone(self):
        rows = [{"id": "a", "rid": "#1:3", "address": "X Street, Town", "registered_address": None,
                 "address_normalized": "x street town"},
                {"id": "b", "rid": "#1:4", "address": None, "registered_address": None, "address_normalized": None}]
        res, scripts = self._run(rows)
        assert (res["keyed"], res["removed"]) == (0, 0) and scripts == []

    def test_a_dry_run_counts_and_writes_nothing(self):
        rows = [{"id": "lei:1", "rid": "#1:1", "address": "A, B", "registered_address": "a b", "address_normalized": None}]
        res, scripts = self._run(rows, apply=False)
        assert res["keyed"] == 1 and res["applied"] is False and scripts == []


class TestHealPersonAliases:
    def test_copies_the_list_then_removes_the_old_key(self):
        calls = []
        def run_sql(sql, params=None, timeout=None):
            calls.append(sql)
            return [{"n": 911}] if sql.startswith("SELECT") else [{"count": 911}]
        with patch.object(m, "run_sql", run_sql):
            assert m.heal_person_aliases(apply=True) == {"applied": True, "persons": 911}
        updates = [c for c in calls if c.startswith("UPDATE")]
        assert updates == ["UPDATE Person SET aliases = alias WHERE alias IS NOT NULL AND aliases IS NULL",
                           "UPDATE Person REMOVE alias"]
        with patch.object(m, "run_sql", run_sql):
            calls.clear()
            m.heal_person_aliases(apply=False)
        assert not [c for c in calls if c.startswith("UPDATE")]
