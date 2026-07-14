"""
Compatibility shim: presents the same db.get_session() / session.run() /
result.single() / record["key"] interface that the routers already use,
but delegates all I/O to the ArcadeDB HTTP client.

ArcadeDB serialisation notes
-----------------------------
• Multi-item returns  (RETURN a, r, b):  each item appears under its
  variable name as a dict with @cat/"v"/"e" for vertex/edge.
• Single-item returns (RETURN n):        ArcadeDB flattens the node
  properties to the top row level — NO variable key is emitted.
  The fallback in _Record.__getitem__ detects this case by checking
  for the @rid marker and wraps the whole row as a _NodeWrapper.
• @cat == "v" → vertex, @cat == "e" → edge  (@type is the class name).
• @props may appear at the top of multi-item rows; no code touches it.
• Write commands (CREATE, MERGE, SET …) return [] — the existing code
  only checks .single() for None or iterates with for, so [] is fine.
"""

import re
from contextlib import contextmanager
from app.db.arcadedb import run_query, run_command

_WRITE_RE = re.compile(
    r'\b(CREATE|SET|DELETE|MERGE|REMOVE|DETACH|DROP)\b', re.IGNORECASE
)
# String literals and line comments are stripped before the keyword scan so
# that a data value quoted directly in the query (e.g. a company named
# "Delete Corp") can never be mistaken for a write clause. All current
# call sites pass data via $params rather than inlining it, so this is a
# hardening measure rather than a fix for an observed misroute.
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _is_write(cypher: str) -> bool:
    stripped = _LINE_COMMENT_RE.sub("", cypher)
    stripped = _STRING_LITERAL_RE.sub("", stripped)
    return bool(_WRITE_RE.search(stripped))


# ── Value wrappers ─────────────────────────────────────────────────────────────

def _wrap(value):
    """Recursively wrap ArcadeDB values into Python-friendly types."""
    if isinstance(value, dict):
        cat = value.get("@cat")
        if cat in ("v", "e"):
            return _NodeWrapper(value)
        if cat == "p":
            return _PathWrapper(value)
        # Plain map (e.g. collect({owner: o, rel: r})) — wrap nested nodes
        return {k: _wrap(v) for k, v in value.items()}
    if isinstance(value, list):
        # A path is serialised as an alternating [vertex, edge, vertex, …] list.
        # A list of ONLY vertices — e.g. collect(DISTINCT node) — is not a path;
        # it must contain at least one edge. (Otherwise a single-node collect,
        # ["v"], was misread as a one-vertex path and became non-iterable.)
        if value and all(isinstance(x, dict) and x.get("@cat") in ("v", "e")
                         for x in value):
            if any(x.get("@cat") == "e" for x in value):
                return _PathWrapper(value)
        return [_wrap(v) for v in value]
    return value


class _NodeWrapper:
    """
    Wraps an ArcadeDB vertex or edge dict.
    Strips @-prefixed ArcadeDB metadata so dict() / .get() return clean dicts.
    """

    def __init__(self, data: dict):
        self._data = {k: v for k, v in data.items() if not k.startswith("@")}

    # dict(wrapper) calls keys() then __getitem__
    def keys(self):
        return self._data.keys()

    def __getitem__(self, key):
        return _wrap(self._data[key])

    def get(self, key, default=None):
        if key not in self._data:
            return default
        return _wrap(self._data[key])

    def __contains__(self, key):
        return key in self._data

    def __iter__(self):
        return iter(self._data)

    def __bool__(self):
        return bool(self._data)

    def items(self):
        return [(k, _wrap(v)) for k, v in self._data.items()]

    def __repr__(self):
        return f"_NodeWrapper({self._data!r})"


class _PathWrapper:
    """
    Wraps an ArcadeDB path so path.nodes and path.relationships work as on
    exposing .nodes and .relationships lists.

    Handles two formats:
    • list  – alternating [vertex, edge, vertex, …] (openCypher serialisation)
    • dict  – @cat == "p" with @vertices/@edges keys
    """

    def __init__(self, data):
        if isinstance(data, list):
            self.nodes         = [_NodeWrapper(x) for i, x in enumerate(data)
                                  if x.get("@cat") == "v"]
            self.relationships = [_NodeWrapper(x) for x in data
                                  if x.get("@cat") == "e"]
        elif isinstance(data, dict):
            verts = data.get("@vertices") or data.get("@nodes") or []
            edges = data.get("@edges") or data.get("@relationships") or []
            self.nodes         = [_NodeWrapper(v) for v in verts]
            self.relationships = [_NodeWrapper(e) for e in edges]
        else:
            self.nodes         = []
            self.relationships = []


# ── Result / Record / Session wrappers ────────────────────────────────────────

class _Record:
    def __init__(self, row: dict):
        self._row = row

    def __getitem__(self, key):
        if key not in self._row:
            # Single-node/edge RETURN: ArcadeDB puts properties at the top
            # level instead of nesting under the variable name.
            if "@rid" in self._row or self._row.get("@cat") in ("v", "e"):
                return _NodeWrapper(self._row)
            raise KeyError(key)
        return _wrap(self._row[key])

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __bool__(self):
        return bool(self._row)

    def __iter__(self):
        return iter(self._row)

    def __repr__(self):
        return f"_Record({self._row!r})"


class _Result:
    def __init__(self, rows: list[dict]):
        self._records = [_Record(r) for r in rows]

    def single(self) -> "_Record | None":
        return self._records[0] if self._records else None

    def __iter__(self):
        return iter(self._records)


class _Session:
    def run(self, cypher: str, **params) -> _Result:
        fn   = run_command if _is_write(cypher) else run_query
        rows = fn(cypher, params)
        return _Result(rows)


class _Connection:
    @contextmanager
    def get_session(self):
        yield _Session()


# Singleton — imported everywhere as: from app.database import db
db = _Connection()
