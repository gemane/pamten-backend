"""Which functions open a scrape scope (`graph_writer._with_autodedup`): every
scrape entry point, and nothing else.

The decorator sat on `_upsert_affiliate` (since #210) and `_same_filer` (since
#349) — two helpers inserted between it and the runner it belonged to — so a
standalone SEC scrape or Exhibit 21 run (the panel refresh, the EX-21 pass of
scrape-companies.sh) ran without the scoped dedup and the freshness stamp. The
wiring IT decorates its own fake runners, so it could never notice."""
import inspect

from app.scraper import runner
from app.scraper.graph_writer import _with_autodedup

_SCOPE_CODE = _with_autodedup(lambda: None).__code__

#: bulk importers and the daily delta: chains of their own (dedupe, dissolution,
#: shortcuts run as steps), not a scrape of one company
_BULK = {"run_import_ch_psc", "run_ch_psc_update", "run_import_basic_company_data",
         "run_import_gleif_succession", "run_import_gleif_lei_cdf", "run_import_gleif_repex",
         "run_import_gleif_rr", "run_gleif_update"}


def _scoped(fn) -> bool:
    return getattr(fn, "__code__", None) is _SCOPE_CODE


def _own_functions():
    return {n: f for n, f in vars(runner).items()
            if inspect.isfunction(f) and (f.__module__ == runner.__name__ or _scoped(f))}


def test_every_scrape_entry_point_opens_a_scope():
    entry = {n for n in _own_functions() if n.startswith("run_") and n not in _BULK}
    assert {"run_scrape_sec_edgar", "run_sec_ex21", "run_sec_acquisitions"} <= entry
    assert sorted(n for n in entry if not _scoped(_own_functions()[n])) == []


def test_no_helper_opens_one():
    assert sorted(n for n, f in _own_functions().items()
                  if _scoped(f) and not n.startswith("run_")) == []
