"""
Stamp `read_from` on the edges and claims written before the grade existed.

The grade says how surely a value was read off its document
(`edge_schema.READ_GRADES`: field > table > layout > prose). Every writer
stamps it now; this fills it in for what is already in the graph, from what
is known about each source's reader, and only where it is still unset — so
it is safe to run again and never overwrites a stamp a writer made.

What is known:

- The bulk and API sources — GLEIF, UK PSC, Wikidata, OpenCorporates — read
  named fields of JSON / NDJSON records: `field`, for owns and role alike.
- SEC EDGAR by `filing_type`: a 13F's information table, a Form 3/4 and a
  Form D are XML — `field`. A 13D/G is XML from the SEC's structured-data
  compliance date (2024-12-18, the day the form types were renamed
  `SCHEDULE 13…`) and a text cover page before it; the raw form name is not
  kept, so the filing date stands in for it: `field` from that day, `prose`
  before, untouched without a date. SEC's seats all come from XML forms
  (the 8-K reader only closes them): `field`.
- An Exhibit 21 / 8.1 list is graded per ROW while it is parsed (a table
  cell, a carried header, a paragraph) — nothing here can know that, so those
  stay unset until the next `sec-ex21` run stamps them.

Set-based SQL in batches (``UPDATE … LIMIT``), each under the DB proxy's
timeout on the dev graph. A full database re-imports its bulk sources with
the fixed code (the rebuild), as the other heals assume.
"""
from __future__ import annotations

from app.db.arcadedb import run_query, run_sql
from app.scraper.edge_schema import READ_FIELD, READ_PROSE

#: the first day a 13D/G had to be filed as structured XML (and was named
#: "SCHEDULE 13…" on EDGAR) — see sec_edgar._is_structured
STRUCTURED_13DG_FROM = "2024-12-18"

#: sources whose every record is a named field
_FIELD_SOURCES = ("GLEIF", "UK PSC", "Wikidata", "OpenCorporates")

_XML_FILINGS = "(filing_type = '13F' OR filing_type = 'Form 4' OR filing_type = '4' OR filing_type = '3')"
_SCHEDULES = "(filing_type LIKE '13D%' OR filing_type LIKE '13G%')"

_BATCH = 2000


def _source_id(name: str) -> str | None:
    rows = run_query("MATCH (s:Source {name: $n}) RETURN s.id AS id", {"n": name})
    return rows[0]["id"] if rows else None


def _count(rows) -> int:
    if not rows:
        return 0
    row = rows[0]
    return int(row.get("count") or 0)


def _stamp(table: str, grade: str, where: str, params: dict, dry_run: bool) -> int:
    """SET read_from = grade on the rows of `table` matching `where` that have
    none yet, in batches; returns how many (would be) stamped."""
    full = f"{where} AND read_from IS NULL"
    if dry_run:
        rows = run_sql(f"SELECT count(*) AS count FROM {table} WHERE {full}", params)
        return _count(rows)
    done = 0
    while True:
        n = _count(run_sql(f"UPDATE {table} SET read_from = :grade WHERE {full} LIMIT {_BATCH}",
                           {**params, "grade": grade}))
        done += n
        if n < _BATCH:
            return done


def heal_read_from(dry_run: bool = False) -> dict:
    """Fill `read_from` where it is unset and the reader is known (module
    docstring). Returns counts per (source, table, grade)."""
    counts: dict = {}

    def stamp(label: str, table: str, grade: str, where: str, params: dict) -> None:
        # several rules can stamp the same grade on one table (SEC's XML
        # filings and its structured schedules): the counts add up
        n = _stamp(table, grade, where, params, dry_run)
        if n:
            key = f"{label}:{table}:{grade}"
            counts[key] = counts.get(key, 0) + n

    for name in _FIELD_SOURCES:
        sid = _source_id(name)
        if not sid:
            continue
        for table in ("OWNS", "HAS_ROLE", "Claim"):
            stamp(name, table, READ_FIELD, "source_id = :s", {"s": sid})

    sec = _source_id("SEC EDGAR")
    if sec:
        p = {"s": sec, "d": STRUCTURED_13DG_FROM}
        for table in ("OWNS", "Claim"):
            stamp("SEC EDGAR", table, READ_FIELD, f"source_id = :s AND {_XML_FILINGS}", p)
            stamp("SEC EDGAR", table, READ_FIELD,
                  f"source_id = :s AND {_SCHEDULES} AND source_date >= :d", p)
            stamp("SEC EDGAR", table, READ_PROSE,
                  f"source_id = :s AND {_SCHEDULES} AND source_date < :d", p)
        stamp("SEC EDGAR", "HAS_ROLE", READ_FIELD, "source_id = :s", p)
        stamp("SEC EDGAR", "Claim", READ_FIELD, "source_id = :s AND kind = 'role'", p)
    counts["dry_run"] = dry_run
    return counts
