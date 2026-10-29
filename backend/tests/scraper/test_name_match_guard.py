"""The name key the SEC name-match guard compares EDGAR's name with
(graph_writer._name_key / _not_this_filer, 2026-10-29)."""
from unittest.mock import MagicMock, patch

import pytest

from app.scraper.graph_writer import _name_key, _not_this_filer


@pytest.mark.parametrize("a,b", [
    ("The Coca-Cola Company", "COCA COLA CO"),
    ("Amazon.com, Inc.", "AMAZON COM INC"),
    ("Bank of America Corporation", "BANK OF AMERICA CORP /DE/"),
    ("Toyota Motor Corporation", "TOYOTA MOTOR CORP/"),
    ("Société Générale", "SOCIETE GENERALE"),
    ("BlackRock, Inc.", "Blackrock, Inc."),
])
def test_spellings_of_one_name_agree(a, b):
    assert _name_key(a) == _name_key(b)


@pytest.mark.parametrize("a,b", [
    ("BlackRock, Inc.", "BlackRock Finance, Inc."),
    ("Tencent", "Tencent Music Entertainment Group"),
    ("Baillie Gifford & Co", "BAILLIE GIFFORD FUNDS"),
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

    def test_an_lei_alone_asks_edgar_nothing(self):
        with patch("app.scraper.sec_edgar.fetch_filer_name") as fetch:
            assert _not_this_filer(_session(name="X"), "n", None, "A" * 20, None) is None
        fetch.assert_not_called()

    def test_edgar_silent_means_the_match_stands(self):
        with patch("app.scraper.sec_edgar.fetch_filer_name", return_value=None):
            assert _not_this_filer(_session(name="X"), "n", "0000000001", None, None) is None
