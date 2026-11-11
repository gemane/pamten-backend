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
    for cmd in ("gleif-succession", "gleif-lei-cdf", "heal-ends-at-dissolution", "dedupe-persons", "geocode",
                "close-reported-by-parent", "mark-shortcuts", "mark-stale"):
        [line] = [ln for ln in step7.splitlines() if f"manage.py {cmd}" in ln]
        assert "SCRAPER_" not in line, line


def test_step_7_runs_the_securities_bridge_before_the_dedup_and_the_shortcuts():
    # findings item 71: the replay stores the CUSIPs (13F, 13D/G covers), so the
    # bridge belongs after it; a join merges two nodes, so before mark-shortcuts —
    # and before the dedup, which folds what the bridge attached (item 89 moved it)
    step7 = _step(7)
    [line] = [ln for ln in step7.splitlines() if "manage.py gleif-isin-bridge" in ln]
    assert "--apply" in line
    assert "SCRAPER_" not in line          # GLEIF's catalogue switch is its only gate
    at = step7.index(line)
    assert at < step7.index("dedupe-entities") < step7.index("manage.py mark-shortcuts")


def test_step_7_reads_the_successions_of_every_lei_after_the_bridge_and_before_the_dedup():
    # findings item 89: the chain read them for the import's LEIs; the replay and the
    # bridge add lei: nodes, so the pass runs again here — the list and the endpoints
    # in the kit folder, the endpoints named, the dedup folding what it added, the
    # dissolution heal after it for the ends written since finish-import's own
    step7 = _step(7)
    order = [step7.index(s) for s in ("manage.py gleif-isin-bridge", 'lei_ids.py" "$R/leis_after_replay.txt"',
                                      "manage.py gleif-succession", "manage.py gleif-lei-cdf", "dedupe-entities",
                                      "manage.py heal-ends-at-dissolution --apply", "manage.py dedupe-persons")]
    assert order == sorted(order), order
    # the two invocations continue over a second line; read them as one
    joined = step7.replace("\\\n", " ")
    [succ] = [ln for ln in joined.splitlines() if "manage.py gleif-succession" in ln]
    assert '--only-file "$R/leis_after_replay.txt"' in succ and '--emit-leis "$R/succession_leis.txt"' in succ
    [names] = [ln for ln in joined.splitlines() if "manage.py gleif-lei-cdf" in ln]
    assert '--only-file "$R/succession_leis.txt"' in names
    assert "/tmp/" not in step7                       # the kit folder, not RAM (the chain's mktemp lesson)


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


def _source_line():
    # the replay imports the app on load; take the pure function out of its source
    import ast
    src = (KIT / "replay_scrapes.py").read_text()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "source_line")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "replay_scrapes.py", "exec"), ns)
    return ns["source_line"]


def test_the_replay_line_shows_the_seat_holders_rerun():
    # findings item 67: whether run-all asked the seats once more was not in the log
    line = _source_line()({
        "sec_edgar": {"status": "ok", "total": 85, "lost_requests": 1,
                      "seats_rerun": {"status": "ok", "total": 2, "seats_asked": ["a", "b", "c"]}},
        "wikidata": {"status": "ok", "total": 1},
        "open_corporates": {"status": "disabled"},
    })
    assert line == {"sec_edgar": ("ok", 85, "lost 1", "seats +2/3"), "wikidata": ("ok", 1),
                    "open_corporates": ("disabled", None)}
    assert _source_line()({"sec_edgar": {"status": "ok", "total": 3,
                                         "seats_rerun": {"status": "error", "detail": "x"}}}) == \
        {"sec_edgar": ("ok", 3, "seats error")}


def _ex21_and_steps():
    # findings item 96: phase C collects the companies it read a list for, C2
    # dates their subsidiary edges once per company; the default steps run it
    import ast
    src = (KIT / "replay_scrapes.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ex21")
    return src, fn


def test_the_replay_runs_the_exhibit_21_history_once_per_company():
    src, fn = _ex21_and_steps()
    import ast
    ns: dict = {"EX21_READ": {}, "short": lambda r: r.get("status"),
                "run_sec_ex21": lambda n: {"Apple": {"status": "ok", "entity_id": "lei:A"},
                                           "Apple Inc.": {"status": "ok", "entity_id": "lei:A"},
                                           "Nobody": {"status": "no_annual_filing"}}[n]}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "replay_scrapes.py", "exec"), ns)
    for name in ("Apple", "Apple Inc.", "Nobody"):
        ns["ex21"](name)
    assert ns["EX21_READ"] == {"lei:A": "Apple"}
    assert '"C", "C2", "D"' in src and 'run_sec_ex21_history' in src


def test_the_replay_adds_each_source_s_time_to_its_phase(tmp_path):
    # the rebuild's timings: run-all reports each source's seconds, the phase sums them
    import ast
    src = (KIT / "replay_scrapes.py").read_text()
    tree = ast.parse(src)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("timing", "phase")]
    rows = tmp_path / "timings.tsv"
    import os
    import time
    ns: dict = {"TIMINGS": str(rows), "SOURCE_SECONDS": {}, "time": time, "os": os}
    exec(compile(ast.Module(body=fns, type_ignores=[]), "replay_scrapes.py", "exec"), ns)

    def fake_run_all(name):
        for src_, secs in (("sec_edgar", 30.0), ("wikidata", 12.0)):
            ns["SOURCE_SECONDS"][src_] = ns["SOURCE_SECONDS"].get(src_, 0.0) + secs
        return "ok"
    ns["phase"]("A run-all over the list", ["Apple", "Tesla"], fake_run_all)
    got = dict(line.split("\t") for line in rows.read_text().splitlines())
    assert got["6.A run-all over the list · sec_edgar"] == "60"
    assert got["6.A run-all over the list · wikidata"] == "24"
    assert "6.A run-all over the list" in got
