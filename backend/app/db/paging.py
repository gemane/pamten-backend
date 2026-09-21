"""Paging a vertex type by its UNIQUE `id` index — the one way that survives
a full-size database.

Three facts, all learned on the 34 GB sizing database (ArcadeDB 26.7.3,
14.2M entities, 13.8M persons) and invisible on a small one:

* `ORDER BY id LIMIT n` without a predicate is planned as FETCH FROM TYPE —
  a scan of the whole type (6.4 GB of Entity) before the first page. The
  same is true of `@rid > last` paging: every page is a scan.
* a ONE-SIDED ascending range (`id > 'x' ORDER BY id`) over that index dies
  inside the iterator — `NullPointerException: "convertedKeys" is null`,
  whatever the bound — while the TWO-SIDED `id > 'x' AND id < 'y'` is a
  0.1–0.3 s index read per 5,000 rows. Same plan (FETCH FROM INDEX) either
  way, so EXPLAIN is necessary but not sufficient. `BETWEEN` is no
  alternative: it is planned as a scan and materialises the whole range.
* a page can come back SHORT of its LIMIT long before the index ends
  (3,664 of 5,000 on the first real page): LIMIT counts index entries,
  stale ones included. The walk ends on an EMPTY page, never a short one.

`iter_id_pages` is that walk. Ids are ASCII — `lei:`, `gb-coh:`, `chpsc:`,
`wd:`, uuids — so a prefix's upper bound is the prefix with its last
character bumped, and the whole space ends below `ID_CEILING`.
"""
from __future__ import annotations

from typing import Iterator

from app.db.arcadedb import run_sql

#: Sorts after every id: the highest code point of the Basic Multilingual Plane.
ID_CEILING = "￿"


def _sql_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def prefix_ceiling(prefix: str) -> str:
    """The smallest string above every string that starts with `prefix`
    (`'lei:'` → `'lei;'`); the whole space when the prefix is empty."""
    if not prefix:
        return ID_CEILING
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def id_page_sql(vtype: str, after: str, below: str, page: int,
                columns: str = "id, @rid AS rid") -> str:
    """One page: ids strictly between `after` and `below`, ascending."""
    return (f"SELECT {columns} FROM {vtype} "
            f"WHERE id > '{_sql_str(after)}' AND id < '{_sql_str(below)}' "
            f"ORDER BY id LIMIT {page}")


def iter_id_pages(vtype: str, prefix: str = "", page: int = 5000,
                  columns: str = "id, @rid AS rid") -> Iterator[list[dict]]:
    """Yield the vertices of `vtype` whose id starts with `prefix`, in id
    order, `page` rows at a time — every page an index range read.

    `columns` must include `id`; it is the cursor. The walk stops at the
    first empty page.
    """
    below = prefix_ceiling(prefix)
    last = prefix
    while True:
        rows = run_sql(id_page_sql(vtype, last, below, page, columns))
        if not rows:
            return
        yield rows
        last = rows[-1]["id"]
