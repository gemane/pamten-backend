"""Of record versus beneficial (sec_writer.stated_by_13dg): where the holder
has filed a Schedule 13D/G that has not ended, that statement is the stake and
a Form 4 count never overwrites it. Claims mocked; the runner's three Form 4
sites are in test_runner.py, the real-database run in
tests/integration/test_form4_yields_to_13dg_it.py."""
from unittest.mock import patch

import pytest

from app.scraper.sec_writer import is_beneficial_ownership_filing, stated_by_13dg


@pytest.mark.parametrize("filing_type, expected", [
    ("13G/A", True), ("13G", True), ("13D", True), ("13D/A", True),
    ("SC 13D", True), ("SC 13G/A", True), ("SCHEDULE 13G/A", True), ("schedule 13d", True),
    ("Form 4", False), ("13F", False), ("EX-21", False), ("10-Q XBRL", False), (None, False), ("", False),
])
def test_the_13dg_family_however_spelt(filing_type, expected):
    assert is_beneficial_ownership_filing(filing_type) is expected


def _claim(**kw):
    base = {"kind": "owns", "from_id": "em", "to_id": "tsla", "source_id": "sec",
            "filing_type": "13G/A", "until": None, "stake_percent": 19.9}
    base.update(kw)
    return base


class TestStatedBy13dg:
    def _ask(self, claims):
        with patch("app.scraper.sec_writer.claims_for", return_value=claims) as cf:
            answer = stated_by_13dg("em", "tsla", "sec")
        cf.assert_called_once_with(from_id="em", to_id="tsla", kind="owns")
        return answer

    def test_an_active_13g_a_from_this_source_speaks_for_the_pair(self):
        assert self._ask([_claim()]) is True

    def test_a_form_4_claim_alone_does_not(self):
        assert self._ask([_claim(filing_type="Form 4", stake_percent=28.4)]) is False

    def test_a_13dg_that_reported_an_exit_no_longer_speaks(self):
        assert self._ask([_claim(until="2026-03-31", stake_percent=0.0)]) is False

    def test_another_sources_13dg_does_not_count(self):
        assert self._ask([_claim(source_id="other")]) is False

    def test_no_claims_means_the_form_4_writes(self):
        assert self._ask([]) is False

    def test_one_active_13dg_among_others_is_enough(self):
        assert self._ask([_claim(filing_type="Form 4"), _claim(source_id="other"), _claim()]) is True
