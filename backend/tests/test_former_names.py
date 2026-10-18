"""app.former_names — a company's former names with the day each ended."""
from app.former_names import entry, merge, names, parse, rename_events


class TestEntryAndParse:
    def test_round_trip(self):
        assert entry("Facebook Inc", "2021-10-27") == "Facebook Inc|2021-10-27"
        assert parse("Facebook Inc|2021-10-27") == ("Facebook Inc", "2021-10-27")

    def test_edgar_timestamp_is_cut_to_the_day(self):
        assert entry("Facebook Inc", "2021-10-27T04:00:00.000Z") == "Facebook Inc|2021-10-27"

    def test_month_and_year_precision_survive(self):
        assert parse(entry("X Ltd", "2021-10")) == ("X Ltd", "2021-10")
        assert parse(entry("X Ltd", "2021")) == ("X Ltd", "2021")

    def test_no_date_and_garbage_date(self):
        assert entry("X Ltd", None) == "X Ltd|"
        assert entry("X Ltd", "27/10/2021") == "X Ltd|"
        assert parse("X Ltd|") == ("X Ltd", None)
        assert parse("X Ltd|yesterday") == ("X Ltd", None)

    def test_a_pipe_inside_the_name_survives(self):
        assert parse(entry("A|B Holdings", "2020-01-01")) == ("A|B Holdings", "2020-01-01")

    def test_bare_name_parses_undated(self):
        assert parse("Facebook Inc") == ("Facebook Inc", None)

    def test_empty_name_is_nothing(self):
        assert entry("", "2020-01-01") is None
        assert entry("  ", None) is None


class TestNames:
    def test_plain_names_in_order_deduped(self):
        assert names(["B Ltd|2020-01-01", "A Ltd|", "b ltd|2019-01-01", "C Ltd"]) == ["B Ltd", "A Ltd", "C Ltd"]

    def test_none(self):
        assert names(None) == []


class TestMerge:
    def test_union_keeps_existing_order_first(self):
        assert merge(["A|2020-01-01"], ["B|2021-01-01", "A|2020-01-01"]) == ["A|2020-01-01", "B|2021-01-01"]

    def test_a_date_fills_in_an_undated_name_either_way_round(self):
        assert merge(["Facebook Inc|"], ["Facebook Inc|2021-10-27"]) == ["Facebook Inc|2021-10-27"]
        assert merge(["Facebook Inc|2021-10-27"], ["facebook inc"]) == ["Facebook Inc|2021-10-27"]

    def test_the_same_name_with_two_dates_is_two_renames(self):
        # a company renamed away and back again
        assert merge(["A|2010-01-01"], ["A|2020-01-01"]) == ["A|2010-01-01", "A|2020-01-01"]

    def test_bare_names_are_taken_and_dated_entries_dedupe_exactly(self):
        assert merge(None, ["Old Co", "Old Co|2001-01-01", "OLD CO|2001-01-01"]) == ["Old Co|2001-01-01"]

    def test_empty(self):
        assert merge(None, None) == []
        assert merge(["A|2020-01-01"], None) == ["A|2020-01-01"]


class TestRenameEvents:
    FORMER = ["BRIGHTSTAR 20:20 UK LIMITED|2021-08-20", "20:20 MOBILE 1 LIMITED|2013-11-27"]

    def test_oldest_first_each_naming_what_followed(self):
        evs = rename_events("LIKEWIZE SERVICES UK LIMITED", self.FORMER)
        assert [(e["since"], e["former_name"], e["new_name"]) for e in evs] == [
            ("2013-11-27", "20:20 MOBILE 1 LIMITED", "BRIGHTSTAR 20:20 UK LIMITED"),
            ("2021-08-20", "BRIGHTSTAR 20:20 UK LIMITED", "LIKEWIZE SERVICES UK LIMITED"),
        ]
        assert all(e["kind"] == "renamed" and e["until"] is None and e["active"] is False for e in evs)

    def test_undated_names_have_no_place_on_a_timeline(self):
        assert rename_events("Now Ltd", ["Then Ltd|", "Then Ltd"]) == []

    def test_a_former_name_equal_to_the_current_one_says_nothing(self):
        assert rename_events("Acme Ltd", ["ACME LTD|2020-01-01"]) == []

    def test_no_current_name_leaves_the_last_successor_open(self):
        assert rename_events(None, ["Old|2020-01-01"])[0]["new_name"] is None

    def test_nothing(self):
        assert rename_events("X", None) == []
