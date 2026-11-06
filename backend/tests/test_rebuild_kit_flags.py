"""The /rebuild kit's step 7 runs the entity dedup with the scraper flags on
(findings item 70): its SEC/LEI bridge reads EDGAR and is gated by them, and the
backend .env — exported by `owlgraph_env` — has them off, so every rebuild's step 7
printed "Merged 0 entities" and never asked a filing for a same-name pair."""
import pathlib
import re

KIT = pathlib.Path(__file__).resolve().parents[2] / ".claude" / "skills" / "rebuild" / "kit"
SCRIPT = (KIT / "rebuild.sh").read_text()


def _step(n: int) -> str:
    """The lines of step ``n`` of rebuild.sh, up to the next step marker."""
    m = re.search(rf'^t "{n} .*?$(.*?)^t "{n + 1} ', SCRIPT, re.S | re.M)
    assert m, f"step {n} not found"
    return m.group(1)


def test_step_7_runs_the_entity_dedup_with_the_scraper_flags_on():
    lines = [ln for ln in _step(7).splitlines() if "dedupe-entities" in ln and not ln.lstrip().startswith("#")]
    assert len(lines) == 1, lines
    command = _step(7)[: _step(7).index(lines[0]) + len(lines[0])]
    # the flags sit on the dedupe's own invocation (a continued line), not exported for the whole run
    invocation = "\n".join(command.splitlines()[-2:])
    for flag in ("SCRAPER_ENABLED=true", "SCRAPER_SEC_EDGAR_ENABLED=true", "SCRAPER_BODS_GLEIF_ENABLED=true"):
        assert flag in invocation, flag
    assert "export SCRAPER_ENABLED" not in SCRIPT, "the flags must not leak into the other steps"


def test_the_other_finishing_steps_run_without_the_flags():
    step7 = _step(7)
    for cmd in ("dedupe-persons", "geocode", "close-reported-by-parent", "mark-shortcuts", "mark-stale"):
        [line] = [ln for ln in step7.splitlines() if f"manage.py {cmd}" in ln]
        assert "SCRAPER_" not in line, line
