"""Stake history (app.stake_history): the dated statements a claim overwrote,
kept for holdings at or above 5 %.

Pure logic first (what counts as a change, which rows a write produces, the
crossings, the stake in force on a date), then the hooks with the SQL layer
mocked: every claim write path reads the stored claim BEFORE it upserts, the
rows ride in the same bulk script, the timeline tells only crossings, time
travel replaces the stake, the export gets its sheet. The real-database
behaviour is tests/integration/test_stake_history_it.py.
"""
from unittest.mock import patch

import pytest

from app.stake_history import (
    MATERIAL_PCT, changed, create_statements, crossings, material, rows_for,
    rows_for_batch, stakes_as_of, statement_date,
)

NOW = "2026-10-22T10:00:00+00:00"


def claim(stake, **kw):
    base = {"kind": "owns", "claim_key": "k1", "from_id": "br", "to_id": "ms", "source_id": "sec",
            "stake_percent": stake, "event_date": None, "source_date": "2025-02-13",
            "denominator_date": None, "filing_type": "13G/A", "source_url": "https://example.com/a",
            "first_seen_at": "2024-02-13T00:00:00Z"}
    base.update(kw)
    return base


# ── What counts ──────────────────────────────────────────────────────────────

class TestChange:
    @pytest.mark.parametrize("old,new,is_change", [
        (7.3, 7.3, False), (7.3, 7.30000001, False), (7.3, 6.1, True),
        (None, None, False), (None, 7.3, True), (7.3, None, True),
        ("7.3", 7.3, False), ("7.3", "6.1", True),
    ])
    def test_changed_is_none_aware_and_tolerant_of_float_noise(self, old, new, is_change):
        assert changed(old, new) is is_change

    @pytest.mark.parametrize("old,new,is_material", [
        (4.9, 4.99, False), (4.9, 5.0, True), (5.0, 4.9, True), (None, 5.0, True),
        (None, 4.9, False), (12.0, 30.0, True), (0, None, False),
    ])
    def test_material_means_one_side_at_or_above_the_line(self, old, new, is_material):
        assert material(old, new) is is_material
        assert MATERIAL_PCT == 5.0


class TestStatementDate:
    def test_the_date_of_event_beats_the_filing_date(self):
        assert statement_date({"event_date": "2025-06-30", "source_date": "2025-08-14"}) == "2025-06-30"

    def test_the_filing_date_when_no_event_date(self):
        assert statement_date({"event_date": None, "source_date": "2025-08-14T00:00:00Z"}) == "2025-08-14"

    def test_a_newer_denominator_dates_a_restated_stake(self):
        # a 13D/G stake re-divided by the issuer's newer class total is as of that total
        assert statement_date({"event_date": "2025-06-30", "denominator_date": "2026-05-02"}) == "2026-05-02"

    def test_an_older_denominator_changes_nothing(self):
        assert statement_date({"event_date": "2025-06-30", "denominator_date": "2024-05-02"}) == "2025-06-30"

    def test_nothing_dated_is_none(self):
        assert statement_date({}) is None


# ── Which rows a write produces ──────────────────────────────────────────────

class TestRowsFor:
    def test_a_first_statement_writes_nothing(self):
        assert rows_for(None, claim(7.3), had_history=False, now=NOW) == []

    def test_an_unchanged_stake_writes_nothing(self):
        assert rows_for(claim(7.3), claim(7.3), had_history=False, now=NOW) == []

    def test_a_change_below_the_line_writes_nothing(self):
        assert rows_for(claim(2.1), claim(3.4), had_history=False, now=NOW) == []

    def test_the_first_change_records_the_replaced_statement_and_the_new_one(self):
        old = claim(4.8, source_date="2024-02-13", filing_type="13G", source_url="https://example.com/13g")
        rows = rows_for(old, claim(7.3, source_date="2025-02-13"), had_history=False, now=NOW)
        assert [(r["stake_percent"], r["previous_percent"], r["as_of"]) for r in rows] == \
               [(4.8, None, "2024-02-13"), (7.3, 4.8, "2025-02-13")]
        assert rows[0]["filing_type"] == "13G" and rows[0]["source_url"] == "https://example.com/13g"
        assert rows[1]["filing_type"] == "13G/A"
        assert all(r["recorded_at"] == NOW for r in rows)
        assert rows[1]["from_id"] == "br" and rows[1]["to_id"] == "ms" and rows[1]["source_id"] == "sec"

    def test_a_later_change_records_only_the_new_statement(self):
        rows = rows_for(claim(7.3), claim(6.1, source_date="2025-08-14"), had_history=True, now=NOW)
        assert [(r["stake_percent"], r["previous_percent"], r["as_of"]) for r in rows] == [(6.1, 7.3, "2025-08-14")]

    def test_a_stake_appearing_where_none_was_stated_writes_one_row(self):
        # the old statement had no number: nothing to date, nothing to keep
        rows = rows_for(claim(None), claim(7.3), had_history=False, now=NOW)
        assert [(r["stake_percent"], r["previous_percent"]) for r in rows] == [(7.3, None)]

    def test_a_stake_withdrawn_is_kept_as_a_null_statement(self):
        rows = rows_for(claim(7.3), claim(None, source_date="2025-08-14"), had_history=True, now=NOW)
        assert [(r["stake_percent"], r["previous_percent"]) for r in rows] == [(None, 7.3)]

    def test_an_undated_old_statement_falls_back_to_first_seen_then_today(self):
        old = claim(4.8, source_date=None, event_date=None)
        rows = rows_for(old, claim(7.3, source_date=None), had_history=False, now=NOW)
        assert rows[0]["as_of"] == "2024-02-13"       # first_seen_at
        assert rows[1]["as_of"] == NOW[:10]           # nothing dated at all

    def test_only_owns_claims(self):
        assert rows_for(claim(7.3, kind="role"), claim(9.0, kind="role"), had_history=False, now=NOW) == []


# ── Crossings and the stake in force ─────────────────────────────────────────

def row(stake, previous, as_of, src="sec", frm="br", to="ms"):
    return {"from_id": frm, "to_id": to, "source_id": src, "stake_percent": stake,
            "previous_percent": previous, "as_of": as_of, "recorded_at": NOW}


class TestCrossings:
    def test_reaching_and_dropping_below_the_line(self):
        out = crossings([row(4.8, None, "2024-01-01"), row(7.3, 4.8, "2025-01-01"),
                         row(9.0, 7.3, "2025-06-30"), row(3.0, 9.0, "2026-01-01")])
        assert [(c["as_of"], c["crossing"]) for c in out] == [("2025-01-01", "reached"), ("2026-01-01", "dropped")]

    def test_a_first_statement_is_not_a_crossing(self):
        # before it the stake was unknown, not below
        assert crossings([row(7.3, None, "2025-01-01")]) == []

    def test_a_withdrawn_stake_is_not_a_crossing(self):
        assert crossings([row(None, 7.3, "2025-01-01")]) == []

    def test_exactly_five_counts_as_reached(self):
        assert crossings([row(5.0, 4.99, "2025-01-01")])[0]["crossing"] == "reached"


class TestStakesAsOf:
    ROWS = [row(7.3, 4.8, "2025-02-13"), row(4.8, None, "2024-02-13"), row(6.1, 7.3, "2025-08-14")]

    def test_the_latest_statement_on_or_before_the_date(self):
        assert stakes_as_of(self.ROWS, "2025-06-30")[("br", "ms", "sec")] == {"stake_percent": 7.3, "as_of": "2025-02-13"}
        assert stakes_as_of(self.ROWS, "2025-02-13")[("br", "ms", "sec")]["stake_percent"] == 7.3

    def test_before_the_first_statement_the_earliest_one(self):
        # what the edge showed before the table existed — never "unknown"
        assert stakes_as_of(self.ROWS, "2020-01-01")[("br", "ms", "sec")] == {"stake_percent": 4.8, "as_of": "2024-02-13"}

    def test_after_the_last_the_last(self):
        assert stakes_as_of(self.ROWS, "2026-12-31")[("br", "ms", "sec")]["stake_percent"] == 6.1

    def test_sources_are_separate_series(self):
        rows = self.ROWS + [row(10.0, 20.0, "2025-01-01", src="gleif")]
        out = stakes_as_of(rows, "2025-06-30")
        assert out[("br", "ms", "gleif")]["stake_percent"] == 10.0
        assert out[("br", "ms", "sec")]["stake_percent"] == 7.3


class TestCreateStatements:
    def test_one_create_per_row_with_prefixed_parameters(self):
        params = {}
        stmts = create_statements([row(7.3, 4.8, "2025-02-13"), row(6.1, 7.3, "2025-08-14")], params)
        assert len(stmts) == 2 and all(s.startswith("CREATE VERTEX StakeHistory SET ") for s in stmts)
        assert params["sh_stake_percent__0"] == 7.3 and params["sh_previous_percent__1"] == 7.3
        assert "as_of = :sh_as_of__1" in stmts[1] and stmts[1].endswith(";")


# ── The hooks, SQL mocked ─────────────────────────────────────────────────────

def sql_recorder(stored: dict | None = None, history_count: int = 0):
    """A run_sql that answers the pre-read with `stored` and the history count,
    and records every statement."""
    calls = []

    def fake(sql, params=None, **_kw):
        calls.append((sql, params or {}))
        if "FROM Claim WHERE claim_key IN" in sql:
            return [stored] if stored and stored["claim_key"] in (params or {}).get("keys", []) else []
        if "count(*) AS n FROM StakeHistory" in sql:
            return [{"n": history_count}]
        return []
    return fake, calls


class TestRowsForBatch:
    def test_reads_the_stored_claims_once_and_asks_for_history_only_on_a_change(self):
        fake, calls = sql_recorder(stored=claim(4.8, source_date="2024-02-13"))
        with patch("app.db.arcadedb.run_sql", fake):
            rows = rows_for_batch([claim(7.3), claim(50.0, claim_key="other", from_id="x")])
        assert [(r["stake_percent"], r["previous_percent"]) for r in rows] == [(4.8, None), (7.3, 4.8)]
        pre_reads = [c for c in calls if "FROM Claim WHERE claim_key IN" in c[0]]
        assert len(pre_reads) == 1 and sorted(pre_reads[0][1]["keys"]) == ["k1", "other"]
        assert sum("count(*) AS n FROM StakeHistory" in c[0] for c in calls) == 1

    def test_an_unchanged_batch_asks_nothing_more(self):
        fake, calls = sql_recorder(stored=claim(7.3))
        with patch("app.db.arcadedb.run_sql", fake):
            assert rows_for_batch([claim(7.3)]) == []
        assert not any("StakeHistory" in c[0] for c in calls)

    def test_existing_history_means_no_replaced_statement_row(self):
        fake, _ = sql_recorder(stored=claim(7.3), history_count=2)
        with patch("app.db.arcadedb.run_sql", fake):
            rows = rows_for_batch([claim(6.1)])
        assert [(r["stake_percent"], r["previous_percent"]) for r in rows] == [(6.1, 7.3)]

    def test_a_twin_later_in_the_batch_compares_to_the_first(self):
        fake, calls = sql_recorder(stored=claim(4.8))
        with patch("app.db.arcadedb.run_sql", fake):
            rows = rows_for_batch([claim(7.3), claim(7.3)])
        assert len(rows) == 2                      # the pair for the first change, nothing for the twin
        assert sum("count(*) AS n FROM StakeHistory" in c[0] for c in calls) == 1

    def test_non_owns_and_keyless_claims_are_ignored(self):
        fake, calls = sql_recorder()
        with patch("app.db.arcadedb.run_sql", fake):
            assert rows_for_batch([claim(7.3, kind="role"), claim(7.3, claim_key=None)]) == []
        assert calls == []

    def test_a_failed_pre_read_writes_no_history_and_raises_nothing(self):
        def boom(*a, **k):
            raise RuntimeError("db away")
        with patch("app.db.arcadedb.run_sql", boom):
            assert rows_for_batch([claim(7.3)]) == []


class TestRecordClaimHook:
    def test_the_stored_stake_is_read_before_the_upsert_and_the_rows_written_after(self):
        from app.claims import KIND_OWNS, claim_key, record_claim
        key = claim_key(KIND_OWNS, "br", "ms", "sec")
        fake, calls = sql_recorder(stored=claim(4.8, claim_key=key, source_date="2024-02-13"))
        with patch("app.db.arcadedb.run_sql", fake):
            record_claim(kind=KIND_OWNS, from_id="br", to_id="ms", source_id="sec",
                         stake_percent=7.3, source_date="2025-02-13", filing_type="13G/A")
        verbs = [c[0].split()[0] + (" claim" if "FROM Claim WHERE claim_key IN" in c[0] else
                                    " history" if "StakeHistory" in c[0] else "") for c in calls]
        assert verbs == ["SELECT claim", "SELECT history", "UPDATE", "CREATE history", "CREATE history"]
        creates = [c[1] for c in calls if c[0].startswith("CREATE VERTEX StakeHistory")]
        assert [(p["sh_stake_percent__0"], p["sh_previous_percent__0"]) for p in creates] == [(4.8, None), (7.3, 4.8)]

    def test_a_role_claim_never_touches_the_history(self):
        from app.claims import KIND_ROLE, record_claim
        fake, calls = sql_recorder()
        with patch("app.db.arcadedb.run_sql", fake):
            record_claim(kind=KIND_ROLE, from_id="p", to_id="ms", source_id="sec", role="CEO")
        assert [c[0].split()[0] for c in calls] == ["UPDATE"]


class TestBulkHooks:
    def test_the_bulk_writer_appends_the_rows_to_its_claim_script(self):
        from app.claims import KIND_OWNS, claim_key
        from app.scraper.bulk_import import _BatchWriter
        key = claim_key(KIND_OWNS, "e1", "e2", "gleif")
        fake, _ = sql_recorder(stored=claim(60.0, claim_key=key, from_id="e1", to_id="e2", source_id="gleif"))
        scripts = []
        with patch("app.db.arcadedb.run_sql", fake), \
             patch("app.scraper.bulk_import.run_sqlscript",
                   side_effect=lambda script, params=None: scripts.append((script, params))):
            b = _BatchWriter(batch_size=100)
            b.owns("e1", "Entity", "e2", {"stake_percent": 75.0, "source_id": "gleif", "source_date": "2026-10-01"})
            b.flush()
        claim_script = next(s for s, _ in scripts if "UPDATE Claim" in s)
        assert claim_script.count("CREATE VERTEX StakeHistory") == 2
        assert claim_script.index("UPDATE Claim") < claim_script.index("CREATE VERTEX StakeHistory")

    def test_the_psc_refresh_appends_the_rows_to_its_update_script(self):
        from types import SimpleNamespace
        from app.claims import KIND_OWNS, claim_key
        from app.scraper.ch_psc_incremental import _PscEdgeWriter
        key = claim_key(KIND_OWNS, "p1", "gb-coh:1", "ch-psc")
        fake, _ = sql_recorder(stored=claim(25.0, claim_key=key, from_id="p1", to_id="gb-coh:1", source_id="ch-psc"))
        mapped = SimpleNamespace(
            owner_id="p1", owner_label="Person", company_id="gb-coh:1", self_link="/l/1",
            edge_props={"stake_percent": 50.0, "source_id": "ch-psc", "source_date": "2026-10-01",
                        "since": "2016-04-06", "since_basis": "register_start", "psc_self_link": "/l/1"})
        scripts = []
        with patch("app.db.arcadedb.run_sql", fake), \
             patch("app.scraper.ch_psc_incremental._flush_script",
                   side_effect=lambda script, params: scripts.append(script)):
            _PscEdgeWriter()._flush_updates([mapped])
        assert scripts and scripts[0].count("CREATE VERTEX StakeHistory") == 2
        assert "UPDATE Claim" in scripts[0]


# ── The readers ───────────────────────────────────────────────────────────────

class TestTimelineCrossings:
    ROWS = [row(4.8, None, "2024-02-13"), row(7.3, 4.8, "2025-02-13"), row(9.0, 7.3, "2025-06-30"),
            row(100.0, 40.0, "2020-01-01", src="gleif", frm="ms", to="li")]

    def test_only_the_crossings_reach_the_timeline_with_their_party(self, client, fake_db, monkeypatch):
        fake_db.queue([], [], [])
        monkeypatch.setattr("app.stake_history.history_for_entity", lambda eid: self.ROWS)
        monkeypatch.setattr("app.routers.relationships._nodes_by_id",
                            lambda ids: {"br": {"id": "br", "name": "BlackRock"}})
        events = client.get("/relationships/history/ms").json()
        stakes = [e for e in events if e["kind"].startswith("stake_")]
        assert len(stakes) == 1
        e = stakes[0]
        assert e["kind"] == "stake_reached" and e["direction"] == "in"
        assert e["party"]["name"] == "BlackRock" and e["since"] == "2025-02-13"
        assert (e["stake_percent"], e["previous_percent"], e["source_id"]) == (7.3, 4.8, "sec")

    def test_an_outbound_crossing_names_the_holding(self, client, fake_db, monkeypatch):
        fake_db.queue([], [], [])
        monkeypatch.setattr("app.stake_history.history_for_entity",
                            lambda eid: [row(3.0, 7.0, "2025-01-01", frm="ms", to="li")])
        monkeypatch.setattr("app.routers.relationships._nodes_by_id", lambda ids: {})
        e = [e for e in client.get("/relationships/history/ms").json() if e["kind"].startswith("stake_")][0]
        assert e["kind"] == "stake_dropped" and e["direction"] == "out" and e["party"] == {"id": "li"}

    def test_an_unreadable_table_leaves_the_timeline_standing(self, client, fake_db, monkeypatch):
        fake_db.queue([], [], [])
        def boom(eid):
            raise RuntimeError("no such type")
        monkeypatch.setattr("app.stake_history.history_for_entity", boom)
        r = client.get("/relationships/history/ms")
        assert r.status_code == 200 and r.json() == []


class TestStakeHistoryEndpoint:
    def test_rows_come_newest_first_with_the_source_named(self, client, monkeypatch):
        monkeypatch.setattr("app.stake_history.history_for_pair",
                            lambda f, t: [row(7.3, 4.8, "2025-02-13"), row(4.8, None, "2024-02-13")])
        monkeypatch.setattr("app.routers.relationships._source_names", lambda: {"sec": "SEC EDGAR"})
        monkeypatch.setattr("app.merged_ids.canonical_id", lambda i: i)
        r = client.get("/relationships/stake-history", params={"from_id": "br", "to_id": "ms"})
        assert r.status_code == 200
        assert [(x["as_of"], x["source_name"]) for x in r.json()] == [("2025-02-13", "SEC EDGAR"), ("2024-02-13", "SEC EDGAR")]

    def test_both_ids_are_required(self, client):
        assert client.get("/relationships/stake-history", params={"from_id": "br"}).status_code == 422


class TestTimeTravelStake:
    def test_the_stake_on_the_date_replaces_the_edges_where_history_exists(self, monkeypatch):
        from app.routers.search import _attach_stake_history
        rows = [row(4.8, None, "2024-02-13"), row(7.3, 4.8, "2025-02-13"),
                row(100.0, 40.0, "2020-01-01", src="gleif", frm="ms", to="li")]
        monkeypatch.setattr("app.stake_history.history_for_entity", lambda eid: rows)
        owners = [{"owner": {"id": "br"}, "relationship": {"stake_percent": 7.3, "source_id": "sec"}},
                  {"owner": {"id": "vg"}, "relationship": {"stake_percent": 8.0, "source_id": "sec"}}]
        subs = [{"entity": {"id": "li"}, "relationship": {"stake_percent": 100.0, "source_id": "gleif"}}]
        _attach_stake_history("ms", owners, subs, "2024-12-31")
        assert owners[0]["relationship"] == {"stake_percent": 4.8, "source_id": "sec", "stake_as_of": "2024-02-13",
                                             "stake_history": True}
        assert owners[1]["relationship"] == {"stake_percent": 8.0, "source_id": "sec"}      # no history: the edge's
        # the one row for the holding says 100 % as of 2020 (40 % before it): on a
        # later day that statement is the one in force
        assert subs[0]["relationship"]["stake_percent"] == 100.0 and subs[0]["relationship"]["stake_as_of"] == "2020-01-01"

    def test_in_the_present_the_rows_are_only_marked(self, monkeypatch):
        from app.routers.search import _attach_stake_history
        monkeypatch.setattr("app.stake_history.history_for_entity",
                            lambda eid: [row(4.8, None, "2024-02-13"), row(7.3, 4.8, "2025-02-13")])
        owners = [{"owner": {"id": "br"}, "relationship": {"stake_percent": 6.1, "source_id": "sec"}},
                  {"owner": {"id": "vg"}, "relationship": {"stake_percent": 8.0, "source_id": "sec"}}]
        _attach_stake_history("ms", owners, [], None)
        assert owners[0]["relationship"] == {"stake_percent": 6.1, "source_id": "sec", "stake_history": True}
        assert owners[1]["relationship"] == {"stake_percent": 8.0, "source_id": "sec"}

    def test_the_mark_is_per_pair_whatever_the_source(self, monkeypatch):
        # another source's series still means "there is history behind this row"
        from app.routers.search import _attach_stake_history
        monkeypatch.setattr("app.stake_history.history_for_entity", lambda eid: [row(4.8, None, "2024-02-13", src="13f")])
        owners = [{"owner": {"id": "br"}, "relationship": {"stake_percent": 7.3, "source_id": "sec"}}]
        _attach_stake_history("ms", owners, [], "2024-12-31")
        assert owners[0]["relationship"] == {"stake_percent": 7.3, "source_id": "sec", "stake_history": True}

    def test_an_unreadable_table_leaves_the_profile_standing(self, monkeypatch):
        from app.routers.search import _attach_stake_history
        def boom(eid):
            raise RuntimeError("no such type")
        monkeypatch.setattr("app.stake_history.history_for_entity", boom)
        owners = [{"owner": {"id": "br"}, "relationship": {"stake_percent": 7.3, "source_id": "sec"}}]
        _attach_stake_history("ms", owners, [], "2024-12-31")
        assert owners[0]["relationship"] == {"stake_percent": 7.3, "source_id": "sec"}

    def test_another_sources_history_does_not_touch_the_stake(self, monkeypatch):
        from app.routers.search import _attach_stake_history
        monkeypatch.setattr("app.stake_history.history_for_entity", lambda eid: [row(4.8, None, "2024-02-13", src="13f")])
        owners = [{"owner": {"id": "br"}, "relationship": {"stake_percent": 7.3, "source_id": "sec"}}]
        _attach_stake_history("ms", owners, [], "2024-12-31")
        assert "stake_as_of" not in owners[0]["relationship"] and owners[0]["relationship"]["stake_percent"] == 7.3

    def test_no_history_means_no_work(self, monkeypatch):
        from app.routers.search import _attach_stake_history
        monkeypatch.setattr("app.stake_history.history_for_entity", lambda eid: [])
        owners = [{"owner": {"id": "br"}, "relationship": {"stake_percent": 7.3, "source_id": "sec"}}]
        _attach_stake_history("ms", owners, [], "2024-12-31")
        assert owners[0]["relationship"] == {"stake_percent": 7.3, "source_id": "sec"}


# ── One statement per run ────────────────────────────────────────────────────

class TestOneStatementPerRun:
    """A runner that writes the same claim twice in one run (the SEC company
    scrape: a holder's 13D/G figure, then their Form 4 figure) states the LAST
    value against the pre-run claim — never a flap through the first."""

    @staticmethod
    def _write(stake, **kw):
        from app.claims import KIND_OWNS, record_claim
        record_claim(kind=KIND_OWNS, from_id="em", to_id="tsla", source_id="sec",
                     stake_percent=stake, **kw)

    @staticmethod
    def _creates(calls):
        return [(p["sh_stake_percent__0"], p["sh_previous_percent__0"], p["sh_filing_type__0"])
                for s, p in calls if s.startswith("CREATE VERTEX StakeHistory")]

    def _stored(self, stake=28.4419):
        from app.claims import KIND_OWNS, claim_key
        key = claim_key(KIND_OWNS, "em", "tsla", "sec")
        return claim(stake, claim_key=key, from_id="em", to_id="tsla", filing_type="Form 4",
                     source_date="2026-06-17", denominator_date="2026-07-16")

    def test_the_last_write_is_the_statement_and_the_flap_is_never_recorded(self):
        from app.stake_history import one_statement_per_run
        fake, calls = sql_recorder(stored=self._stored())
        with patch("app.db.arcadedb.run_sql", fake):
            with one_statement_per_run():
                self._write(19.9, filing_type="13G/A", event_date="2026-06-16")
                self._write(28.4188, filing_type="Form 4", source_date="2026-06-17",
                            denominator_date="2026-10-15")
                assert not any(s.startswith("CREATE") for s, _ in calls)   # nothing until the end
        verbs = [s.split()[0] + (" claim" if "FROM Claim WHERE claim_key IN" in s else
                                 " history" if "StakeHistory" in s else "") for s, _ in calls]
        assert verbs == ["SELECT claim", "UPDATE", "UPDATE", "SELECT history",
                         "CREATE history", "CREATE history"]
        assert self._creates(calls) == [(28.4419, None, "Form 4"), (28.4188, 28.4419, "Form 4")]
        assert not any(p.get("sh_stake_percent__0") == 19.9 for _, p in calls)
        assert [p["sh_as_of__0"] for s, p in calls if s.startswith("CREATE")] == ["2026-07-16", "2026-10-15"]

    def test_a_flap_back_to_the_stored_number_writes_nothing(self):
        from app.stake_history import one_statement_per_run
        fake, calls = sql_recorder(stored=self._stored())
        with patch("app.db.arcadedb.run_sql", fake):
            with one_statement_per_run():
                self._write(19.9, filing_type="13G/A")
                self._write(28.4419, filing_type="Form 4")
        assert [s.split()[0] for s, _ in calls] == ["SELECT", "UPDATE", "UPDATE"]

    def test_the_stored_claim_is_read_once_per_key_at_its_first_write(self):
        from app.claims import KIND_OWNS, record_claim
        from app.stake_history import one_statement_per_run
        fake, calls = sql_recorder(stored=self._stored())
        with patch("app.db.arcadedb.run_sql", fake):
            with one_statement_per_run():
                self._write(19.9, filing_type="13G/A")
                record_claim(kind=KIND_OWNS, from_id="x", to_id="tsla", source_id="sec",
                             stake_percent=6.0, filing_type="13G")
                self._write(27.0, filing_type="Form 4")
                self._write(26.0, filing_type="Form 4")
        pre_reads = [p["keys"] for s, p in calls if "FROM Claim WHERE claim_key IN" in s]
        assert len(pre_reads) == 2 and all(len(k) == 1 for k in pre_reads)
        assert self._creates(calls) == [(28.4419, None, "Form 4"), (26.0, 28.4419, "Form 4")]

    def test_outside_the_block_history_is_immediate_again(self):
        from app.stake_history import one_statement_per_run
        fake, calls = sql_recorder(stored=self._stored())
        with patch("app.db.arcadedb.run_sql", fake):
            with one_statement_per_run():
                pass
            self._write(19.9, filing_type="13G/A")
        assert [s.split()[0] for s, _ in calls] == ["SELECT", "SELECT", "UPDATE", "CREATE", "CREATE"]

    def test_a_failed_pre_read_skips_that_key_only_and_raises_nothing(self):
        from app.claims import KIND_OWNS, record_claim
        from app.stake_history import one_statement_per_run
        inner, calls = sql_recorder(stored=self._stored())

        def fake(sql, params=None, **kw):
            if "FROM Claim WHERE claim_key IN" in sql and self._stored()["claim_key"] not in params["keys"]:
                raise RuntimeError("db away")
            return inner(sql, params, **kw)
        with patch("app.db.arcadedb.run_sql", fake):
            with one_statement_per_run():
                record_claim(kind=KIND_OWNS, from_id="x", to_id="tsla", source_id="sec",
                             stake_percent=6.0, filing_type="13G")
                self._write(27.0, filing_type="Form 4")
        assert self._creates(calls) == [(28.4419, None, "Form 4"), (27.0, 28.4419, "Form 4")]

    def test_an_exception_in_the_run_still_writes_the_rows_of_what_landed(self):
        from app.stake_history import one_statement_per_run
        fake, calls = sql_recorder(stored=self._stored())
        with patch("app.db.arcadedb.run_sql", fake):
            with pytest.raises(RuntimeError, match="later"):
                with one_statement_per_run():
                    self._write(27.0, filing_type="Form 4")
                    raise RuntimeError("later")
        assert self._creates(calls) == [(28.4419, None, "Form 4"), (27.0, 28.4419, "Form 4")]

    def test_nested_blocks_join_the_outer_run(self):
        from app.stake_history import one_statement_per_run
        fake, calls = sql_recorder(stored=self._stored())
        with patch("app.db.arcadedb.run_sql", fake):
            with one_statement_per_run():
                with one_statement_per_run():
                    self._write(19.9, filing_type="13G/A")
                assert not any(s.startswith("CREATE") for s, _ in calls)
                self._write(28.4419, filing_type="Form 4")
        assert not any(s.startswith("CREATE") for s, _ in calls)

    def test_usable_as_a_decorator(self):
        from app.stake_history import one_statement_per_run

        @one_statement_per_run()
        def scrape():
            self._write(19.9, filing_type="13G/A")
            self._write(28.4188, filing_type="Form 4")
        fake, calls = sql_recorder(stored=self._stored())
        with patch("app.db.arcadedb.run_sql", fake):
            scrape()
        assert self._creates(calls) == [(28.4419, None, "Form 4"), (28.4188, 28.4419, "Form 4")]

    def test_a_role_claim_inside_the_run_is_written_and_ignored(self):
        from app.claims import KIND_ROLE, record_claim
        from app.stake_history import one_statement_per_run
        fake, calls = sql_recorder()
        with patch("app.db.arcadedb.run_sql", fake):
            with one_statement_per_run():
                record_claim(kind=KIND_ROLE, from_id="p", to_id="tsla", source_id="sec", role="CEO")
        assert [s.split()[0] for s, _ in calls] == ["UPDATE"]


class TestStatementDatedBeforeTheOneItReplaces:
    """Musk → Tesla: the Form 4 claim (28.4 % of record, restated against the
    10-Q total of 2026-10-15) is replaced by his 13G/A statement (19.9 %, as of
    2026-06-16). The replaced statement is dated AFTER the new one, so it is not
    kept as a prior row — time travel would show it after the 13G/A's day."""

    OLD = dict(stake=28.4188, filing_type="Form 4", event_date=None, source_date="2026-06-17",
               denominator_date="2026-10-15")
    NEW = dict(stake=19.9, filing_type="13G/A", event_date="2026-06-16", source_date="2026-06-17",
               denominator_date=None)

    def test_no_replaced_row_but_the_previous_number_is_kept(self):
        rows = rows_for(claim(**self.OLD), claim(**self.NEW), had_history=False, now=NOW)
        assert [(r["stake_percent"], r["previous_percent"], r["as_of"], r["filing_type"]) for r in rows] == [
            (19.9, 28.4188, "2026-06-16", "13G/A")]
        assert stakes_as_of(rows, "2026-12-31")[("br", "ms", "sec")]["stake_percent"] == 19.9

    def test_a_same_day_replacement_keeps_the_replaced_statement(self):
        old = claim(28.4188, filing_type="Form 4", source_date="2026-06-16", denominator_date=None)
        rows = rows_for(old, claim(**self.NEW), had_history=False, now=NOW)
        assert [(r["stake_percent"], r["as_of"]) for r in rows] == [(28.4188, "2026-06-16"), (19.9, "2026-06-16")]

    def test_the_later_statement_still_keeps_the_earlier_one(self):
        rows = rows_for(claim(**self.NEW), claim(**self.OLD), had_history=False, now=NOW)
        assert [(r["stake_percent"], r["as_of"]) for r in rows] == [(19.9, "2026-06-16"), (28.4188, "2026-10-15")]
