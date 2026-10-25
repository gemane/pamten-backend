"""Generational marks (app.generations): what tells a father from a son by
name alone, and the listing of nodes that already hold both."""
from unittest.mock import patch

from app import generations as g


class TestMarks:
    def test_reads_every_spelling_and_normalises_it(self):
        assert g.generation_marks("Thomas S. Murphy, Jr.") == {"jr"}
        assert g.generation_marks("Thomas Murphy, Sr.") == {"sr"}
        assert g.generation_marks("William H. Gates III") == {"iii"}
        assert g.generation_marks("John Smith Junior", "John Smith Senior") == {"jr", "sr"}
        assert g.generation_marks("Gates William H Iii") == {"iii"}

    def test_a_plain_name_carries_none(self):
        assert g.generation_marks("Thomas Murphy") == set()
        assert g.generation_marks(None, "") == set()
        assert g.generation_marks("Ivan IV Jr")                      # a name with marks is not a plain name

    def test_only_two_different_marks_conflict(self):
        assert g.marks_conflict({"sr"}, {"jr"})
        assert g.marks_conflict({"ii"}, {"iii"})
        assert not g.marks_conflict({"iii"}, {"iii"})                 # Gates both sides
        assert not g.marks_conflict(set(), {"jr"})                    # a doubt, not a verdict
        assert not g.marks_conflict(set(), set())


class TestListing:
    ROWS = [{"id": "p1", "full_name": "Thomas Murphy", "aliases": ["Thomas Murphy, Sr.", "Thomas S. Murphy, Jr."],
             "wikidata_id": "Q3525537", "sec_cik": None},
            {"id": "p2", "full_name": "Bill Gates", "aliases": ["William H. Gates III"], "wikidata_id": "Q5284", "sec_cik": None},
            {"id": "p3", "full_name": "Plain Name", "aliases": None, "wikidata_id": None, "sec_cik": "1"}]

    def test_names_only_the_nodes_holding_two_marks(self):
        with patch("app.db.paging.iter_id_pages", lambda label, **kw: iter([self.ROWS])):
            rows = g.mixed_generation_persons()
        assert [(r["id"], r["marks"]) for r in rows] == [("p1", ["jr", "sr"])]
        assert rows[0]["aliases"] == self.ROWS[0]["aliases"] and rows[0]["wikidata_id"] == "Q3525537"
