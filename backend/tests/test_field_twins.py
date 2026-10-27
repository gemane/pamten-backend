"""One fact, one field (app.field_twins): the merge helper every writer uses."""
from app import field_twins as m


class TestMerge:
    def test_the_scalar_joins_the_list_once(self):
        assert m.merge_nationalities(["CA"], "US") == ["CA", "US"]
        assert m.merge_nationalities(["US", "CA"], "US") == ["US", "CA"]
        assert m.merge_nationalities([], "US") == ["US"]
        assert m.merge_nationalities(None, None) == []

    def test_blanks_and_whitespace_are_dropped(self):
        assert m.merge_nationalities(["", " GB "], "") == ["GB"]


class TestFlags:
    """A flag is true or false, never unset: the six of `schema.FLAGS`."""

    def test_the_declared_flags(self):
        from app.db.schema import FLAGS
        assert set(FLAGS) == {("OWNS", "stale"), ("OWNS", "shortcut"), ("OWNS", "also_ultimate"),
                              ("Entity", "is_nominee"), ("Entity", "on_demand_scraped"),
                              ("Person", "on_demand_scraped")}
        assert ("Entity", "active") not in FLAGS          # null = no register has said


class TestWritersUseTheFold:
    """The rule is enforced where the data enters."""

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
