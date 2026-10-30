"""The name key the SEC name-match guard compares EDGAR's name with
(graph_writer._name_key / _not_this_filer, 2026-10-29)."""
from unittest.mock import MagicMock, patch

import pytest

from app.scraper.graph_writer import _filer_as_registered, _name_key, _not_this_filer


@pytest.mark.parametrize("a,b", [
    ("The Coca-Cola Company", "COCA COLA CO"),
    ("Amazon.com, Inc.", "AMAZON COM INC"),
    ("Bank of America Corporation", "BANK OF AMERICA CORP /DE/"),
    ("Toyota Motor Corporation", "TOYOTA MOTOR CORP/"),
    ("Société Générale", "SOCIETE GENERALE"),
    ("BlackRock, Inc.", "Blackrock, Inc."),
    ("Baillie Gifford & Co", "Baillie Gifford and Company"),
    ("JPMorgan Chase & Co.", "JPMorgan Chase"),
    # findings items 12 and 13 (2026-10-30)
    ("Blackstone L.P.", "BLACKSTONE LP"),
    ("Blackstone L.P", "Blackstone"),
    ("Siemens Beteiligungen GmbH & Co. KG", "SIEMENS BETEILIGUNGEN"),
    ("Henkel AG & Co. KGaA", "Henkel"),
    ("Robert Bosch GmbH", "ROBERT BOSCH"),
    ("U.S. Bancorp", "US BANCORP"),
    ("Toyota Motor Co., Ltd.", "TOYOTA MOTOR CORP"),
    ("Anheuser-Busch InBev SA/NV", "ANHEUSER-BUSCH INBEV"),
])
def test_spellings_of_one_name_agree(a, b):
    assert _name_key(a) == _name_key(b)


@pytest.mark.parametrize("name,key", [
    ("NV ENERGY, INC.", ("nv", "energy")),
    ("Innovex Co-Invest Fund, L.P.", ("innovex", "co", "invest", "fund")),
    ("The Limited", ("limited",)),
    ("AB Volvo", ("ab", "volvo")),
])
def test_a_form_or_the_inside_a_name_stays(name, key):
    assert _name_key(name) == key


@pytest.mark.parametrize("a,b", [
    ("BlackRock, Inc.", "BlackRock Finance, Inc."),
    ("Tencent", "Tencent Music Entertainment Group"),
    ("Baillie Gifford & Co", "BAILLIE GIFFORD FUNDS"),
    ("Procter & Gamble", "Procter Gamble"),
    ("KG Mobility Corp", "Mobility Corp"),          # KG is a legal form only at the end
    # so is every other form (findings item 5, 2026-10-30)
    ("NV ENERGY, INC.", "ENERGY FUELS INC"),
    ("NV Energy", "Energy"),
])
def test_another_word_is_another_company(a, b):
    assert _name_key(a) != _name_key(b)


def _session(**node):
    s = MagicMock()
    s.run.return_value.single.return_value = {"name": None, "other": None, "cik": None, "lei": None, **node}
    return s


class TestNotThisFiler:
    def test_edgars_other_name(self):
        with patch("app.scraper.sec_edgar.fetch_filer_name", return_value="BlackRock Finance, Inc."):
            why = _not_this_filer(_session(name="BlackRock, Inc."), "n", "0001364742", None, None)
        assert why == "EDGAR names CIK 0001364742 'BlackRock Finance, Inc.'"

    def test_a_register_other_name_agrees(self):
        s = _session(name="삼성전자(주)", other=["Samsung Electronics Co., Ltd."])
        assert _not_this_filer(s, "n", "0000000001", None, "SAMSUNG ELECTRONICS CO LTD") is None

    def test_another_id_needs_no_request(self):
        with patch("app.scraper.sec_edgar.fetch_filer_name") as fetch:
            assert _not_this_filer(_session(cik="0000000002"), "n", "0000000001", None, None) \
                == "it carries CIK 0000000002"
            assert _not_this_filer(_session(lei="B" * 20), "n", None, "A" * 20, None) \
                == f"it carries LEI {'B' * 20}"
        fetch.assert_not_called()

    def test_the_same_id_in_another_spelling_is_the_same(self):
        # a bare CIK stored before 2026-10-26, a lower-case LEI
        assert _not_this_filer(_session(name="Meta Platforms, Inc.", cik="1326801"), "n",
                               "0001326801", None, "META PLATFORMS INC") is None
        assert _not_this_filer(_session(lei="a" * 20), "n", None, "A" * 20, None) is None

    def test_an_lei_alone_asks_edgar_nothing(self):
        with patch("app.scraper.sec_edgar.fetch_filer_name") as fetch:
            assert _not_this_filer(_session(name="X"), "n", None, "A" * 20, None) is None
        fetch.assert_not_called()

    def test_edgar_silent_means_the_match_stands(self):
        with patch("app.scraper.sec_edgar.fetch_filer_name", return_value=None):
            assert _not_this_filer(_session(name="X"), "n", "0000000001", None, None) is None


class TestFilerAsRegistered:
    """A filing spells the name its filer had then; the node takes the name in
    force and the dated former names (2026-10-30)."""
    SUB = {"name": "BlackRock Finance, Inc.",
           "formerNames": [{"name": "BlackRock Inc.", "to": "2024-09-26T00:00:00.000Z"},
                           {"name": "BlackRock, Inc.", "to": "2006-10-05T00:00:00.000Z"}]}

    def test_an_old_spelling_gives_edgars_name_and_the_former_names(self):
        with patch("app.scraper.sec_edgar._submissions", return_value=self.SUB):
            assert _filer_as_registered("0001364742", "BlackRock Inc.") == (
                "BlackRock Finance, Inc.", ["BlackRock Inc.|2024-09-26", "BlackRock, Inc.|2006-10-05"])

    def test_the_current_name_in_another_spelling_changes_nothing(self):
        with patch("app.scraper.sec_edgar._submissions", return_value=self.SUB):
            assert _filer_as_registered("0001364742", "BLACKROCK FINANCE INC") is None

    def test_edgar_unreachable_leaves_the_filings_name(self):
        with patch("app.scraper.sec_edgar._submissions", side_effect=RuntimeError("down")):
            assert _filer_as_registered("0001364742", "BlackRock Inc.") is None


@pytest.mark.parametrize("name,key", [
    ("Domino's Pizza, Inc.", ("dominos", "pizza")),
    ("Domino’s Pizza, Inc.", ("dominos", "pizza")),
    ("DOMINOS PIZZA INC", ("dominos", "pizza")),
    ("McDonald's Corp", ("mcdonalds",)),
    ("McDonald’s Corp", ("mcdonalds",)),
])
def test_an_apostrophe_is_dropped_not_a_word_break(name, key):
    # the split turned the straight one into a stray "s" word (2026-10-30)
    assert _name_key(name) == key


class TestAGleifNodeMetByAFilersName:
    """A node that carries an LEI and no CIK, found by an EDGAR filer's name:
    the registers decide (`legal_identity`, findings item 4)."""

    def _lei_node(self, **extra):
        return _session(name="Domino's Pizza LLC", lei="254900BIRZMWFO44TO04", **extra)

    def test_the_rule_refuses_the_llc(self):
        from app.scraper import graph_writer
        from app.scraper.legal_identity import Filer
        with patch("app.scraper.legal_identity.filer_facts", return_value=Filer("0001286681", "DOMINOS PIZZA INC")), \
             patch("app.scraper.legal_identity.same_legal_entity",
                   return_value=("refuse", "legal name differs")) as rule:
            why = _not_this_filer(self._lei_node(), "lei:254900BIRZMWFO44TO04", "0001286681", None, None)
        assert why == "its GLEIF record is not filer 0001286681: legal name differs"
        assert rule.call_args.args[1].name == "DOMINOS PIZZA INC"
        assert ("lei:254900BIRZMWFO44TO04", "0001286681") not in graph_writer._JOINED

    def test_a_join_is_remembered_for_the_write(self):
        from app.scraper import graph_writer
        from app.scraper.legal_identity import Filer
        with patch("app.scraper.legal_identity.filer_facts", return_value=Filer("0001286681", "DOMINOS PIZZA INC")), \
             patch("app.scraper.legal_identity.same_legal_entity", return_value=("join", "name+place")):
            assert _not_this_filer(self._lei_node(), "lei:INC", "0001286681", None, None) is None
        assert graph_writer._JOINED.pop(("lei:INC", "0001286681")) == "name+place"

    def test_edgar_silent_falls_back_to_the_name(self):
        with patch("app.scraper.legal_identity.filer_facts", return_value=None), \
             patch("app.scraper.sec_edgar.fetch_filer_name", return_value="DOMINOS PIZZA INC"):
            assert _not_this_filer(self._lei_node(), "n", "0001286681", None, None) is None

    def test_a_node_without_an_lei_keeps_the_name_test(self):
        with patch("app.scraper.legal_identity.same_legal_entity") as rule, \
             patch("app.scraper.sec_edgar.fetch_filer_name", return_value="DOMINOS PIZZA INC"):
            assert _not_this_filer(_session(name="Domino's Pizza Inc"), "n", "0001286681", None, None) is None
        rule.assert_not_called()


class TestTheFirstCandidateTheRegistersAllow:
    def test_the_llc_refused_the_inc_taken(self):
        from app.scraper import graph_writer
        refusals = {"lei:LLC": "its GLEIF record is not filer 0001286681: legal name differs"}
        with patch.object(graph_writer, "_not_this_filer", side_effect=lambda s, i, *a: refusals.get(i)):
            got = graph_writer._first_filer_match(MagicMock(), ["lei:LLC", "lei:INC"], "DOMINOS PIZZA INC",
                                                  "0001286681", None, None)
        assert got == ("lei:INC", True)

    def test_every_candidate_of_the_name_is_weighed(self):
        from app.scraper import graph_writer
        s = MagicMock()
        s.run.side_effect = lambda q, **kw: [{"id": "lei:LLC"}, {"id": "lei:INC"}] if "name_normalized" in q else []
        assert graph_writer._candidates_by_name(s, "DOMINOS PIZZA INC", "dominos pizza", None) == ["lei:LLC", "lei:INC"]


def test_dotted_letters_are_one_word_and_a_lone_letter_stays():
    assert _name_key("Blackstone L.P.") == ("blackstone",)
    assert _name_key("Toys R Us") == ("toys", "r", "us")
    # known gap, not widened: EDGAR writes "JPMORGAN" as one word
    assert _name_key("J.P. Morgan Chase & Co.") == ("jp", "morgan", "chase")
