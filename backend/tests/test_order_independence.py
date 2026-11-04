"""Writer tests pass on their own, not only after an earlier test warmed a cache.

Findings item 43: three tests that count a mocked session's calls passed in
the suite but failed alone, because the source data-mode cache was cold on the
first write in a process and read the Source nodes through the mock. The
autouse `source_mode_cache` fixture (tests/conftest.py) fixes the start state;
this runs each of the three in a fresh process, first and alone, which is the
only way to see the defect — inside this process the cache is already warm.
"""
import os
import subprocess
import sys

import pytest

ALONE = [
    "tests/scraper/test_runner.py::TestUpsertOwns::test_creates_edge_when_not_exists",
    "tests/scraper/test_runner.py::TestUpsertOwns::test_refreshes_and_backfills_when_edge_exists",
    "tests/scraper/test_runner.py::TestSuccession::test_upsert_succession_skips_self_loop",
]
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("nodeid", ALONE)
def test_a_writer_test_passes_alone_in_a_fresh_process(nodeid):
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", nodeid],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
