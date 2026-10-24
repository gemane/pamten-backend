"""Tests for the schema/index bootstrap (SQL layer mocked)."""

import re
from unittest.mock import patch

from app.db import schema


def _run(side_effect=None):
    """Patch run_sql, returning the mock that recorded the issued statements."""
    return patch.object(schema, "run_sql", side_effect=side_effect)


def _held_rules():
    """Every rule of `constraints()` as the catalog reports it: the flags as
    booleans, min/max/default as strings (probed on 26.10.1)."""
    held: dict = {}
    for vtype, prop, attr, value in schema.constraints():
        key = schema._RULE_KEYS[attr]
        held.setdefault(f"{vtype}.{prop}", {})[key] = (value == "true") if key in ("mandatory", "notNull") else value
    return held


def test_creates_every_vertex_type_once():
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    for vtype in ("Entity", "Person", "Source", "User"):
        assert f"CREATE VERTEX TYPE {vtype} IF NOT EXISTS" in issued


def test_creates_property_and_index_for_each_entry():
    with _run() as m:
        result = schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    # spot-check the hot-path lookup indexes
    assert any("CREATE INDEX IF NOT EXISTS ON Entity (name_normalized) NOTUNIQUE" == s for s in issued)
    assert any("CREATE PROPERTY Entity.wikidata_id IF NOT EXISTS STRING" == s for s in issued)
    assert result["skipped"] is False
    assert result["failed"] == []


def test_id_and_email_indexes_are_unique():
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    assert "CREATE INDEX IF NOT EXISTS ON User (email) UNIQUE" in issued
    assert "CREATE INDEX IF NOT EXISTS ON Entity (id) UNIQUE" in issued


def test_is_idempotent_all_ddl_uses_if_not_exists():
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    # Every DDL statement is idempotent — VERTEX TYPE, EDGE TYPE, PROPERTY and
    # INDEX all use IF NOT EXISTS, so a re-run (startup, bulk-load rebuild) logs
    # no "already exists" failures. For PROPERTY it must sit BEFORE the type.
    for s in issued:
        if not s.startswith("CREATE"):
            continue                      # the two catalog reads are not DDL
        assert "IF NOT EXISTS" in s, s
        if s.startswith("CREATE PROPERTY"):
            # The type follows IF NOT EXISTS: STRING for an indexed lookup
            # column unless _PROPERTY_TYPES says otherwise (Person.alias is a
            # LIST — declared STRING, ArcadeDB rejects `$name IN p.alias`),
            # and the declared type for every number, flag and list a writer
            # sets (_TYPED_PROPERTIES).
            assert re.search(r"IF NOT EXISTS (STRING|LIST|INTEGER|LONG|DOUBLE|BOOLEAN)$", s), s


def test_declared_property_types_are_rendered():
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    assert "CREATE PROPERTY Person.alias IF NOT EXISTS LIST" in issued
    assert "CREATE PROPERTY Person.full_name IF NOT EXISTS STRING" in issued


def test_continues_and_records_failures():
    # fail only the User.email index; everything else should still run
    def side(stmt, *a, **k):
        if "ON User (email) UNIQUE" in stmt and stmt.startswith("CREATE INDEX"):
            raise RuntimeError("duplicate keys")
    with _run(side_effect=side):
        result = schema.ensure_indexes()
    assert result["skipped"] is False
    assert len(result["failed"]) == 1
    assert "ON User (email) UNIQUE" in result["failed"][0]["stmt"]
    assert result["ok"]  # the rest applied


def test_unreachable_db_is_skipped_without_raising():
    with _run(side_effect=ConnectionError("refused")):
        result = schema.ensure_indexes()
    assert result["skipped"] is True
    # bailed on the very first statement, no exception propagated
    assert result["failed"] == []


def test_the_psc_edge_index_is_declared():
    """The Companies House refresh matches an edge by `psc_self_link`, in batches,
    then updates by the same key. Both work unindexed — by scanning every OWNS edge
    per batch — so nothing *fails* without the index, it just takes hours instead of
    minutes. Asserted on the emitted DDL, because a test that loops over the
    declaration list passes happily when the list is empty."""
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    assert "CREATE PROPERTY OWNS.psc_self_link IF NOT EXISTS STRING" in issued
    assert "CREATE INDEX IF NOT EXISTS ON OWNS (psc_self_link) NOTUNIQUE" in issued


def test_an_edge_index_does_not_create_a_vertex_type():
    """`_INDEXES`'s first element drives `CREATE VERTEX TYPE`, which is exactly why
    edge indexes live in their own list. Declaring OWNS in the wrong one would make
    the bootstrap try to create a vertex type shadowing the edge type."""
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    assert "CREATE VERTEX TYPE OWNS IF NOT EXISTS" not in issued
    assert "CREATE EDGE TYPE OWNS IF NOT EXISTS" in issued


class TestRebuildRecreatesAMissingFulltextIndex:
    """A bulk load drops the FULL_TEXT index and relies on the schema bootstrap
    to re-create it; when that bootstrap timed out during the flush (the full
    PSC load did), REBUILD found nothing and /search stayed dark."""

    def test_not_found_leads_to_create_and_no_second_rebuild(self):
        issued = []

        def run(sql, *a, **kw):
            issued.append(sql)
            if sql.startswith("REBUILD") and "Entity[search_text]" in sql:
                raise RuntimeError("Index with name 'Entity[search_text]' was not found")
            return []
        with patch.object(schema, "run_sql", side_effect=run):
            res = schema.rebuild_fulltext_indexes(timeout=1)
        creates = [s for s in issued if s.startswith("CREATE INDEX") and "Entity (search_text)" in s]
        assert creates, "the missing index is created"
        assert issued.count("REBUILD INDEX `Entity[search_text]`") == 1, \
            "the CREATE indexed the rows; a REBUILD after it would do the same work again"
        assert not any(f["index"] == "Entity[search_text]" for f in res["failed"]), res

    def test_the_hard_path_drops_creates_and_does_not_rebuild_again(self):
        # 14M entities: the CREATE took 25 min on the sizing box and the
        # belt-and-braces REBUILD another 25 for the same result.
        issued = []
        catalog = [{"name": "Entity[search_text]", "properties": [["search_text"]]}]

        def run(sql, *a, **kw):
            issued.append(sql)
            return catalog if sql.startswith("SELECT name, properties FROM schema:indexes") else []
        with patch.object(schema, "run_sql", side_effect=run):
            res = schema.rebuild_fulltext_indexes(timeout=1, hard=True)
        assert any(s.startswith("DROP INDEX") for s in issued)
        assert any(s.startswith("CREATE INDEX") and "FULL_TEXT" in s for s in issued)
        assert not any(s.startswith("REBUILD") for s in issued), issued
        assert not res["failed"]

    def test_other_errors_still_fail_the_index(self):
        def run(sql, *a, **kw):
            if sql.startswith("REBUILD") and "Entity[search_text]" in sql:
                raise RuntimeError("timed out")
            return []
        with patch.object(schema, "run_sql", side_effect=run):
            res = schema.rebuild_fulltext_indexes(timeout=1)
        assert any(f["index"] == "Entity[search_text]" for f in res["failed"])


class TestStartupIssuesNoDdlWhenTheSchemaIsComplete:
    """Even `IF NOT EXISTS` takes the schema lock; on the sizing box thirty of
    them queued behind an hour-long index compaction at API startup and every
    request waited behind them. The catalog is read first, DDL only for gaps."""

    @staticmethod
    def _catalog(types, indexes, declared=(), rules=None):
        """The catalog as the server reports it: types with their declared
        properties (`declared` = "Type.prop" names), the rules each holds
        (`rules` = {"Type.prop": {catalog key: value}}; None = every rule of
        `constraints()` as the server would report it), and the indexes."""
        rules = _held_rules() if rules is None else rules

        def run(sql, *a, **kw):
            if sql == "SELECT name, properties FROM schema:types":
                return [{"name": t, "properties": [{"name": d.split(".", 1)[1], **rules.get(d, {})}
                                                   for d in declared if d.split(".", 1)[0] == t]}
                        for t in types]
            if sql == "SELECT name FROM schema:indexes":
                return [{"name": i} for i in indexes]
            return []
        return run

    _TYPED = ({f"{t}.{p}" for t, p, _ in schema._TYPED_PROPERTIES}
              | {f"{t}.{p}" for t, p in schema._RULED_STRINGS}
              | {f"{t}.{p}" for t, p, _ in schema._INDEXES} | {f"{t}.{p}" for t, p, _ in schema._EDGE_INDEXES})

    def test_a_complete_schema_issues_nothing(self):
        types = {t for t, _, _ in schema._INDEXES} | set(schema._EDGE_TYPES)
        indexes = ({f"{t}[{p}]" for t, p, _ in schema._INDEXES}
                   | {f"{t}[{p}]" for t, p, _ in schema._EDGE_INDEXES}
                   | {f"{t}[{p}]" for t, p in schema._FULLTEXT_INDEXES})
        with _run(self._catalog(types, indexes, self._TYPED)) as m:
            res = schema.ensure_indexes()
        issued = [c.args[0] for c in m.call_args_list]
        assert not any(s.startswith("CREATE") for s in issued), issued
        assert res.get("complete") is True and res["failed"] == []

    def test_only_the_missing_index_is_created(self):
        types = {t for t, _, _ in schema._INDEXES} | set(schema._EDGE_TYPES)
        indexes = ({f"{t}[{p}]" for t, p, _ in schema._INDEXES}
                   | {f"{t}[{p}]" for t, p, _ in schema._EDGE_INDEXES}
                   | {f"{t}[{p}]" for t, p in schema._FULLTEXT_INDEXES}) - {"Entity[search_text]"}
        with _run(self._catalog(types, indexes, self._TYPED)) as m:
            schema.ensure_indexes()
        creates = [c.args[0] for c in m.call_args_list if c.args[0].startswith("CREATE")]
        assert creates == ["CREATE PROPERTY Entity.search_text IF NOT EXISTS STRING",
                           "CREATE INDEX IF NOT EXISTS ON Entity (search_text) FULL_TEXT"]

    def test_an_unreadable_catalog_falls_back_to_the_full_ddl(self):
        def run(sql, *a, **kw):
            if sql.startswith("SELECT name"):
                raise RuntimeError("no such type schema:types")
            return []
        with _run(run) as m:
            res = schema.ensure_indexes()
        issued = [c.args[0] for c in m.call_args_list]
        assert "CREATE VERTEX TYPE Entity IF NOT EXISTS" in issued
        assert res["skipped"] is False

    def test_an_unreachable_server_still_skips(self):
        def run(sql, *a, **kw):
            raise ConnectionError("ArcadeDB unreachable: timed out")
        with _run(run):
            res = schema.ensure_indexes()
        assert res["skipped"] is True


class TestTypedProperties:
    """Every number, flag and list a writer sets has a declared type, on vertex
    and edge types alike; a complete catalog issues no DDL for them."""

    def test_the_numbers_flags_and_lists_are_declared(self):
        with _run() as m:
            schema.ensure_indexes()
        issued = [c.args[0] for c in m.call_args_list]
        for stmt in ("CREATE PROPERTY OWNS.stake_percent IF NOT EXISTS DOUBLE",
                     "CREATE PROPERTY OWNS.shares IF NOT EXISTS DOUBLE",
                     "CREATE PROPERTY Claim.stake_percent IF NOT EXISTS DOUBLE",
                     "CREATE PROPERTY Entity.founded IF NOT EXISTS INTEGER",
                     "CREATE PROPERTY Entity.is_nominee IF NOT EXISTS BOOLEAN",
                     "CREATE PROPERTY Entity.aliases IF NOT EXISTS LIST",
                     "CREATE PROPERTY Entity.cusips IF NOT EXISTS LIST",
                     "CREATE PROPERTY Person.nationalities IF NOT EXISTS LIST",
                     "CREATE PROPERTY Source.enabled IF NOT EXISTS BOOLEAN"):
            assert stmt in issued, stmt
        # an indexed LIST is declared once, by the index loop, never twice
        assert issued.count("CREATE PROPERTY Person.alias IF NOT EXISTS LIST") == 1

    def test_a_declared_property_issues_no_ddl(self):
        types, indexes, declared, rules = self._complete()
        assert schema._statements((types, indexes, declared, rules)) == []
        # one typed property missing from the catalog → exactly its declaration
        del declared["OWNS.shares"]
        assert schema._statements((types, indexes, declared, rules)) == ["CREATE PROPERTY OWNS.shares IF NOT EXISTS DOUBLE"]

    def test_an_older_two_part_catalog_still_works_and_declares_the_types(self):
        types = {t for t, _, _ in schema._INDEXES} | set(schema._EDGE_TYPES)
        indexes = {f"{t}[{p}]" for t, p, _ in schema._INDEXES} | {f"{t}[{p}]" for t, p, _ in schema._EDGE_INDEXES} \
            | {f"{t}[{p}]" for t, p in schema._FULLTEXT_INDEXES}
        stmts = schema._statements((types, indexes))
        creates = [s for s in stmts if s.startswith("CREATE PROPERTY")]
        assert len(creates) == len(schema._TYPED_PROPERTIES) + len(schema._RULED_STRINGS)
        # the rules cannot be compared without the catalog: every one is issued
        assert len([s for s in stmts if s.startswith("ALTER PROPERTY")]) == len(schema.constraints())

    def test_the_catalog_read_reports_declared_properties(self):
        rows = {"SELECT name, properties FROM schema:types": [
                    {"name": "OWNS", "properties": [{"name": "stake_percent", "type": "DOUBLE"}]},
                    {"name": "Entity", "properties": []}],
                "SELECT name FROM schema:indexes": [{"name": "Entity[id]"}]}
        with _run(side_effect=lambda sql, *a, **k: rows[sql]):
            types, indexes, declared, rules = schema._existing_schema()
        assert types == {"OWNS", "Entity"} and indexes == {"Entity[id]"}
        assert declared == {"OWNS.stake_percent": "DOUBLE"} and "OWNS.stake_percent" in declared
        assert rules == {"OWNS.stake_percent": {}}

    @staticmethod
    def _complete():
        types = {t for t, _, _ in schema._INDEXES} | set(schema._EDGE_TYPES) | {"Claim", "StakeHistory", "IndustryCode"}
        indexes = {f"{t}[{p}]" for t, p, _ in schema._INDEXES} | {f"{t}[{p}]" for t, p, _ in schema._EDGE_INDEXES} \
            | {f"{t}[{p}]" for t, p in schema._FULLTEXT_INDEXES}
        declared = {f"{t}.{p}": k for t, p, k in schema._TYPED_PROPERTIES}
        declared.update({f"{t}.{p}": k for (t, p), k in schema._PROPERTY_TYPES.items()})
        declared.update({f"{t}.{p}": "STRING" for t, p in schema._RULED_STRINGS})
        # the indexed strings the rules sit on are declared by the index loop
        declared.update({f"{t}.{p}": "STRING" for t, p, _ in schema._INDEXES if f"{t}.{p}" not in declared})
        return types, indexes, declared, _held_rules()

    def test_a_property_declared_with_another_type_is_dropped_and_declared_again(self):
        # shares_outstanding was declared LONG by the deploy of the day before it
        # became DOUBLE; ArcadeDB has no ALTER PROPERTY … TYPE, and a declaration
        # that stays LONG coerces every healed row straight back
        types, indexes, declared, rules = self._complete()
        assert schema._statements((types, indexes, declared, rules)) == []
        declared["OWNS.shares_outstanding"] = "LONG"
        declared["Claim.voting_shares"] = "LONG"
        assert schema._statements((types, indexes, declared, rules)) == [
            "DROP PROPERTY OWNS.shares_outstanding",
            "CREATE PROPERTY OWNS.shares_outstanding IF NOT EXISTS DOUBLE",
            "DROP PROPERTY Claim.voting_shares",
            "CREATE PROPERTY Claim.voting_shares IF NOT EXISTS DOUBLE",
        ]

    def test_a_catalog_without_types_is_trusted_as_it_was(self):
        # an older caller passes bare names: nothing to compare, nothing dropped
        types, indexes, declared, rules = self._complete()
        stmts = schema._statements((types, indexes, set(declared), rules))
        assert stmts == [] and not any(s.startswith("DROP") for s in stmts)

    def test_an_indexed_property_declared_with_another_type_is_only_reported(self, caplog):
        types, indexes, declared, rules = self._complete()
        declared["Person.alias"] = "STRING"             # indexed: a redeclare means a reindex
        with caplog.at_level("WARNING", logger="app.db.schema"):
            stmts = schema._statements((types, indexes, declared, rules))
        assert stmts == []
        assert any("Person.alias is declared STRING" in r.message for r in caplog.records)

    def test_ensure_indexes_runs_the_redeclaration(self):
        types, indexes, declared, rules = self._complete()
        declared["OWNS.shares_outstanding"] = "LONG"
        rows = {"SELECT name, properties FROM schema:types": [
                    {"name": t, "properties": [{"name": k.split(".", 1)[1], "type": v, **rules.get(k, {})}
                                               for k, v in declared.items() if k.split(".", 1)[0] == t]}
                    for t in types],
                "SELECT name FROM schema:indexes": [{"name": i} for i in indexes]}
        with _run(side_effect=lambda sql, *a, **k: rows.get(sql, [])) as m:
            res = schema.ensure_indexes()
        issued = [c.args[0] for c in m.call_args_list]
        assert res["ok"] == ["DROP PROPERTY OWNS.shares_outstanding",
                             "CREATE PROPERTY OWNS.shares_outstanding IF NOT EXISTS DOUBLE"]
        assert issued.index("DROP PROPERTY OWNS.shares_outstanding") \
            < issued.index("CREATE PROPERTY OWNS.shares_outstanding IF NOT EXISTS DOUBLE")

    def test_dates_are_not_typed(self):
        for _, prop, ptype in schema._TYPED_PROPERTIES:
            assert ptype != "DATE" and ptype != "DATETIME"
            assert not prop.endswith(("_date", "_at", "since", "until")), prop

    def test_share_counts_are_doubles_not_longs(self):
        # a Form 4 reports fractional shares; a LONG declaration is refused over one
        for _, prop, ptype in schema._TYPED_PROPERTIES:
            if prop in ("shares", "shares_outstanding", "voting_shares"):
                assert ptype == "DOUBLE", prop

    def test_typed_properties_lists_every_declared_non_string(self):
        rows = schema.typed_properties()
        assert ("Person", "alias", "LIST") in rows and ("OWNS", "shares", "DOUBLE") in rows
        assert len({(t, p) for t, p, _ in rows}) == len(rows)


class TestConstraints:
    """The rules the server enforces beyond type (`constraints()`): built from
    the enums the code validates against, issued only where the catalog lacks
    them, refused by the server while a row breaks them."""

    def test_the_vocabularies_come_from_the_enums(self):
        from app.models.entity import EntityType
        from app.models.relationship import OwnershipType
        from app.scraper.edge_schema import READ_GRADES
        rules = {(t, p, a): v for t, p, a, v in schema.constraints()}
        assert rules[("Entity", "type", "REGEXP")] == "^(" + "|".join(e.value for e in EntityType) + ")$"
        assert rules[("OWNS", "ownership_type", "REGEXP")] == "^(" + "|".join(e.value for e in OwnershipType) + ")$"
        assert rules[("OWNS", "read_from", "REGEXP")] == "^(" + "|".join(READ_GRADES) + ")$"
        assert rules[("Claim", "kind", "REGEXP")] == "^(owns|role|succession)$"
        assert rules[("Entity", "country", "REGEXP")] == "^[A-Z]{2}$"
        assert rules[("OWNS", "stake_percent", "MIN")] == "0" and rules[("OWNS", "stake_percent", "MAX")] == "100"
        assert rules[("OWNS", "stale", "DEFAULT")] == "false"
        for key in (("Entity", "id"), ("Person", "id"), ("Claim", "claim_key"), ("Claim", "to_id")):
            assert rules[(*key, "MANDATORY")] == "true" and rules[(*key, "NOTNULL")] == "true"
        # a claim may lack its source (the SEC writer records one for an edge
        # whose source is unknown); the key then has no source part
        assert ("Claim", "source_id", "MANDATORY") not in rules

    def test_names_and_dates_carry_no_rule(self):
        # a placeholder `lei:` node is created without a name and named by a
        # later import; a date keeps its source's precision
        for vtype, prop, _, _ in schema.constraints():
            assert prop not in ("name", "full_name", "name_normalized"), (vtype, prop)
            assert not prop.endswith(("_date", "_at", "since", "until")), (vtype, prop)

    def test_every_ruled_property_is_declared_by_something(self):
        declared = ({(t, p) for t, p, _ in schema._TYPED_PROPERTIES} | set(schema._PROPERTY_TYPES)
                    | {(t, p) for t, p, _ in schema._INDEXES} | {(t, p) for t, p, _ in schema._EDGE_INDEXES}
                    | set(schema._RULED_STRINGS))
        for vtype, prop, _, _ in schema.constraints():
            assert (vtype, prop) in declared, (vtype, prop)

    def test_a_held_rule_issues_nothing_and_a_missing_one_its_alter(self):
        types, indexes, declared, rules = TestTypedProperties._complete()
        assert schema._statements((types, indexes, declared, rules)) == []
        del rules["Entity.country"]["regexp"]
        rules["OWNS.stake_percent"]["max"] = "1"             # held with another value
        rules["Claim.claim_key"]["mandatory"] = False
        assert schema._statements((types, indexes, declared, rules)) == [
            "ALTER PROPERTY Claim.claim_key MANDATORY true",
            "ALTER PROPERTY Entity.country REGEXP '^[A-Z]{2}$'",
            "ALTER PROPERTY OWNS.stake_percent MAX 100",
        ]

    def test_the_catalog_s_strings_and_booleans_compare_as_the_code_s_text(self):
        assert schema._same_rule("0", "0") and schema._same_rule(True, "true") and schema._same_rule("False", "false")
        assert not schema._same_rule(None, "true") and not schema._same_rule("1", "100")

    def test_an_undeclared_ruled_string_is_declared_before_its_rule(self):
        types, indexes, declared, rules = TestTypedProperties._complete()
        del declared["OWNS.read_from"]
        del rules["OWNS.read_from"]
        stmts = schema._statements((types, indexes, declared, rules))
        assert stmts == ["CREATE PROPERTY OWNS.read_from IF NOT EXISTS STRING",
                         "ALTER PROPERTY OWNS.read_from REGEXP '^(field|table|form|layout|prose|narrative)$'"]

    def test_the_catalog_read_reports_the_rules(self):
        rows = {"SELECT name, properties FROM schema:types": [
                    {"name": "T", "properties": [
                        {"name": "id", "type": "STRING", "mandatory": True, "notNull": True, "custom": {}},
                        {"name": "stake", "type": "DOUBLE", "min": "0", "max": "100"},
                        {"name": "free", "type": "STRING"}]}],
                "SELECT name FROM schema:indexes": []}
        with _run(side_effect=lambda sql, *a, **k: rows[sql]):
            *_, rules = schema._existing_schema()
        assert rules == {"T.id": {"mandatory": True, "notNull": True},
                         "T.stake": {"min": "0", "max": "100"}, "T.free": {}}
