"""The curated GLEIF seed list (app/scraper/data/test_leis.txt) the rebuild's
GLEIF subset loads with each seed's whole family. A mistyped LEI loads nothing
and says nothing, so every line is checked: an ISO 17442 code (mod-97 check
digits) and no seed listed twice."""
import pathlib
import re

SEEDS = pathlib.Path(__file__).resolve().parents[2] / "app" / "scraper" / "data" / "test_leis.txt"


def _leis() -> list[str]:
    out = []
    for line in SEEDS.read_text().splitlines():
        code = line.split("#", 1)[0].strip()
        if code:
            out.append(code)
    return out


def _check_digits_ok(lei: str) -> bool:
    digits = "".join(str(int(ch, 36)) for ch in lei)
    return int(digits) % 97 == 1


def test_every_seed_is_a_valid_lei():
    bad = [lei for lei in _leis() if not re.fullmatch(r"[0-9A-Z]{18}[0-9]{2}", lei) or not _check_digits_ok(lei)]
    assert bad == []


def test_no_seed_twice():
    leis = _leis()
    assert len(leis) == len(set(leis))


def test_the_start_page_groups_are_seeded():
    # the international block of 2026-11-07 (scripts/scrape-targets.txt mirrors it)
    leis = set(_leis())
    for lei in ("5493006W3QUS5LMH6R84", "529900F0LT5OP4SV6122", "213800FD9J2IHTA7YX78", "5493008JZL5CRK3VS565"):
        assert lei in leis
