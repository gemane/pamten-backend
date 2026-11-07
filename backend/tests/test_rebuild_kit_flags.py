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


def test_step_7_runs_the_securities_bridge_after_the_dedup_and_before_the_shortcuts():
    # findings item 71: the replay stores the CUSIPs (13F, 13D/G covers), so the
    # bridge belongs after it; a join merges two nodes, so before mark-shortcuts
    step7 = _step(7)
    [line] = [ln for ln in step7.splitlines() if "manage.py gleif-isin-bridge" in ln]
    assert "--apply" in line
    assert "SCRAPER_" not in line          # GLEIF's catalogue switch is its only gate
    at = step7.index(line)
    assert step7.index("dedupe-entities") < at < step7.index("manage.py mark-shortcuts")


def test_step_8_prints_the_source_calls_before_the_snapshot():
    # the calls to each source and the bucket since step 1 created the database
    # (app/source_calls.py); the snapshot keeps them for the next verify's diff
    step8 = SCRIPT[SCRIPT.index('t "8 '):]
    lines = [ln for ln in step8.splitlines() if not ln.lstrip().startswith("#")]
    [calls] = [i for i, ln in enumerate(lines) if "manage.py source-calls" in ln]
    [snap] = [i for i, ln in enumerate(lines) if "snapshot.py" in ln]
    assert calls < snap
    assert "--from 2000-01-01" in lines[calls], "every row of the fresh database"


def test_the_replay_counts_its_calls():
    # the replay is not a manage.py command, so it switches the counter on itself
    replay = (KIT / "replay_scrapes.py").read_text()
    assert "source_calls.start()" in replay
