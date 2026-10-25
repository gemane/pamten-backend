"""Tests for the schema/index bootstrap (SQL layer mocked)."""

import re
from contextlib import contextmanager
from unittest.mock import patch

from app.db import schema


@contextmanager
def _run(side_effect=None):
    """Patch run_sql AND run_sqlscript; yield the run_sql mock, whose call list
    is every statement issued — a script counts as its statements, once. The
    script double behaves like the server: if any statement in it would be
    refused (`side_effect` raises for it), the whole script is refused and
    nothing in it is recorded; the bootstrap then re-issues the chunk
    statement by statement through run_sql, which records each once."""
    with patch.object(schema, "run_sql", side_effect=side_effect) as m:
        def script(text, params=None):
            stmts = schema._statements_of(text)
            if side_effect is not None:
                for stmt in stmts:
                    side_effect(stmt)
            for stmt in stmts:
                m(stmt)
        with patch.object(schema, "run_sqlscript", side_effect=script):
            yield m


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
              | {f"{t}.{p}" for t, p in schema.string_properties()}
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
        assert len(creates) == (len(schema._TYPED_PROPERTIES) + len(schema._RULED_STRINGS)
                                + len(schema.string_properties()))
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
        declared.update({f"{t}.{p}": "STRING" for t, p in schema.string_properties()})
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
            "DROP PROPERTY OWNS.shares_outstanding IF EXISTS",
            "CREATE PROPERTY OWNS.shares_outstanding IF NOT EXISTS DOUBLE",
            "DROP PROPERTY Claim.voting_shares IF EXISTS",
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
        assert res["ok"] == ["DROP PROPERTY OWNS.shares_outstanding IF EXISTS",
                             "CREATE PROPERTY OWNS.shares_outstanding IF NOT EXISTS DOUBLE"]
        assert issued.index("DROP PROPERTY OWNS.shares_outstanding IF EXISTS") \
            < issued.index("CREATE PROPERTY OWNS.shares_outstanding IF NOT EXISTS DOUBLE")

    def test_dates_are_not_typed(self):
        for vtype, prop, ptype in schema._TYPED_PROPERTIES:
            assert ptype != "DATE" and ptype != "DATETIME"
            if vtype == "RefreshToken":
                continue                    # epoch seconds, a DOUBLE on purpose (app.auth.refresh)
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
        # a claim is what one source says: it cannot exist without naming it
        assert rules[("Claim", "source_id", "MANDATORY")] == "true"
        assert rules[("Claim", "source_id", "NOTNULL")] == "true"

    def test_names_carry_no_rule(self):
        # a placeholder `lei:` node is created without a name and named by a
        # later import
        for vtype, prop, _, _ in schema.constraints():
            assert prop not in ("name", "full_name", "name_normalized"), (vtype, prop)

    def test_every_date_and_timestamp_carries_its_shape(self):
        rules = {(t, p, a): v for t, p, a, v in schema.constraints()}
        for vtype, prop in schema._DATES:
            assert rules[(vtype, prop, "REGEXP")] == schema._DATE_PATTERN, (vtype, prop)
        for vtype, prop in schema._TIMESTAMPS:
            assert rules[(vtype, prop, "REGEXP")] == schema._TIMESTAMP_PATTERN, (vtype, prop)
        # the fields the writers set, by name — none forgotten
        for key in (("OWNS", "since"), ("OWNS", "until"), ("OWNS", "source_date"), ("OWNS", "stale_since"),
                    ("Claim", "since"), ("Claim", "source_date"), ("HAS_ROLE", "since"),
                    ("Entity", "incorporated_on"), ("Person", "birth_date"), ("StakeHistory", "as_of")):
            assert key in schema._DATES, key
        for key in (("Entity", "created_at"), ("Entity", "last_scraped_at"), ("OWNS", "last_scraped_at"),
                    ("Claim", "first_seen_at"), ("Claim", "last_seen_at"), ("StakeHistory", "recorded_at")):
            assert key in schema._TIMESTAMPS, key
        assert not set(schema._DATES) & set(schema._TIMESTAMPS)

    def test_the_date_shape_keeps_a_sources_precision_and_refuses_another_notation(self):
        import re
        date = re.compile(schema._DATE_PATTERN)
        for ok in ("2019-03-04", "2019-00-00", "2019-03", "2019", "2026-08-07"):
            assert date.match(ok), ok
        for bad in ("2019-3-4", "4 March 2019", "03/04/2019", "2019-03-04T00:00:00+00:00",
                    "", " 2019-03-04", "2019-03-04 ", "20190304", "2019-03-04-05"):
            assert not date.match(bad), bad

    def test_the_timestamp_shape_needs_a_time_and_an_offset(self):
        import re
        from datetime import datetime, timezone
        ts = re.compile(schema._TIMESTAMP_PATTERN)
        assert ts.match(datetime.now(timezone.utc).isoformat())        # what every writer sends
        for ok in ("2026-10-25T10:00:00+00:00", "2026-10-25T10:00:00.123456+00:00",
                   "2026-10-25T10:00:00.123456Z", "2026-10-25T10:00:00Z", "2026-10-25T12:00:00+02:00",
                   "2020-01-01T00:00Z"):                 # ArcadeDB's own spelling of a parsed instant
            assert ts.match(ok), ok
        for bad in ("2026-10-25", "2026-10-25T10:00:00", "2026-10-25T10:00", "2026-10-25 10:00:00+00:00",
                    "yesterday", ""):
            assert not ts.match(bad), bad

    def test_a_pattern_travels_inside_an_sql_string_literal(self):
        # `ALTER PROPERTY … REGEXP '<pattern>'`: no quote to end the literal
        # early, no backslash for the SQL parser to interpret
        for vtype, prop, attr, value in schema.constraints():
            if attr == "REGEXP":
                assert "'" not in value and "\\" not in value, (vtype, prop, value)
                assert schema._rule_sql(vtype, prop, attr, value) == f"ALTER PROPERTY {vtype}.{prop} REGEXP '{value}'"

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


class TestTheLedger:
    """Every property a writer sets is declared — the strings too (2026-10-25).
    ArcadeDB accepts an undeclared property, so the ledger is what catches a
    typo: the writers' own property lists are held against it here, and the
    audit holds the stored rows against it on a live database."""

    def test_the_writers_edge_and_claim_properties_are_all_declared(self):
        from app import claims, industry, stake_history
        from app.scraper import edge_schema
        ledger = schema.all_properties()
        for prop in edge_schema.OWNS_PROPS:
            assert ("OWNS", prop) in ledger, prop
        for prop in edge_schema.ROLE_PROPS:
            assert ("HAS_ROLE", prop) in ledger, prop
        for prop in edge_schema.RELATED_TO_PROPS:
            assert ("RELATED_TO", prop) in ledger, prop
        bag = claims.claim_props(kind="owns", from_id="a", to_id="b", source_id="s")
        for prop in list(bag) + ["first_seen_at"]:
            assert ("Claim", prop) in ledger, prop
        for prop in stake_history._ROW_FIELDS:
            assert ("StakeHistory", prop) in ledger, prop
        row = industry.statement_props("e", "uk-sic", "62012", None, source_id="s")
        for prop in list(row) + ["first_seen_at"]:
            assert ("IndustryCode", prop) in ledger, prop

    def test_the_string_ledger_holds_only_what_nothing_else_declares(self):
        elsewhere = ({(t, p) for t, p, _ in schema._TYPED_PROPERTIES} | set(schema._PROPERTY_TYPES)
                     | {(t, p) for t, p, _ in schema._INDEXES} | {(t, p) for t, p, _ in schema._EDGE_INDEXES}
                     | {(t, p) for t, p in schema._FULLTEXT_INDEXES} | set(schema._RULED_STRINGS))
        strings = schema.string_properties()
        assert len(strings) == len(set(strings))                     # listed once
        for key in strings:
            assert key not in elsewhere, key
        for vtype, props in schema._STRING_PROPERTIES.items():
            assert len(props) == len(set(props)), vtype              # no twin inside a type
        # every type named in the ledger is one the bootstrap creates
        created = {t for t, _, _ in schema._INDEXES} | set(schema._EDGE_TYPES)
        for vtype in schema._STRING_PROPERTIES:
            assert vtype in created, vtype
        for vtype, _, _ in schema._TYPED_PROPERTIES:
            assert vtype in created, vtype

    def test_a_complete_catalog_declares_no_string_and_a_missing_one_is_declared(self):
        types, indexes, declared, rules = TestTypedProperties._complete()
        assert schema._statements((types, indexes, declared, rules)) == []
        del declared["Entity.legal_form"]
        assert schema._statements((types, indexes, declared, rules)) == [
            "CREATE PROPERTY Entity.legal_form IF NOT EXISTS STRING"]

    def test_the_audit_names_what_the_rows_carry_beyond_the_catalog(self):
        types, indexes, declared, rules = TestTypedProperties._complete()
        rows = {"Entity": [{"@rid": "#1:0", "id": "a", "name": "A", "countrry": "US", "founded_date": None},
                           {"@rid": "#1:1", "id": "b", "legal_form": "PLC", "founded_date": None}],
                "OWNS": [{"@rid": "#2:0", "stake_percent": 10.0, "stakke": 10}],
                "Person": []}
        asked = []

        def run(sql, *a, **kw):
            if sql == "SELECT name, properties FROM schema:types":
                return [{"name": t, "properties": [{"name": d.split(".", 1)[1]} for d in declared
                                                   if d.split(".", 1)[0] == t]} for t in types]
            if sql == "SELECT name FROM schema:indexes":
                return [{"name": i} for i in indexes]
            asked.append(sql)
            for t, r in rows.items():
                if sql.startswith(f"SELECT FROM {t} LIMIT"):
                    return r
            return []
        with _run(run):
            assert schema.undeclared_properties(sample=7) == {
                "Entity": {"countrry": {"rows": 1, "null_only": False},
                           "founded_date": {"rows": 2, "null_only": True}},     # a leftover of the rename
                "OWNS": {"stakke": {"rows": 1, "null_only": False}}}
        assert "SELECT FROM Entity LIMIT 7" in asked and "SELECT FROM Person LIMIT 7" in asked

    def test_the_audit_is_empty_without_a_catalog(self):
        def run(sql, *a, **kw):
            raise RuntimeError("no such type schema:types")
        with _run(run):
            assert schema.undeclared_properties() == {}


class TestTheBootstrapIsBatched:
    """The DDL goes to the server in scripts, not one request per statement
    (520 statements: 5.5 s singly, 0.4 s in nine scripts on 26.10.1); a
    refused script is re-issued statement by statement so the one statement
    the server refuses is still named and the rest still apply."""

    def test_a_fresh_database_gets_scripts_of_at_most_sixty_statements(self):
        scripts = []
        with patch.object(schema, "run_sql"), \
                patch.object(schema, "run_sqlscript", side_effect=lambda t, p=None: scripts.append(t)):
            res = schema.ensure_indexes()
        stmts = schema._statements(None)
        assert len(scripts) == -(-len(stmts) // schema._SCRIPT_SIZE) and len(scripts) < len(stmts) / 10
        assert all(len(schema._statements_of(t)) <= schema._SCRIPT_SIZE for t in scripts)
        assert [s for t in scripts for s in schema._statements_of(t)] == stmts     # every one, once, in order
        assert res["ok"] == stmts and res["failed"] == []

    def test_a_refused_script_falls_back_to_single_statements_and_names_the_one_refused(self):
        bad = "CREATE INDEX IF NOT EXISTS ON User (email) UNIQUE"

        def run(sql, *a, **kw):
            if sql == bad:
                raise RuntimeError("refused")
            return []
        with _run(run) as m:
            res = schema.ensure_indexes()
        issued = [c.args[0] for c in m.call_args_list]
        assert [f["stmt"] for f in res["failed"]] == [bad]
        assert issued.count(bad) == 1 and len(issued) == len(set(issued))    # nothing issued twice
        assert set(res["ok"]) | {bad} == set(s for s in issued if s.startswith(("CREATE", "ALTER", "DROP")))

    def test_a_script_and_its_statements_round_trip(self):
        stmts = ["CREATE VERTEX TYPE X IF NOT EXISTS", "ALTER PROPERTY X.c REGEXP '^(a|b)$'",
                 "CREATE PROPERTY X.d IF NOT EXISTS STRING"]
        assert schema._statements_of(schema._script(stmts)) == stmts
        assert schema._script(stmts).count(";") == 3
        for s in schema._statements(None):
            assert ";" not in s, s                                           # the separator is never inside one

    def test_an_unreachable_server_inside_a_script_still_skips(self):
        def run(sql, *a, **kw):
            if sql.startswith("CREATE"):
                raise ConnectionError("down")
            raise RuntimeError("no such type schema:types")
        with _run(run):
            assert schema.ensure_indexes()["skipped"] is True
