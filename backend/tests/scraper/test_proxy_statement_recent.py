"""How old a proxy statement may be is counted from today (findings item 48);
the cut-off used to be the literal 2026 - 4."""
from datetime import date
from unittest.mock import patch

import pytest

from app.scraper import proxy_statement


@pytest.mark.parametrize("today,filed,recent", [
    (date(2026, 11, 3), "2022-04-01", True),
    (date(2026, 11, 3), "2021-12-31", False),
    (date(2030, 1, 2), "2026-04-24", True),
    (date(2030, 1, 2), "2025-04-24", False),     # the literal 2026 kept this "recent"
    (date(2030, 1, 2), "", False),
    (date(2030, 1, 2), None, False),
])
def test_the_window_moves_with_today(today, filed, recent):
    assert proxy_statement._is_recent(filed, today) is recent


def test_without_a_date_it_reads_the_clock():
    class Later(date):
        @classmethod
        def today(cls):
            return date(2031, 6, 1)
    with patch.object(proxy_statement, "date", Later):
        assert proxy_statement._is_recent("2026-04-24") is False
        assert proxy_statement._is_recent("2027-04-24") is True
