"""One active OWNS edge per pair, whichever source arrives second — against a real
ArcadeDB (app.scraper.owns_merge).

Every writer used to find only the edge it had drawn itself (SEC by source_id,
GLEIF by its marker, PSC by its appointment link), so a second source drew a
second edge: News Corp's timeline listed Dow Jones twice, and the dedup after
every GLEIF import deleted SEC's edge — its "listed since 2013" with it — for
the next SEC scrape to draw again. Each test here has a writer arrive at a pair
another source already holds.
"""
import pytest

pytestmark = pytest.mark.integration

LOWER = {"since": "2013-06-30", "since_basis": "first_listed",
         "since_source_url": "https://www.sec.gov/2013-ex21.htm"}
FIELDS = ("source_id", "credibility_score", "filing_type", "stake_percent", "source_url",
          "since", "since_basis", "since_source_url", "direct_or_indirect",
          "psc_self_link", "until", "interest_types")


def _nodes(it_db, *ids):
    for i in ids:
        it_db.run_command("CREATE (:Entity {id:$id, name:$id})", {"id": i})


def _draw(it_db, a, b, **props):
    sets = ", ".join(f"{k}: ${k}" for k in props)
    it_db.run_command(
        f"MATCH (a:Entity {{id:$a}}), (b:Entity {{id:$b}}) CREATE (a)-[:OWNS {{{sets}}}]->(b)",
        {"a": a, "b": b, **props})


def _edges(it_db, a, b, active=True):
    where = "WHERE r.until IS NULL " if active else ""
    ret = ", ".join(f"r.{f} AS {f}" for f in FIELDS)
    return [{f: row.get(f) for f in FIELDS} for row in it_db.run_command(
        f"MATCH (:Entity {{id:$a}})-[r:OWNS]->(:Entity {{id:$b}}) {where}RETURN {ret}",
        {"a": a, "b": b})]


def _claims(it_db, a, b):
    return {r["source_id"]: r for r in it_db.run_sql(
        "SELECT source_id, filing_type, since, stake_percent, until FROM Claim "
        "WHERE from_id = :a AND to_id = :b", {"a": a, "b": b})}


def _gleif_edge(it_db, since="2024-06-30", **extra):
    _draw(it_db, "nc", "dj", direct_or_indirect="direct", ownership_type="controlling",
          filing_type="RR", source_id="gleif", credibility_score=92,
          interest_types=["accountingConsolidation"], since=since, **extra)


def _psc_edge(it_db, link="/company/1/psc/a"):
    _draw(it_db, "nc", "dj", filing_type="PSC", stake_percent=75.0, ownership_type="controlling",
          source_id="psc", credibility_score=97, since="2016-04-06", psc_self_link=link,
          source_url="https://find-and-update.example.test/psc")


def _sec_ex21(owner="nc", owned="dj"):
    from app.scraper.sec_writer import _upsert_owns_sec
    _upsert_owns_sec(owner, owned, "sec", "controlling", "2026-08-07", None,
                     source_url="https://www.sec.gov/2026-ex21.htm", filing_type="EX-21",
                     filing_dates_the_stake=False)


# ── SEC arriving second ───────────────────────────────────────────────────────

class TestSecSharesTheEdge:
    def test_takes_over_a_gleif_edge_it_outranks_and_keeps_the_marker(self, it_db):
        _nodes(it_db, "nc", "dj")
        _gleif_edge(it_db)
        _sec_ex21()
        [edge] = _edges(it_db, "nc", "dj")
        assert (edge["source_id"], edge["filing_type"], edge["credibility_score"]) == ("sec", "EX-21", 98)
        assert edge["source_url"] == "https://www.sec.gov/2026-ex21.htm"
        assert edge["direct_or_indirect"] == "direct"      # GLEIF's structure stays
        assert edge["since"] == "2024-06-30"               # GLEIF's date: SEC stated none
        assert set(_claims(it_db, "nc", "dj")) == {"sec"}  # (GLEIF's claim was never seeded)

    def test_leaves_a_psc_stake_holding_the_answer(self, it_db):
        _nodes(it_db, "nc", "dj")
        _psc_edge(it_db)
        _sec_ex21()
        [edge] = _edges(it_db, "nc", "dj")
        assert (edge["source_id"], edge["stake_percent"], edge["filing_type"]) == ("psc", 75.0, "PSC")
        assert _claims(it_db, "nc", "dj")["sec"]["filing_type"] == "EX-21"

    def test_a_stated_earlier_start_is_combined_in(self, it_db):
        from app.scraper.sec_writer import _upsert_owns_sec
        _nodes(it_db, "fund", "dj")
        _draw(it_db, "fund", "dj", source_id="psc", credibility_score=97, stake_percent=30.0,
              since="2016-04-06", filing_type="PSC")
        # a 13D dates the stake and states one: it outranks and brings its date
        _upsert_owns_sec("fund", "dj", "sec", "significant", "2011-03-01", 31.0,
                         filing_type="SC 13D")
        [edge] = _edges(it_db, "fund", "dj")
        assert (edge["source_id"], edge["stake_percent"], edge["since"]) == ("sec", 31.0, "2011-03-01")

    def test_rescrapes_update_the_shared_edge_and_never_draw_a_second(self, it_db):
        _nodes(it_db, "nc", "dj")
        _gleif_edge(it_db)
        _sec_ex21()
        _sec_ex21()
        assert len(_edges(it_db, "nc", "dj", active=False)) == 1


class TestTheListedSinceBound:
    def test_lands_on_a_shared_edge_another_source_holds(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        _nodes(it_db, "nc", "dj")
        _psc_edge(it_db)
        _sec_ex21()
        assert set_since_lower_bound("nc", "dj", "2013-06-30", LOWER["since_source_url"]) is True
        [edge] = _edges(it_db, "nc", "dj")
        assert {k: edge[k] for k in LOWER} == LOWER
        assert edge["source_id"] == "psc"                  # the answer is untouched

    def test_needs_an_exhibit_21_claim_for_the_pair(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        _nodes(it_db, "nc", "dj")
        _psc_edge(it_db)                                   # no SEC list ever named it
        assert set_since_lower_bound("nc", "dj", "2013-06-30", None) is False
        assert _edges(it_db, "nc", "dj")[0]["since"] == "2016-04-06"


# ── GLEIF arriving second ─────────────────────────────────────────────────────

class TestGleifSharesTheEdge:
    def _upsert(self, marker="direct", since="2024-06-30"):
        from app.scraper.gleif_incremental import _owns_edge_upsert
        return _owns_edge_upsert("nc", "dj", "LEI0000000000000DJ00", marker, "gleif", 92, since)

    def test_adopts_an_sec_edge_marking_it_and_leaving_its_answer(self, it_db):
        _nodes(it_db, "nc", "dj")
        _sec_ex21()
        from app.scraper.sec_writer import set_since_lower_bound
        set_since_lower_bound("nc", "dj", "2013-06-30", LOWER["since_source_url"])
        assert self._upsert() == "adopted"
        [edge] = _edges(it_db, "nc", "dj")
        assert edge["direct_or_indirect"] == "direct"
        assert edge["interest_types"] == ["accountingConsolidation"]
        assert (edge["source_id"], edge["credibility_score"]) == ("sec", 98)
        assert {k: edge[k] for k in LOWER} == LOWER        # 2013 or earlier beats 2024
        assert set(_claims(it_db, "nc", "dj")) == {"sec", "gleif"}

    def test_later_deltas_update_without_stamping_its_credibility(self, it_db):
        _nodes(it_db, "nc", "dj")
        _sec_ex21()
        self._upsert()
        assert self._upsert(since="1991-01-20") == "updated"
        [edge] = _edges(it_db, "nc", "dj")
        assert (edge["source_id"], edge["credibility_score"]) == ("sec", 98)
        assert (edge["since"], edge["since_basis"]) == ("1991-01-20", None)   # earlier stated start

    def test_takes_over_an_edge_it_outranks(self, it_db):
        _nodes(it_db, "nc", "dj")
        _draw(it_db, "nc", "dj", source_id="oc", credibility_score=85, ownership_type="unknown")
        assert self._upsert() == "adopted"
        [edge] = _edges(it_db, "nc", "dj")
        assert (edge["source_id"], edge["credibility_score"], edge["filing_type"]) == ("gleif", 92, "RR")
        assert edge["source_url"] == "https://search.gleif.org/#/record/LEI0000000000000DJ00"

    def test_retiring_does_not_close_an_edge_another_source_holds(self, it_db):
        from app.merged_ids import canonical_id  # noqa: F401 - _close_owns canonicalises lei: ids
        from app.scraper.gleif_incremental import _close_owns, _owns_edge_upsert
        _nodes(it_db, "lei:P", "lei:C")
        _draw(it_db, "lei:P", "lei:C", source_id="sec", credibility_score=98, filing_type="EX-21")
        _owns_edge_upsert("lei:P", "lei:C", "C", "direct", "gleif", 92, None)
        assert _close_owns("P", "C", "direct", "2026-09-01", "gleif") == 0
        assert len(_edges(it_db, "lei:P", "lei:C")) == 1
        # …while GLEIF's own edge still closes
        _nodes(it_db, "lei:Q")
        _owns_edge_upsert("lei:P", "lei:Q", "Q", "direct", "gleif", 92, None)
        assert _close_owns("P", "Q", "direct", "2026-09-01", "gleif") == 1


# ── UK PSC arriving second ────────────────────────────────────────────────────

def _mapped(link="/company/1/psc/a", stake=75.0, since="2016-04-06", until=None):
    from app.scraper.companies_house_psc import PscMapped
    return PscMapped(
        kind_cat="entity", self_link=link, company_id="dj", company_props={},
        owner_id="nc", owner_label="Entity", owner_props={},
        edge_props={"filing_type": "PSC", "stake_percent": stake, "voting_power_pct": None,
                    "ownership_type": "controlling", "interest_types": ["shareholding"],
                    "direct_or_indirect": None, "since": since, "until": until,
                    "source_id": "psc", "credibility_score": 97, "psc_self_link": link,
                    "source_url": "https://find-and-update.example.test/psc",
                    "source_date": since})


def _psc_write(*mapped):
    from app.scraper.ch_psc_incremental import _PscEdgeWriter
    w = _PscEdgeWriter()
    for m in mapped:
        w.add(m)
    w.flush()
    return w.counts


class TestPscSharesTheEdge:
    def test_adopts_an_sec_list_edge_its_stake_outranks(self, it_db):
        _nodes(it_db, "nc", "dj")
        _gleif_edge(it_db, since=None)
        _sec_ex21()
        counts = _psc_write(_mapped())
        assert counts["adopted"] == 1 and counts["created"] == 0
        [edge] = _edges(it_db, "nc", "dj")
        assert (edge["source_id"], edge["stake_percent"], edge["psc_self_link"]) == \
            ("psc", 75.0, "/company/1/psc/a")
        assert edge["direct_or_indirect"] == "direct"      # PSC's None does not wipe GLEIF's
        assert edge["since"] == "2016-04-06"

    def test_an_earlier_listed_since_bound_survives_the_nightly_update(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        _nodes(it_db, "nc", "dj")
        _sec_ex21()
        _psc_write(_mapped())
        set_since_lower_bound("nc", "dj", "2013-06-30", LOWER["since_source_url"])
        _psc_write(_mapped())                              # the next night, by link
        [edge] = _edges(it_db, "nc", "dj")
        assert {k: edge[k] for k in LOWER} == LOWER
        _psc_write(_mapped(since="2010-01-01"))            # a corrected, earlier stated start
        [edge] = _edges(it_db, "nc", "dj")
        assert (edge["since"], edge["since_basis"], edge["since_source_url"]) == ("2010-01-01", None, None)

    def test_records_only_its_claim_where_a_stake_filing_outranks_it(self, it_db):
        _nodes(it_db, "nc", "dj")
        _draw(it_db, "nc", "dj", source_id="sec", credibility_score=98, stake_percent=80.0,
              filing_type="SC 13D", since="2012-01-01")
        counts = _psc_write(_mapped())
        assert counts.get("claim_only") == 1 and counts["created"] == 0
        [edge] = _edges(it_db, "nc", "dj")
        assert (edge["source_id"], edge["stake_percent"], edge["psc_self_link"]) == ("sec", 80.0, None)
        assert _claims(it_db, "nc", "dj")["psc"]["stake_percent"] == 75.0

    def test_nightly_updates_leave_an_edge_a_stake_filing_took_over(self, it_db):
        from app.scraper.sec_writer import _upsert_owns_sec
        _nodes(it_db, "nc", "dj")
        _psc_write(_mapped())                              # PSC draws it
        _upsert_owns_sec("nc", "dj", "sec", "controlling", "2026-01-05", 80.0,
                         filing_type="SC 13D")             # SEC outranks and takes over
        _psc_write(_mapped())                              # PSC's next night, by link
        [edge] = _edges(it_db, "nc", "dj")
        assert (edge["source_id"], edge["stake_percent"]) == ("sec", 80.0)

    def test_a_ceased_appointment_keeps_its_own_closed_edge(self, it_db):
        _nodes(it_db, "nc", "dj")
        _sec_ex21()
        _psc_write(_mapped(link="/company/1/psc/old", until="2015-01-01"))
        assert [e["source_id"] for e in _edges(it_db, "nc", "dj")] == ["sec"]
        assert len(_edges(it_db, "nc", "dj", active=False)) == 2

    def test_a_vanished_record_does_not_close_an_edge_another_source_holds(self, it_db):
        from app.scraper.ch_psc_incremental import close_vanished
        from app.scraper.sec_writer import _upsert_owns_sec
        _nodes(it_db, "nc", "dj")
        _psc_write(_mapped())
        _upsert_owns_sec("nc", "dj", "sec", "controlling", "2026-01-05", 80.0, filing_type="SC 13D")
        close_vanished(["/company/1/psc/a"], "2026-09-01", source_id="psc")
        assert len(_edges(it_db, "nc", "dj")) == 1
        claims = _claims(it_db, "nc", "dj")
        assert claims["psc"]["until"] == "2026-09-01"      # PSC's own claim is closed
        assert claims["sec"]["until"] is None              # SEC's is not


# ── The cleanup folds what it deletes ─────────────────────────────────────────

class TestDedupFolds:
    def test_gleif_and_sec_fold_into_one_edge_carrying_both(self, it_db):
        from app.scraper.maintenance import deduplicate_owns_edges
        _nodes(it_db, "nc", "dj")
        _gleif_edge(it_db)
        _draw(it_db, "nc", "dj", source_id="sec", credibility_score=98, filing_type="EX-21",
              source_url="https://www.sec.gov/2026-ex21.htm", **LOWER)
        res = deduplicate_owns_edges()
        assert (res["duplicates_removed"], res["survivors_folded"]) == (1, 1)
        [edge] = _edges(it_db, "nc", "dj", active=False)
        assert edge["direct_or_indirect"] == "direct"      # GLEIF's edge survived…
        assert (edge["source_id"], edge["filing_type"]) == ("sec", "EX-21")   # …with SEC's answer
        assert {k: edge[k] for k in LOWER} == LOWER        # and the earlier bound

    def test_sec_and_psc_keep_the_stake_and_gain_the_bound(self, it_db):
        from app.scraper.maintenance import deduplicate_owns_edges
        _nodes(it_db, "nc", "dj")
        _psc_edge(it_db)
        _draw(it_db, "nc", "dj", source_id="sec", credibility_score=98, filing_type="EX-21", **LOWER)
        deduplicate_owns_edges()
        [edge] = _edges(it_db, "nc", "dj", active=False)
        assert (edge["source_id"], edge["stake_percent"], edge["psc_self_link"]) == \
            ("psc", 75.0, "/company/1/psc/a")
        assert {k: edge[k] for k in LOWER} == LOWER

    def test_a_same_source_double_is_only_deleted(self, it_db):
        from app.scraper.maintenance import deduplicate_owns_edges
        _nodes(it_db, "nc", "dj")
        _gleif_edge(it_db)
        _gleif_edge(it_db)
        res = deduplicate_owns_edges()
        assert (res["duplicates_removed"], res["survivors_folded"]) == (1, 0)


def test_the_timeline_lists_a_shared_pair_once(it_db):
    from app.routers.relationships import ownership_history_of
    _nodes(it_db, "nc", "dj")
    _gleif_edge(it_db)
    _sec_ex21()
    events, _ = ownership_history_of("nc", 100)
    assert [e["party"]["id"] for e in events if e["kind"] == "ownership_out"] == ["dj"]
