"""EDGAR's bulk name list as the person lookup (findings item 135)."""
import os
from unittest.mock import MagicMock, patch

import pytest

from app.scraper import sec_client, sec_lookup, sec_names

# the list's shape: four lines without a name, companies and people, a former
# name of one filer, latin-1 bytes
RAW = (
    ":0001003197:\n"
    ":0001036125:\n"
    "COOK TIMOTHY D:0001214156:\n"
    "COOK TIMOTHY DEVERE:0001902974:\n"
    "COOKSON GROUP PLC:0000900001:\n"
    "DENHOLM ROBYN M:0001242782:\n"
    "GATES BILL & MELINDA FOUNDATION:0001166559:\n"
    "GATES WILLIAM H III:0000902012:\n"
    "MORRIS MARIA R:0001423154:\n"
    "MORRIS MARIANNE:0001383376:\n"
    "MUSK ELON:0001494730:\n"
    "MUSK ELON R:1494730:\n"
    "NU\xd1EZ JOS\xc9:0000000077:\n"
    "O'BRIEN, JOHN J. JR:0000000088:\n"
    "apple inc.:0000320193:\n"
).encode("latin-1") + b"".join(b"ZZ FILLER %04d INC:%010d:\n" % (i, i) for i in range(1100))


@pytest.fixture
def index(tmp_path):
    return sec_names.index_path(str(tmp_path), download=lambda: RAW)


def test_the_index_is_sorted_normalised_and_without_the_nameless_lines():
    lines = sec_names.build_index(RAW).decode().splitlines()
    assert lines == sorted(lines, key=str.encode) and len(lines) == 1113      # "MUSK ELON R" is another name
    assert "APPLE INC\t0000320193" in lines and "O'BRIEN JOHN J JR\t0000000088" in lines
    assert "MUSK ELON R\t0001494730" in lines                                 # the CIK padded
    assert not any(line.startswith("\t") for line in lines)


@pytest.mark.parametrize("prefix, names", [
    ("cook timothy", ["COOK TIMOTHY D", "COOK TIMOTHY DEVERE"]),
    ("Cook Tim", ["COOK TIMOTHY D", "COOK TIMOTHY DEVERE"]),              # a prefix, as EDGAR's search matches
    ("COOK", ["COOK TIMOTHY D", "COOK TIMOTHY DEVERE", "COOKSON GROUP PLC"]),
    ("Morris Maria", ["MORRIS MARIA R", "MORRIS MARIANNE"]),
    ("Musk Elon", ["MUSK ELON", "MUSK ELON R"]),                          # the name itself, and what goes on
    ("Apple Inc.", ["APPLE INC"]),
    ("O\'Brien, John", ["O'BRIEN JOHN J JR"]),
    ("Nu\xf1ez", ["NU\xd1EZ JOS\xc9"]),
    ("AA", []), ("ZZZ", []), ("", []),                                     # before the first, after the last
])
def test_names_starting(index, prefix, names):
    assert [n for n, _ in sec_names.names_starting(index, prefix)] == names


def test_every_name_of_the_index_is_found_by_itself(index):
    # the binary search over the file's bytes: first line, last line, each between
    for line in open(index, encoding="utf-8").read().splitlines():
        name, cik = line.split("\t")
        assert (name, cik) in sec_names.names_starting(index, name)


@pytest.mark.parametrize("name, ciks", [
    ("Robyn Denholm", ["0001242782"]),                # surname first, as EDGAR names a person
    ("DENHOLM ROBYN M", ["0001242782"]),              # an SEC-styled name is found as given
    ("Timothy Cook", ["0001214156", "0001902974"]),   # namesakes are all candidates
    ("Elon Musk", ["0001494730"]),                    # two names of one filer: one candidate
    ("Maria Morris", ["0001423154", "0001383376"]),   # Marianne too: the issuer check decides
    ("Tim Cook", ["0001214156", "0001902974"]),       # the short form of a given name
    ("Cook", ["0001214156", "0001902974"]),           # Cookson Group plc is no person
    ("Bill Gates", []),                               # the foundation is no person
    ("Thomas S. Murphy, Jr.", []),
])
def test_person_ciks(index, name, ciks):
    assert sec_names.person_ciks(sec_lookup._name_words(name), 5, index) == ciks


def test_person_candidates_are_cut_to_the_limit(index):
    assert sec_names.person_ciks(["Timothy", "Cook"], 1, index) == ["0001214156"]


def test_no_list_is_none_not_nobody():
    from app.config import settings
    assert settings.SEC_NAME_LIST_DIR == ""                # the suite keeps it off (conftest)
    assert sec_names.index_path() is None
    assert sec_names.person_ciks(["Robyn", "Denholm"], 5) is None


class TestRefresh:
    def test_a_fresh_index_is_not_downloaded_again(self, tmp_path):
        download = MagicMock(return_value=RAW)
        first = sec_names.index_path(str(tmp_path), download=download)
        assert sec_names.index_path(str(tmp_path), download=download) == first and download.call_count == 1

    def test_an_old_index_is_refreshed(self, tmp_path):
        download = MagicMock(return_value=RAW)
        path = sec_names.index_path(str(tmp_path), download=download)
        later = os.path.getmtime(path) + sec_names.MAX_AGE_HOURS * 3600 + 1
        sec_names.index_path(str(tmp_path), download=download, now=lambda: later)
        assert download.call_count == 2

    def test_a_failed_refresh_keeps_the_index_at_hand_and_waits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sec_names, "_last_failure", 0.0)
        path = sec_names.index_path(str(tmp_path), download=lambda: RAW)
        before = open(path, "rb").read()
        later = os.path.getmtime(path) + sec_names.MAX_AGE_HOURS * 3600 + 1
        down = MagicMock(side_effect=OSError("down"))
        assert sec_names.index_path(str(tmp_path), download=down, now=lambda: later) == path
        assert sec_names.index_path(str(tmp_path), download=down, now=lambda: later + 60) == path
        assert down.call_count == 1 and open(path, "rb").read() == before          # no retry inside the pause
        sec_names.index_path(str(tmp_path), download=down, now=lambda: later + sec_names.RETRY_AFTER_SECONDS + 1)
        assert down.call_count == 2
        assert [f for f in os.listdir(tmp_path)] == [sec_names.INDEX_FILE]          # no temp file left

    def test_a_failed_first_download_is_no_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sec_names, "_last_failure", 0.0)
        assert sec_names.index_path(str(tmp_path), download=MagicMock(side_effect=OSError("down"))) is None

    def test_an_error_page_is_not_taken_for_the_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sec_names, "_last_failure", 0.0)
        assert sec_names.index_path(str(tmp_path), download=lambda: b"<html>Request Rate Threshold Exceeded</html>") is None
        assert os.listdir(tmp_path) == []


class TestTheLookupAsksTheListFirst:
    def test_the_list_gives_more_candidates_than_the_search(self, index):
        with patch.object(sec_names, "person_ciks", return_value=[]) as listed:
            sec_lookup._lookup_person_ciks("Robyn Denholm")
        listed.assert_called_once_with(["Robyn", "Denholm"], sec_lookup.LIST_PERSON_CANDIDATES)
        assert sec_lookup.LIST_PERSON_CANDIDATES > sec_lookup.MAX_PERSON_CANDIDATES

    def test_with_a_list_no_request_is_sent(self, index):
        with patch.object(sec_names, "index_path", return_value=index), \
             patch.object(sec_client, "_response") as response:
            assert sec_lookup._lookup_person_ciks("Robyn Denholm") == ["0001242782"]
            assert sec_lookup._lookup_person_ciks("Bill Gates") == []        # nobody listed is an answer too
        response.assert_not_called()

    def test_without_a_list_the_live_search_is_asked(self):
        with patch.object(sec_names, "index_path", return_value=None), \
             patch.object(sec_lookup, "_search_person_ciks", return_value=["0001242782"]) as search:
            assert sec_lookup._lookup_person_ciks("Robyn M. Denholm") == ["0001242782"]
        search.assert_called_once_with(["Robyn", "M.", "Denholm"])


class TestTheCommand:
    def _run(self, tmp_path, download, force=False, directory=True):
        import types
        import manage
        from app.config import settings
        with patch.object(settings, "SEC_NAME_LIST_DIR", str(tmp_path) if directory else ""), \
             patch.object(sec_names, "_download", download):
            manage.cmd_sec_name_list(types.SimpleNamespace(force=force))

    def test_it_downloads_and_says_how_many_names(self, tmp_path, capsys):
        self._run(tmp_path, lambda: RAW)
        assert "1,113 names downloaded" in capsys.readouterr().out
        assert sec_names.count(os.path.join(tmp_path, sec_names.INDEX_FILE)) == 1113

    def test_a_list_of_today_is_kept_unless_forced(self, tmp_path, capsys):
        download = MagicMock(return_value=RAW)
        self._run(tmp_path, download)
        self._run(tmp_path, download)
        assert download.call_count == 1 and "kept" in capsys.readouterr().out
        self._run(tmp_path, download, force=True)
        assert download.call_count == 2

    def test_a_failed_download_exits_1_and_keeps_the_index(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(sec_names, "_last_failure", 0.0)
        self._run(tmp_path, lambda: RAW)
        with pytest.raises(SystemExit) as exit_:
            self._run(tmp_path, MagicMock(side_effect=OSError("down")), force=True)
        assert exit_.value.code == 1 and "the index at hand is kept" in capsys.readouterr().out
        assert sec_names.count(os.path.join(tmp_path, sec_names.INDEX_FILE)) == 1113

    def test_without_a_directory_it_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exit_:
            self._run(tmp_path, lambda: RAW, directory=False)
        assert exit_.value.code == 1 and "SEC_NAME_LIST_DIR" in capsys.readouterr().out


# ── a company's exact name (findings item 135, the companies part) ───────────
COMPANIES = (
    "AMGEN INC:0000318154:\n"
    "AMGEN INC /DE/:0000000501:\n"
    "AMGEN INTERNATIONAL HOLDINGS:0000000502:\n"
    "FRANKLIN RESOURCES INC:0000038777:\n"
    "FRANKLIN TEMPLETON INC:0000038777:\n"
    "NESTLE HOLDINGS INC:0001099999:\n"
    "NESTLE SA:0001095119:\n"
    "T ROWE PRICE GROUP INC:0000000601:\n"
    "WALMART INC.:0000104169:\n"
    "WALMART STORES:0000000701:\n"
).encode("latin-1") + b"".join(b"ZZ FILLER %04d INC:%010d:\n" % (i, i) for i in range(1100))


@pytest.fixture
def companies(tmp_path):
    return sec_names.index_path(str(tmp_path), download=lambda: COMPANIES)


@pytest.mark.parametrize("name, prefix, ciks", [
    # the dot the live index stumbles over, and EDGAR's tag: both the same name
    ("Amgen Inc.", None, ["0000318154", "0000000501"]),
    ("Walmart Inc", None, ["0000104169"]),
    # another form word than the registrant's: only the base words list it
    ("Amgen Corporation", None, []),
    ("Amgen Corporation", "amgen", ["0000318154", "0000000501"]),
    # the list is unaccented; the holding of the same first word is another name
    ("Nestlé S.A.", None, ["0001095119"]),
    ("Nestlé", "Nestlé", ["0001095119"]),
    # a former name is listed like a current one — the caller confirms it
    ("Franklin Resources, Inc.", None, ["0000038777"]),
    ("Koch Industries, Inc.", None, []),
    ("Inc.", None, []),
])
def test_registrants_named(companies, name, prefix, ciks):
    assert sec_names.registrants_named(name, prefix, path=companies) == ciks


def test_no_list_is_none_for_a_company_too():
    assert sec_names.registrants_named("Amgen Inc.") is None


class TestTheCompanyLookupAsksTheListFirst:
    """`sec_lookup._exact_name_registrant`: the type-free pass of the name lookup
    and the rule a proxy table's holder is found by."""

    @staticmethod
    def sub(name, forms=("SC 13G",)):
        return {"name": name, "filings": {"recent": {"form": list(forms), "filingDate": ["2026-01-02"] * len(forms)}}}

    def run(self, name, path, subs, listed_live=()):
        asked = []

        def record(cik):
            asked.append(cik)
            return subs[cik]
        with patch.object(sec_names, "index_path", return_value=path), \
             patch.object(sec_lookup, "_browse_index", return_value=list(listed_live)) as live, \
             patch.object(sec_client, "_submissions", side_effect=record):
            return sec_lookup._exact_name_registrant(name), asked, live

    def test_a_name_the_live_index_misses_is_found_with_no_search_request(self, companies):
        found, asked, live = self.run("Walmart Inc.", companies, {"0000104169": self.sub("Walmart Inc.")})
        assert found == {"cik": "0000104169", "name": "Walmart Inc."}
        live.assert_not_called()

    def test_a_name_the_list_does_not_hold_asks_nobody(self, companies):
        found, asked, live = self.run("Koch Industries, Inc.", companies, {})
        assert found is None and asked == []
        live.assert_not_called()

    def test_a_former_name_is_not_taken(self, companies):
        # the list holds FRANKLIN RESOURCES INC; the filer's record says what it is called today
        found, asked, _ = self.run("Franklin Resources, Inc.", companies,
                                   {"0000038777": self.sub("FRANKLIN TEMPLETON INC")})
        assert found is None
        assert asked == ["0000038777"]                  # held against the name once, not again by its base words

    def test_a_dormant_duplicate_the_list_reaches_is_not_taken(self, companies):
        # item 59: five no-action letters, not the filer (PRICE T ROWE GROUP INC);
        # the live index, asked for the name as written, lists nothing either
        found, _, live = self.run("T. Rowe Price Group, Inc.", companies,
                                  {"0000000601": self.sub("T ROWE PRICE GROUP INC", forms=("NO ACT",))})
        assert found is None
        live.assert_called_once_with("T. Rowe Price Group, Inc.", "")

    def test_a_holder_that_files_nothing_read_is_left_to_the_live_index(self, companies):
        # a fund that files Form D alone is still the holder a proxy names: the
        # live index took it before the list, and still does (12 of 752 names)
        found, _, live = self.run("Walmart Inc.", companies, {"0000104169": self.sub("Walmart Inc.", forms=("D",))},
                                  listed_live=[("0000104169", "Walmart Inc.")])
        assert found == {"cik": "0000104169", "name": "Walmart Inc."}
        live.assert_called_once_with("Walmart Inc.", "")

    def test_of_two_registrants_of_one_name_the_one_filing_what_is_read_wins(self, companies):
        found, _, _ = self.run("Amgen Inc.", companies, {
            "0000318154": self.sub("AMGEN INC", forms=("10-K", "4")),
            "0000000501": self.sub("AMGEN INC /DE/", forms=("REGDEX",))})
        assert found == {"cik": "0000318154", "name": "AMGEN INC"}

    def test_another_form_word_is_found_by_the_base_words(self, companies):
        found, _, _ = self.run("Walmart Corporation", companies, {"0000104169": self.sub("Walmart Inc.")})
        assert found == {"cik": "0000104169", "name": "Walmart Inc."}

    def test_without_a_list_the_live_index_is_asked_and_a_lone_match_needs_no_filing_check(self):
        with patch.object(sec_lookup, "_browse_index", return_value=[("0000104169", "Walmart Inc.")]) as live, \
             patch.object(sec_client, "_submissions") as record:
            assert sec_lookup._exact_name_registrant("Walmart Inc.") == {"cik": "0000104169", "name": "Walmart Inc."}
        live.assert_called_once_with("Walmart Inc.", "")
        record.assert_not_called()

    def test_the_table_holder_lookup_goes_through_it(self):
        import inspect

        from app.scraper import sec_writer
        source = inspect.getsource(sec_writer._table_holder_node)
        assert "_exact_name_registrant(name)" in source and "_browse_index" not in source
