"""Every Cypher anchor by id names its label.

An unlabelled ``MATCH (n {id: $id})`` cannot use the per-type id index, so
ArcadeDB scans every vertex type. Invisible on the dev graph; on the full
import (14.2M entities + 13.8M persons) one such anchor in the profile's
voting-groups query ran for more than 400 seconds and turned every company
profile into an HTTP 500 at the 60 s timeout, while the labelled form took
10–70 ms. The rule was first learned in PR #91 and then broken again in six
places — hence a test that reads the source, not a memory.
"""
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.db.anchors import NODE_LABELS, label_or_entity, node_label

APP = Path(__file__).resolve().parents[1] / "app"

# `MATCH (n {id: …` — an anchor pattern whose node has no `:Label`. The colon after
# `id` keeps prose like `MATCH (a {id}), (b {id})` in docstrings out of it.
_UNLABELLED = re.compile(r"(?:MATCH|MERGE)\s*\(\s*\w*\s*\{\s*id\s*:")


def _cypher_sources():
    for path in sorted(APP.rglob("*.py")):
        yield path.relative_to(APP.parent), path.read_text(encoding="utf-8")


class TestNoUnlabelledAnchorInTheSource:
    def test_every_anchor_by_id_names_its_label(self):
        offenders = []
        for rel, text in _cypher_sources():
            for m in _UNLABELLED.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{rel}:{line}: {text[m.start():m.start() + 40]!r}")
        assert not offenders, (
            "Unlabelled Cypher anchor(s) — these scan every vertex type on a big "
            "database (see app/db/anchors.py):\n  " + "\n  ".join(offenders))

    def test_the_pattern_catches_the_shapes_that_shipped(self):
        """The regex must flag the exact forms that were in the code."""
        for bad in ("MATCH (m {id: $id})-[r:RELATED_TO]->(g:Entity)",
                    "MATCH (owner {id: $owner_id})",
                    "MATCH (a {id:$oid}), (b {id:$tid}) MERGE (a)-[r:OWNS]->(b)",
                    "MATCH (n {id: $oid})\n   SET n.full_name = $proxy_name"):
            assert _UNLABELLED.search(bad), bad

    def test_and_lets_labelled_and_prose_forms_through(self):
        for good in ("MATCH (m:Entity {id: $id})-[r:RELATED_TO]->(g:Entity)",
                     "MATCH (owner:Person {id: $owner_id})",
                     "MATCH (a:{label} {{id: $a}})-[r:OWNS]->(b:Entity {{id: $b}})",
                     "`MATCH (a {id}), (b {id})` full-scans every node"):
            assert not _UNLABELLED.search(good), good


class TestNodeLabel:
    """The lookup a caller uses when it does not know the label: two indexed
    point reads, Entity first, Person second."""

    @staticmethod
    def _session(hits: dict):
        session = MagicMock()

        def run(cypher, **params):
            label = re.search(r"\(n:(\w+)", cypher).group(1)
            res = MagicMock()
            res.single.return_value = {"id": params["id"]} if hits.get(label) else None
            return res
        session.run.side_effect = run
        return session

    def test_an_entity_id_resolves_to_entity_in_one_read(self):
        s = self._session({"Entity": True, "Person": True})
        assert node_label("lei:1", s) == "Entity"
        assert s.run.call_count == 1

    def test_a_person_id_resolves_after_the_entity_miss(self):
        s = self._session({"Person": True})
        assert node_label("p1", s) == "Person"
        assert s.run.call_count == 2
        assert all("{id: $id}" in c.args[0] and ":" in c.args[0].split("(n")[1][:8]
                   for c in s.run.call_args_list)

    def test_an_unknown_id_is_none_after_both(self):
        s = self._session({})
        assert node_label("nope", s) is None
        assert s.run.call_count == len(NODE_LABELS)

    def test_an_empty_id_asks_nothing(self):
        s = self._session({"Entity": True})
        assert node_label("", s) is None
        s.run.assert_not_called()

    def test_the_query_callable_form(self):
        calls = []

        def query(cypher, params):
            calls.append(cypher)
            return [{"id": params["id"]}] if "(n:Person" in cypher else []
        assert node_label("p1", query=query) == "Person"
        assert len(calls) == 2

    def test_it_needs_one_of_the_two(self):
        with pytest.raises(TypeError):
            node_label("x")


class TestLabelOrEntity:
    def test_known_labels_pass_through(self):
        assert label_or_entity("Person") == "Person"
        assert label_or_entity("Entity") == "Entity"

    def test_anything_else_is_a_company(self):
        for other in (None, "", "Company", "person", "Source"):
            assert label_or_entity(other) == "Entity"


class TestTheSitesThatScanned:
    """Each query that used to anchor unlabelled now carries the label it was
    given (or read back). Text-level checks on the fake session — the
    behaviour itself is covered by the integration suites."""

    def test_voting_groups_anchor_on_the_profiles_own_label(self, fake_db):
        from app.routers.search import _voting_groups_of
        _voting_groups_of(fake_db, "e1", set(), "Entity")
        _voting_groups_of(fake_db, "p1", set(), "Person")
        assert "MATCH (m:Entity {id: $id})" in fake_db.calls[0][0]
        assert "MATCH (m:Person {id: $id})" in fake_db.calls[1][0]

    def test_the_entity_profile_passes_entity_and_the_person_profile_person(self):
        src = (APP / "routers" / "search.py").read_text(encoding="utf-8")
        assert '_voting_groups_of(session, entity_id, hidden, "Entity")' in src
        assert '_voting_groups_of(session, person_id, hidden, "Person")' in src

    def test_mark_stale_ownership_writes_with_the_owners_label(self, fake_db, monkeypatch):
        from app.scraper import maintenance
        monkeypatch.setattr(maintenance, "run_sql", lambda *a, **k: [])
        fake_db.queue([
            {"aid": "p1", "alabel": "Person", "bid": "e1",
             "seen": "2020-01-01T00:00:00+00:00", "stale": None},
            {"aid": "e2", "alabel": "Entity", "bid": "e1",
             "seen": "2020-01-01T00:00:00+00:00", "stale": None},
        ])
        res = maintenance.mark_stale_ownership(days=30)
        assert res["marked"] == 2
        scan, first, second = (c[0] for c in fake_db.calls)
        assert "labels(a)[0] AS alabel" in scan and "(b:Entity)" in scan
        assert "MATCH (a:Person {id: $a})-[r:OWNS]->(b:Entity {id: $b})" in first
        assert "MATCH (a:Entity {id: $a})-[r:OWNS]->(b:Entity {id: $b})" in second

    def test_mark_13f_stale_writes_with_the_filers_label(self, fake_db):
        from app.scraper.sec_writer import mark_13f_stale
        fake_db.queue([{"aid": "fund1", "alabel": "Entity"},
                       {"aid": "p1", "alabel": "Person"}])
        assert mark_13f_stale("e1", "2026-06-30") == 2
        scan, first, second = (c[0] for c in fake_db.calls)
        assert "labels(a)[0] AS alabel" in scan
        assert "MATCH (a:Entity {id: $a})-[r:OWNS]->(b:Entity {id: $b})" in first
        assert "MATCH (a:Person {id: $a})-[r:OWNS]->(b:Entity {id: $b})" in second

    def test_proxy_write_names_the_company_and_the_matched_owner(self, monkeypatch):
        from app.scraper import proxy_write
        queries, commands = [], []

        def run_query(cypher, params=None):
            queries.append(cypher)
            if "(c:Entity {id: $id})" in cypher:
                return [{"id": "e1", "name": "Acme Corp"}]
            return [{"oid": "p1", "olabel": "Person", "matched_name": "Brin Sergey",
                     "stake": 5.0, "file_date": None, "source_id": "s",
                     "ownership_type": "minority", "since": None}]
        monkeypatch.setattr(proxy_write, "run_query", run_query)
        monkeypatch.setattr(proxy_write, "run_command",
                            lambda cypher, params=None: commands.append(cypher))
        # The proxy parser (bs4) is imported lazily inside the writer; stub the
        # module so this stays a query-text test, not a parser one.
        import sys
        import types
        stub = types.ModuleType("app.scraper.proxy_statement")
        stub.fetch_proxy_ownership = lambda company: {
            "owners": [{"name": "Sergey Brin", "voting_power_pct": 5.0}]}
        monkeypatch.setitem(sys.modules, "app.scraper.proxy_statement", stub)
        res = proxy_write.write_proxy_ownership("Acme Corp", entity_id="e1")
        assert res.get("db_error") is None
        assert "MATCH (c:Entity {id: $id})" in queries[0]
        assert "(c:Entity {id: $cid})" in queries[1] and "labels(n)[0] AS olabel" in queries[1]
        # the reorder rename and the edge update both anchor on the owner's label
        assert any("MATCH (n:Person {id: $oid})\n" in c for c in commands)
        assert any("MATCH (n:Person {id: $oid})-[r:OWNS]->(c:Entity {id: $cid})" in c
                   for c in commands)

    def test_federation_import_labels_both_ends(self, fake_db, monkeypatch):
        from app.routers import federation
        monkeypatch.setattr(federation, "_ensure_peer_source", lambda *a, **k: "src")
        monkeypatch.setattr(federation, "_upsert_entity", lambda s, ref, *a, **k: ref["id"])
        monkeypatch.setattr(federation, "_upsert_person", lambda s, ref, *a, **k: ref["id"])
        monkeypatch.setattr(federation, "record_claim", lambda **k: None)
        monkeypatch.setattr(federation, "deduplicate_high_confidence", lambda *a, **k: None,
                            raising=False)
        data = {"format": federation.EXPORT_FORMAT, "entities": [], "persons": [],
                "ownerships": [
                    {"owner": {"id": "p1", "kind": "person", "full_name": "A B"},
                     "owned": {"id": "e1", "name": "Acme"}, "stake_percent": 10},
                    {"owner": {"id": "e2", "name": "Holdco"},
                     "owned": {"id": "e1", "name": "Acme"}, "stake_percent": 90},
                ]}
        federation.import_snapshot(data, "peer", 60)
        merges = [c[0] for c in fake_db.calls if "MERGE (a)-[r:OWNS]->(b)" in c[0]]
        assert len(merges) == 2
        assert merges[0].startswith("MATCH (a:Person {id:$oid}), (b:Entity {id:$tid})")
        assert merges[1].startswith("MATCH (a:Entity {id:$oid}), (b:Entity {id:$tid})")
