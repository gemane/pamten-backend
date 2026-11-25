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


def test_step_7_runs_the_backend_s_one_list_step_by_step():
    # the steps, their order and their flags were written out here until
    # 2026-11-25, beside four other lists that had drifted apart; they are the
    # `after-scrapes` profile of app/finishing.py now (tests/test_finishing.py
    # pins it: the flags on the dedup and the two EDGAR passes, the bridge first,
    # the officers between the two dedups, the shortcuts last)
    step7 = _step(7)
    assert 'FINISHING="$(python3 manage.py finish --profile after-scrapes --list 2>/dev/null)"' in step7
    assert 'python3 manage.py finish --profile after-scrapes --only "$name" </dev/null' in step7
    assert 'done <<< "$FINISHING"' in step7
    # a step that fails is said and the rebuild goes on …
    assert '|| echo "  ! $label failed (see above)"' in step7.replace("\\\n   ", "")
    # … but a list that cannot be read is no rebuild without finishing steps
    assert '[ -n "$FINISHING" ] || die ' in step7
    # no finishing command is written out here any more
    code = "\n".join(ln for ln in step7.splitlines() if not ln.lstrip().startswith("#"))
    for cmd in ("dedupe-entities", "heal-ends-at-dissolution", "ch-officers", "dedupe-persons", "geocode",
                "close-reported-by-parent", "close-ended-classes", "close-absent-from-table",
                "close-held-by-subsidiary", "mark-shortcuts", "mark-stale", "gleif-isin-bridge"):
        assert f"manage.py {cmd}" not in code, cmd
    assert "SCRAPER_" not in code and "export SCRAPER_ENABLED" not in SCRIPT   # the profile gives each step its flags


def test_step_7_records_each_step_s_calls_under_the_label_the_diff_compares():
    # findings item 135: "7 <label>" rows in calls.tsv; the labels are the profile's
    step7 = _step(7)
    loop = step7[step7.index("while IFS="):step7.index('done <<< "$FINISHING"')]
    assert loop.index("manage.py finish") < loop.index('calls "7 $label"')


def test_step_7_reads_the_successions_of_every_lei_after_the_bridge_and_before_the_dedup():
    # findings item 89: the chain read them for the import's LEIs; the replay and the
    # bridge add lei: nodes, so the pass runs again here — the list and the endpoints
    # in the kit folder, the endpoints named, the dedup folding what it added. It is
    # the one finishing pass that stays in the kit (its inputs are the kit's own).
    from app import finishing
    step7 = _step(7)
    loop = step7[step7.index("while IFS="):step7.index('done <<< "$FINISHING"')]
    assert 'if [ "$name" = securities-bridge ]; then successions_after_the_replay; fi' in loop
    assert loop.index('calls "7 $label"') < loop.index("successions_after_the_replay")
    order = finishing.known_order("after-scrapes")
    assert order.index("securities-bridge") + 1 == order.index("entity-dedup")     # so: bridge, successions, dedup
    joined = step7.replace("\\\n", " ")
    body = joined[joined.index("successions_after_the_replay() {"):joined.index("FINISHING=")]
    order = [body.index(s) for s in ('lei_ids.py" "$R/leis_after_replay.txt"', "manage.py gleif-succession",
                                     "manage.py gleif-lei-cdf")]
    assert order == sorted(order), order
    [succ] = [ln for ln in body.splitlines() if "manage.py gleif-succession" in ln]
    assert '--only-file "$R/leis_after_replay.txt"' in succ and '--emit-leis "$R/succession_leis.txt"' in succ
    [names] = [ln for ln in body.splitlines() if "manage.py gleif-lei-cdf" in ln]
    assert '--only-file "$R/succession_leis.txt"' in names
    assert "SCRAPER_" not in body
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
    recorded: list = []          # the phase's calls to the sources (`calls`, findings item 135)
    ns: dict = {"TIMINGS": str(rows), "SOURCE_SECONDS": {}, "time": time, "os": os, "calls": recorded.append}
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
    assert recorded == ["A run-all over the list"]


def test_the_rebuild_never_loads_former_officers():
    # the user, 2026-11-24: the serving officers are loaded by the rebuild (the
    # `uk-officers` step of the profile, between the two dedups) — never with
    # --history: former officers wait for the DPIA paragraph
    from app import finishing
    assert "uk-officers" in finishing.known_order("after-scrapes")
    assert "--history" not in "\n".join(ln for ln in SCRIPT.splitlines() if not ln.lstrip().startswith("#"))
