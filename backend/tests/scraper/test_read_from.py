"""
`read_from` — how surely a value was read off its document.

The vocabulary and its order, how a reading combines, where the grade sits
in the conflict rules (below credibility, above the date), that a claim
carries it, and which grade each Exhibit 21 reader stamps on its rows.
"""
from pathlib import Path

import pytest

from app.claims import best_claim, claim_props, edge_values_from
from app.scraper.edge_schema import (READ_FIELD, READ_FORM, READ_GRADES, READ_LAYOUT, READ_NARRATIVE,
                                     READ_PROSE, READ_TABLE, read_rank, weakest_reading)
from app.scraper.owns_merge import ANSWER_FIELDS, answer_rank, outranks
from app.scraper.sec_ex21 import parse_exhibit

FX = Path(__file__).parent / "fixtures"


class TestTheVocabulary:
    def test_best_first(self):
        assert READ_GRADES == ("field", "table", "form", "layout", "prose", "narrative")
        assert (read_rank("field") > read_rank("table") > read_rank("form") > read_rank("layout")
                > read_rank("prose") > read_rank("narrative") > read_rank(None))

    def test_a_regulators_form_outranks_the_pages_layout(self):
        # the labels on a cover page are the SEC's, numbered and fixed; an
        # indentation is the filer's alone
        assert read_rank(READ_FORM) > read_rank(READ_LAYOUT)

    def test_the_three_text_grades_in_order(self):
        # a form's numbered row, a line shaped like a list item, a sentence
        assert read_rank(READ_FORM) > read_rank(READ_PROSE) > read_rank(READ_NARRATIVE)

    def test_an_unknown_grade_ranks_like_none(self):
        assert read_rank("xml") == read_rank(None) == 0

    @pytest.mark.parametrize("grades,want", [
        ((READ_TABLE, READ_LAYOUT), READ_LAYOUT),
        ((READ_LAYOUT, READ_TABLE), READ_LAYOUT),
        ((READ_FIELD, READ_PROSE, READ_TABLE), READ_PROSE),
        ((READ_FORM, READ_FIELD), READ_FORM),      # a cover page's number under an index's name
        ((READ_PROSE, READ_NARRATIVE), READ_NARRATIVE),
        ((READ_TABLE, None), READ_TABLE),          # an unset part says nothing
        ((None, None), None),
        ((READ_TABLE,), READ_TABLE),
        ((), None),
    ])
    def test_a_value_is_as_surely_read_as_its_least_sure_part(self, grades, want):
        assert weakest_reading(*grades) == want

    def test_the_grade_is_an_answer_field(self):
        # it travels with the source that holds the edge's answer
        assert "read_from" in ANSWER_FIELDS


def _claim(cred, stake=None, read_from=None, date="2026-01-01", source="s"):
    return {"source_id": source, "credibility_score": cred, "stake_percent": stake,
            "read_from": read_from, "source_date": date, "filing_type": "13G/A"}


class TestWhereTheGradeSits:
    def test_below_credibility(self):
        # a filing's sentence (98, prose) still beats a register's field (97):
        # the grade says how surely WE read it, not whose word is better
        assert outranks(_claim(98, 5.0, READ_PROSE), _claim(97, 5.0, READ_FIELD))
        assert not outranks(_claim(97, 5.0, READ_FIELD), _claim(98, 5.0, READ_PROSE))

    def test_among_equals_the_surer_reading_wins(self):
        assert outranks(_claim(98, 5.0, READ_FIELD), _claim(98, 5.0, READ_PROSE))
        assert outranks(_claim(98, 5.0, READ_TABLE), _claim(98, 5.0, READ_LAYOUT))
        assert outranks(_claim(98, 5.0, READ_FORM), _claim(98, 5.0, READ_LAYOUT))
        assert outranks(_claim(98, 5.0, READ_PROSE), _claim(98, 5.0, READ_NARRATIVE))
        assert outranks(_claim(98, 5.0, READ_NARRATIVE), _claim(98, 5.0, None)), "a graded reading beats an ungraded one"

    def test_a_tie_keeps_the_incumbent(self):
        assert not outranks(_claim(98, 5.0, READ_FIELD), _claim(98, 5.0, READ_FIELD))

    def test_a_stated_stake_still_comes_first(self):
        assert outranks(_claim(98, 5.0, READ_PROSE), _claim(98, None, READ_FIELD))

    def test_the_order_in_full(self):
        assert answer_rank(_claim(98, 5.0, READ_TABLE)) == (True, True, 98, read_rank(READ_TABLE))

    def test_best_claim_prefers_the_surer_reading_over_the_newer_date(self):
        old_xml = _claim(98, 5.0, READ_FIELD, date="2025-01-01", source="a")
        new_text = _claim(98, 5.0, READ_PROSE, date="2026-01-01", source="b")
        assert best_claim([new_text, old_xml])["source_id"] == "a"
        # same grade: the newer one, as before
        assert best_claim([_claim(98, 5.0, READ_FIELD, date="2025-01-01", source="a"),
                           _claim(98, 5.0, READ_FIELD, date="2026-01-01", source="b")])["source_id"] == "b"


class TestTheClaim:
    def test_carries_the_grade_and_defaults_to_unset(self):
        base = dict(kind="owns", from_id="a", to_id="b", source_id="s")
        assert claim_props(**base, read_from=READ_TABLE)["read_from"] == "table"
        assert claim_props(**base)["read_from"] is None

    def test_the_winning_claim_hands_the_grade_and_the_filing_type_to_the_edge(self):
        values = edge_values_from([_claim(98, 5.0, READ_LAYOUT, source="a")])
        assert values["read_from"] == "layout" and values["filing_type"] == "13G/A"


HEADER = ("<table><tr><td>Name</td><td>Jurisdiction</td></tr>"
          "<tr><td>Alpha Ltd.</td><td>Bermuda</td></tr></table>")


class TestWhatEachExhibit21ReaderStamps:
    def test_a_cell_under_the_filers_own_header_is_a_table_reading(self):
        assert parse_exhibit(HEADER)[0]["read_from"] == "table"
        apple = parse_exhibit((FX / "apple_ex21.htm").read_text(), "Apple Inc.")
        assert {s["read_from"] for s in apple} == {"table"}

    def test_the_next_page_under_a_carried_header_is_a_layout_reading(self):
        html = HEADER + "<table><tr><td>Beta GmbH</td><td>Germany</td></tr></table>"
        subs = {s["name"]: s["read_from"] for s in parse_exhibit(html)}
        assert subs == {"Alpha Ltd.": "table", "Beta GmbH": "layout"}

    def test_a_headerless_table_is_a_layout_reading(self):
        html = ("<table><tr><td>Alpha Ltd.</td><td>Bermuda</td></tr>"
                "<tr><td>Beta GmbH</td><td>Germany</td></tr></table>")
        assert {s["read_from"] for s in parse_exhibit(html)} == {"layout"}

    def test_a_list_grouped_under_country_rows_is_a_layout_reading(self):
        subs = parse_exhibit((FX / "abinbev_20f_note34.htm").read_text(), "Anheuser-Busch InBev SA/NV")
        assert subs and {s["read_from"] for s in subs} == {"layout"}

    def test_names_under_their_countries_are_a_layout_reading(self):
        subs = parse_exhibit((FX / "bat_ex8_excerpt.htm").read_text(), "British American Tobacco p.l.c.")
        assert subs and {s["read_from"] for s in subs} == {"layout"}

    def test_a_list_written_as_paragraphs_is_a_prose_reading(self):
        # "Name (Jurisdiction)" per paragraph: lines shaped like list items,
        # not sentences — prose, never narrative
        subs = parse_exhibit((FX / "alibaba_ex81_excerpt.htm").read_text(), "Alibaba Group Holding Limited")
        assert subs and {s["read_from"] for s in subs} == {"prose"}


class TestWhatTheOtherTextReadersStamp:
    def test_a_legacy_cover_page_is_a_form_reading(self):
        # pinned in test_sec_edgar (the regex path of fetch_ownership_filings);
        # here only that the constant the writer imports is the right grade
        from app.scraper import sec_edgar
        assert sec_edgar.READ_FORM == "form"
        assert not hasattr(sec_edgar, "READ_PROSE"), "the cover page is a form, not prose"

    def test_an_8k_departure_is_a_narrative_reading(self):
        # pinned in test_runner (the 8-K close's kwargs); here the import
        from app.scraper import runner
        assert runner.READ_NARRATIVE == "narrative"
        assert not hasattr(runner, "READ_PROSE"), "no SEC writer in runner grades anything prose"

    def test_a_parent_column_is_still_a_table_reading(self):
        html = ("<table><tr><td>Name</td><td>Parent</td><td>Jurisdiction</td></tr>"
                "<tr><td>Alpha Ltd.</td><td>Atlas Corp.</td><td>Bermuda</td></tr>"
                "<tr><td>Beta GmbH</td><td>Alpha Ltd.</td><td>Germany</td></tr></table>")
        beta = parse_exhibit(html, "Atlas Corp.")[1]
        assert (beta["parent"], beta["parent_basis"], beta["read_from"]) == ("Alpha Ltd.", "column", "table")
