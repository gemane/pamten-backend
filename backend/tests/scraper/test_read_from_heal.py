"""
`heal-read-from`: the rules that stamp the grade onto what was written before
it existed, against a recorded database — which statements go out, with which
grade, and that nothing already stamped is touched (the WHERE carries
`read_from IS NULL`) — except by the one re-grade, which moves a schedule's
retired `prose` stamp to `form` and nothing else.
"""
from unittest.mock import patch

from app.scraper import read_from_heal as heal

SOURCES = {"GLEIF": "s-gleif", "UK PSC": "s-psc", "Wikidata": "s-wd",
           "OpenCorporates": "s-oc", "SEC EDGAR": "s-sec"}


def _run(dry_run=False, sources=SOURCES, batches=(0,)):
    """Record every SQL statement; each UPDATE answers with the next count in
    `batches` (then 0)."""
    seen: list[tuple[str, dict]] = []
    counts = iter(batches)

    def fake_sql(stmt, params=None):
        seen.append((stmt, params or {}))
        if stmt.startswith("UPDATE"):
            return [{"count": next(counts, 0)}]
        return [{"count": 3}]

    def fake_query(cypher, params=None):
        sid = sources.get(params["n"])
        return [{"id": sid}] if sid else []

    with patch.object(heal, "run_sql", side_effect=fake_sql), \
         patch.object(heal, "run_query", side_effect=fake_query):
        return heal.heal_read_from(dry_run=dry_run), seen


class TestWhatIsStamped:
    def test_every_statement_fills_only_what_is_unset(self):
        _, seen = _run()
        updates = [s for s, _ in seen if s.startswith("UPDATE")]
        fills = [s for s in updates if "read_from = :old" not in s]
        assert fills and all("read_from IS NULL" in s for s in fills)
        assert all(" LIMIT " in s for s in updates), "batched under the proxy timeout"

    def test_the_regrade_moves_a_schedules_prose_to_form_and_nothing_else(self):
        _, seen = _run()
        regrades = [(s, p) for s, p in seen if s.startswith("UPDATE") and "read_from = :old" in s]
        assert {s.split()[1] for s, _ in regrades} == {"OWNS", "Claim"}
        for s, p in regrades:
            assert (p["old"], p["grade"]) == ("prose", "form")
            assert "LIKE '13D%'" in s and "LIKE '13G%'" in s, "only a schedule — an Exhibit 21 paragraph stays prose"
            assert "source_id = :s" in s and p["s"] == "s-sec"
            assert "source_date" not in s, "exact by grade and form, no date proxy"

    def test_the_field_sources_get_field_on_edges_seats_and_claims(self):
        _, seen = _run()
        for name, sid in SOURCES.items():
            if name == "SEC EDGAR":
                continue
            tables = {s.split()[1] for s, p in seen
                      if s.startswith("UPDATE") and p.get("s") == sid and p.get("grade") == "field"}
            assert tables == {"OWNS", "HAS_ROLE", "Claim"}, name

    def test_sec_by_filing_type_and_date(self):
        _, seen = _run()
        sec = [(s, p) for s, p in seen if s.startswith("UPDATE") and p.get("s") == "s-sec"]
        xml = [s for s, p in sec if "filing_type = '13F'" in s]
        assert len(xml) == 2 and all(p["grade"] == "field" for s, p in sec if "13F" in s)
        assert all("Form 4" in s and "filing_type = '3'" in s for s in xml)
        schedules = [(s, p) for s, p in sec if "LIKE '13D%'" in s and "read_from IS NULL" in s]
        assert {(("source_date >= :d" in s), p["grade"]) for s, p in schedules} == {
            (True, "field"), (False, "form")}
        assert all(p["d"] == "2024-12-18" for _, p in schedules)
        assert all("source_date < :d" in s for s, p in schedules if p["grade"] == "form")
        assert not any(p["grade"] == "prose" for _, p in sec), "nothing SEC is stamped prose here"
        # an Exhibit 21 row is graded while it is parsed — never here
        assert not any("EX-21" in s or "EX-8" in s for s, _ in sec)
        seats = [(s, p) for s, p in sec if s.split()[1] == "HAS_ROLE"]
        assert len(seats) == 1 and seats[0][1]["grade"] == "field"
        assert any("kind = 'role'" in s and p["grade"] == "field" for s, p in sec if "Claim" in s)

    def test_a_source_the_graph_lacks_is_skipped(self):
        res, seen = _run(sources={"SEC EDGAR": "s-sec"})
        assert not any(p.get("s") == "s-gleif" for _, p in seen)
        assert all(p.get("s") == "s-sec" for s, p in seen if s.startswith("UPDATE"))


class TestCountsAndBatches:
    def test_batches_until_a_short_page(self):
        # 2000, 2000, 5 → three statements for the first rule, 4005 stamped
        res, seen = _run(batches=(2000, 2000, 5))
        first = [s for s, _ in seen if s.startswith("UPDATE")][:3]
        assert len({s for s in first}) == 1, "the same statement, repeated"
        assert res["GLEIF:OWNS:field"] == 4005
        assert [s for s, _ in seen if s.startswith("UPDATE")][3] != first[0], "then the next rule"

    def test_dry_run_counts_and_writes_nothing(self):
        res, seen = _run(dry_run=True)
        assert not any(s.startswith("UPDATE") for s, _ in seen)
        counted = [s for s, _ in seen if s.startswith("SELECT count")]
        assert all("read_from IS NULL" in s or "read_from = :old" in s for s in counted)
        assert any("read_from = :old" in s for s in counted), "the re-grade is counted too"
        assert res["dry_run"] is True and res["GLEIF:OWNS:field"] == 3

    def test_the_fill_and_the_regrade_add_up_under_one_key(self):
        # every UPDATE answers 1: per SEC table, form = the date fill + the re-grade
        res, _ = _run(batches=(1,) * 40)
        assert res["SEC EDGAR:OWNS:form"] == 2 and res["SEC EDGAR:Claim:form"] == 2
        assert "SEC EDGAR:OWNS:prose" not in res

    def test_zero_counts_are_left_out(self):
        res, _ = _run()
        assert res == {"dry_run": False}
