"""
The PSC refresh against a real ArcadeDB.

Everything load-bearing here is SQL a mocked session would accept however it were
written, and two of the properties have never had any coverage at all:

* **A second run must change nothing.** The bulk importer's `CREATE EDGE` doubles
  every edge on a re-import, cleaned up afterwards by a whole-database dedup pass.
  That is tolerable once, for a load into an empty graph, and unacceptable for a
  refresh. Nothing in this repo re-imported a snapshot twice before this file.
* **`ceased_on` must close an edge, and losing it must reopen one.** 17.9% of the
  register carries a cessation date and nothing tested it.

The write path also depends on ArcadeDB matching an edge by an indexed property —
SQL cannot reach an edge through its endpoints, so `psc_self_link` is the only
handle the refresh has.
"""
import json
import zipfile

import pytest

pytestmark = pytest.mark.integration

SRC = "src-psc"
CRED = 97


def _individual(company, key, ceased=None, natures=None, name=None):
    data = {
        "kind": "individual-person-with-significant-control",
        "name": name or f"Owner {key}",
        "nationality": "British",
        "natures_of_control": natures or ["ownership-of-shares-75-to-100-percent"],
        "notified_on": "2016-04-06",
        "links": {"self": f"/company/{company}/persons-with-significant-control/individual/{key}"},
    }
    if ceased:
        data["ceased_on"] = ceased
    return {"company_number": company, "data": data}


def _corporate(company, key, reg_number=None):
    ident = {"country_registered": "England"}
    if reg_number:
        ident["registration_number"] = reg_number
    return {"company_number": company, "data": {
        "kind": "corporate-entity-person-with-significant-control",
        "name": f"Holdco {key}", "identification": ident,
        "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
        "links": {"self": f"/company/{company}/persons-with-significant-control/corporate/{key}"},
    }}


def _snapshot(tmp_path, records, date_, name="psc.zip"):
    z = tmp_path / name
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(f"persons-with-significant-control-snapshot-{date_}.txt",
                    "\n".join(json.dumps(r) for r in records))
    return str(z)


def _edges(it_db):
    return {r["link"]: r for r in it_db.run_command(
        "MATCH ()-[r:OWNS]->() WHERE r.psc_self_link IS NOT NULL "
        "RETURN r.psc_self_link AS link, r.until AS until, r.until_reason AS reason, "
        "r.stake_percent AS stake")}


@pytest.fixture
def loaded(it_db, tmp_path):
    """Snapshot A imported in full, with the digest it will be diffed against."""
    from app.scraper.ch_psc_incremental import mark_psc_load_done
    from app.scraper.companies_house_psc import import_ch_psc

    records = [
        _individual("00000001", "steady"),                       # never changes
        _individual("00000002", "ceases"),                       # gains ceased_on in B
        _individual("00000003", "reopens", ceased="2020-01-31"),  # loses it in B
        _individual("00000004", "restake"),                      # natures change in B
        _individual("00000005", "vanishes"),                     # absent from B
        _corporate("00000006", "corp"),                          # gains a reg number in B
    ]
    snap = _snapshot(tmp_path, records, "2026-07-27", "a.zip")
    digest = str(tmp_path / "psc-digest.tsv.gz")
    import_ch_psc(snap, SRC, CRED, digest_out=digest)
    mark_psc_load_done("full")
    return {"digest": digest, "tmp": tmp_path}


def _apply(loaded, records, date_="2026-07-28", **kw):
    """Digest snapshot B, diff it against the baseline, apply it."""
    from app.scraper import ch_psc_incremental as inc

    snap = _snapshot(loaded["tmp"], records, date_, f"b-{date_}.zip")
    new_digest = str(loaded["tmp"] / f"new-{date_}.tsv.gz")
    inc.write_digest(snap, new_digest)
    diff = inc.diff_digests(loaded["digest"], new_digest)
    counts = inc.apply_diff(snap, diff, SRC, CRED, until_date=date_, **kw)
    inc.rotate_digest(new_digest, loaded["digest"])
    return diff, counts


def _b_records(**over):
    recs = {
        "steady": _individual("00000001", "steady"),
        "ceases": _individual("00000002", "ceases", ceased="2026-07-01"),
        "reopens": _individual("00000003", "reopens"),
        "restake": _individual("00000004", "restake",
                               natures=["ownership-of-shares-25-to-50-percent"]),
        "corp": _corporate("00000006", "corp", reg_number="12345678"),
        "new": _individual("00000007", "arrives"),
    }
    recs.update(over)
    return [r for r in recs.values() if r is not None]


class TestWhatTheRefreshDoes:
    def test_only_what_moved_is_touched(self, loaded, it_db):
        # Five changed of six records; the untouched one is not rewritten.
        diff, counts = _apply(loaded, _b_records())
        assert "/company/00000001/persons-with-significant-control/individual/steady" \
            not in diff.touched
        assert counts["touched"] == 5 and counts["closed"] == 1

    def test_a_cessation_closes_the_edge(self, loaded, it_db):
        # The commonest real change, and previously untested anywhere.
        _apply(loaded, _b_records())
        e = _edges(it_db)["/company/00000002/persons-with-significant-control/individual/ceases"]
        assert e["until"] == "2026-07-01"
        assert e["reason"] is None, "a cessation is not a withdrawal"

    def test_a_correction_reopens_a_closed_edge(self, loaded, it_db):
        # `until` is written unconditionally rather than COALESCEd. GLEIF coalesces
        # because its delta records are partial; a PSC snapshot record is the whole
        # current truth, so a removed `ceased_on` must reopen the edge or a corrected
        # PSC stays closed forever.
        _apply(loaded, _b_records())
        e = _edges(it_db)["/company/00000003/persons-with-significant-control/individual/reopens"]
        assert e["until"] is None

    def test_a_changed_stake_is_written(self, loaded, it_db):
        _apply(loaded, _b_records())
        e = _edges(it_db)["/company/00000004/persons-with-significant-control/individual/restake"]
        assert e["stake"] == 25

    def test_a_vanished_record_is_closed_not_deleted(self, loaded, it_db):
        # No cessation date and no reason from Companies House — a withdrawal or a
        # correction. Marked as such, because "controlled until this date" and "the
        # register says this was wrong" are different facts.
        _apply(loaded, _b_records())
        e = _edges(it_db)["/company/00000005/persons-with-significant-control/individual/vanishes"]
        assert e["until"] == "2026-07-28", "dated with the snapshot, not with now()"
        assert e["reason"] == "withdrawn"

    def test_a_vanished_record_keeps_its_person_and_claim(self, loaded, it_db):
        _apply(loaded, _b_records())
        link = "/company/00000005/persons-with-significant-control/individual/vanishes"
        assert it_db.run_command("MATCH (p:Person {id:$id}) RETURN p.id AS id",
                                 {"id": f"chpsc:{link}"}), "the person was deleted"

        # A Claim is keyed on (kind, from, to, source) — it has never carried the
        # appointment link — so it has to be found through the edge's endpoints.
        # Which is exactly why closing one is easy to forget: nothing about the
        # close path mentions the Claim, and the provenance is then left asserting
        # a holding the graph has ended.
        claims = it_db.run_command(
            "MATCH (c:Claim) WHERE c.from_id = $f AND c.to_id = $t RETURN c.until AS until",
            {"f": f"chpsc:{link}", "t": "gb-coh:00000005"})
        assert claims, "the claim was deleted along with the record"
        assert claims[0]["until"] == "2026-07-28", "the claim still asserts a live holding"

    def test_a_new_record_creates_its_edge(self, loaded, it_db):
        _apply(loaded, _b_records())
        assert "/company/00000007/persons-with-significant-control/individual/arrives" in _edges(it_db)

    def test_an_untouched_record_keeps_its_edge_open(self, loaded, it_db):
        _apply(loaded, _b_records())
        e = _edges(it_db)["/company/00000001/persons-with-significant-control/individual/steady"]
        assert e["until"] is None


class TestApplyingItTwiceChangesNothing:
    """The property the whole design rests on, and the one with no prior coverage."""

    def test_the_second_run_is_a_no_op(self, loaded, it_db):
        _apply(loaded, _b_records())
        before = _edges(it_db)
        count_before = it_db.run_command("MATCH ()-[r:OWNS]->() RETURN count(r) AS n")[0]["n"]

        # Re-digesting the same snapshot against the rotated baseline: the diff is
        # empty, so this is stronger than "the writes are idempotent" — nothing is
        # attempted at all.
        diff, counts = _apply(loaded, _b_records(), date_="2026-07-28")
        assert diff.total == 0 and counts["touched"] == 0 and counts["closed"] == 0

        assert it_db.run_command("MATCH ()-[r:OWNS]->() RETURN count(r) AS n")[0]["n"] \
            == count_before, "a second run duplicated edges"
        assert _edges(it_db) == before

    def test_edges_are_not_duplicated_when_a_record_changes_twice(self, loaded, it_db):
        # The failure the bulk writer's CREATE EDGE would cause, exercised over two
        # consecutive changes to the same appointment.
        link = "/company/00000004/persons-with-significant-control/individual/restake"
        _apply(loaded, _b_records(), date_="2026-07-28")
        _apply(loaded, _b_records(
            restake=_individual("00000004", "restake",
                                natures=["ownership-of-shares-50-to-75-percent"])),
            date_="2026-07-29")
        rows = it_db.run_command(
            "MATCH ()-[r:OWNS]->() WHERE r.psc_self_link = $l RETURN r.stake_percent AS s",
            {"l": link})
        assert len(rows) == 1 and rows[0]["s"] == 50


class TestOnlyExisting:
    def test_a_company_the_graph_does_not_hold_is_skipped(self, loaded, it_db):
        # A curated database must not be dragged towards the whole register.
        _, counts = _apply(loaded, _b_records(
            outsider=_individual("09999999", "outsider")), only_existing=True)
        assert counts["not_here"] >= 1
        assert not it_db.run_command("MATCH (e:Entity {id:'gb-coh:09999999'}) RETURN e.id AS id")

    def test_companies_the_graph_holds_are_still_refreshed(self, loaded, it_db):
        _apply(loaded, _b_records(), only_existing=True)
        e = _edges(it_db)["/company/00000002/persons-with-significant-control/individual/ceases"]
        assert e["until"] == "2026-07-01"


class TestTheCorporateOwnerIdCanMove:
    def test_gaining_a_registration_number_does_not_orphan_the_edge(self, loaded, it_db):
        # `_entity_psc_id` keys a corporate PSC on its UK number when it has one, so
        # a record gaining `registration_number` changes the owner node id. The edge
        # is found by its self link, so it is still exactly one edge afterwards.
        _apply(loaded, _b_records())
        link = "/company/00000006/persons-with-significant-control/corporate/corp"
        rows = it_db.run_command(
            "MATCH (o)-[r:OWNS]->() WHERE r.psc_self_link = $l RETURN o.id AS oid", {"l": link})
        assert len(rows) == 1


class TestTheOnlyExistingGate:
    def test_it_asks_only_about_companies_house_companies(self, loaded, it_db):
        """The gate is a set held in memory — ~5.6M ids on a full UK load, around
        620 MB. Widening it to every Entity would add GLEIF's millions for nothing:
        a PSC record's controlled company is always `gb-coh:`, so no other id can
        ever be consulted."""
        from app.scraper.ch_psc_incremental import existing_company_ids

        it_db.run_command("CREATE (e:Entity {id:'lei:SOMELEI', name:'Not a CH company'})")
        ids = existing_company_ids()

        assert "lei:SOMELEI" not in ids
        assert all(i.startswith("gb-coh:") for i in ids)
        assert "gb-coh:00000001" in ids, "the CH companies really are there"


class TestTheBaselineRecordsItsSnapshot:
    def test_a_load_with_a_digest_records_which_snapshot_it_was(self, it_db, tmp_path):
        """Otherwise the first refresh cannot tell how big a gap it is covering.

        Found by running the real thing: after a baseline import from the 27 July
        snapshot, a refresh against 20 August reported `gap_days: 1`. The churn
        allowance scales with the gap, so 24 days of legitimate change would have
        been measured against a single day's budget — and the staleness guard had
        nothing to compare against, so an *older* snapshot could be applied over a
        newer baseline.
        """
        from app.scraper.ch_psc_incremental import read_last_snapshot
        from app.scraper.runner import run_import_ch_psc
        from app.config import settings

        settings.SCRAPER_ENABLED = True
        settings.SCRAPER_BODS_UK_PSC_ENABLED = True
        snap = _snapshot(tmp_path, [_individual("00000001", "one")], "2026-07-27", "base.zip")
        run_import_ch_psc(snap, digest_out=str(tmp_path / "d.tsv.gz"))

        state = read_last_snapshot()
        assert state and state["snapshot_date"] == "2026-07-27"

    def test_a_load_without_a_digest_records_no_snapshot(self, it_db, tmp_path):
        # No digest means no baseline to diff against, so claiming a snapshot was
        # "applied" would let a refresh run with nothing to compare to.
        from app.scraper.ch_psc_incremental import read_last_snapshot
        from app.scraper.runner import run_import_ch_psc
        from app.config import settings

        settings.SCRAPER_ENABLED = True
        settings.SCRAPER_BODS_UK_PSC_ENABLED = True
        snap = _snapshot(tmp_path, [_individual("00000002", "two")], "2026-07-27", "nodigest.zip")
        run_import_ch_psc(snap)
        assert read_last_snapshot() is None


class TestClaimsFollowTheRefresh:
    """The refresh previously touched only the edge: a claim's last_seen_at
    stayed frozen at bulk-import time and a NEW appointment got no claim at
    all — while close_vanished diligently closed claims this path had never
    created."""

    def _claim_for(self, it_db, company):
        from app.db.arcadedb import run_sql
        rows = run_sql(
            "SELECT from_id, to_id, source_id, stake_percent, ownership_type, "
            "first_seen_at, last_seen_at, credibility_score FROM Claim "
            "WHERE to_id = :c", {"c": f"gb-coh:{company}"})
        return rows

    def test_a_new_appointment_gets_a_claim(self, loaded, it_db):
        _apply(loaded, _b_records())
        rows = self._claim_for(it_db, "00000007")
        assert len(rows) == 1
        assert rows[0]["source_id"] == SRC
        assert rows[0]["credibility_score"] == CRED

    def test_a_refresh_moves_last_seen_and_keeps_first_seen(self, loaded, it_db):
        before = self._claim_for(it_db, "00000004")
        assert len(before) == 1, "the bulk import writes the baseline claim"
        _apply(loaded, _b_records())          # natures change → restake touched
        after = self._claim_for(it_db, "00000004")
        assert len(after) == 1, "a refresh UPSERTs the same claim_key"
        assert after[0]["first_seen_at"] == before[0]["first_seen_at"], \
            "COALESCE(first_seen_at, …) must keep the original sighting"
        assert after[0]["last_seen_at"] >= before[0]["last_seen_at"]
        assert after[0]["stake_percent"] != before[0]["stake_percent"], \
            "the refreshed claim records the new stake band"
