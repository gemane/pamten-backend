"""The calls to each outside source, per step of the rebuild (findings item 135).

`SourceCall` holds one total per day, so a rebuild knew that EDGAR was asked
14,399 times and for what (11,753 times a filer's record), not WHEN: the
scrapes, the Exhibit 21 phases and step 7's passes over the whole graph were
one number. `rebuild.sh` and the replay call this after each step that can ask
a source: the totals so far, less the totals at the last call, go to
`calls.tsv` in the kit folder ("step<TAB>source[/request type]<TAB>calls"),
which the snapshot keeps and the verify's diff compares.

Usage: python calls_by_step.py "<step label>"
"""
import json
import os
import sys

BACKEND = "/home/administrator/github/pamten-backend/backend"
HERE = os.path.dirname(os.path.abspath(__file__))
TSV = os.path.join(HERE, "calls.tsv")
STATE = os.path.join(HERE, "calls_state.json")


def totals(q) -> dict[str, int]:
    """Every call counted so far: {"sec-edgar/filer-record": n, "gleif": n, …}
    — a source's typed calls under "source/type", the rest under the source."""
    out: dict[str, int] = {}
    for r in q("SELECT source, what, sum(count) AS n FROM SourceCall GROUP BY source, what") or []:
        if not r.get("source"):
            continue
        key = f"{r['source']}/{r['what']}" if r.get("what") else r["source"]
        out[key] = out.get(key, 0) + int(r.get("n") or 0)
    return out


def record(label: str, now: dict[str, int], tsv: str = TSV, state: str = STATE) -> dict[str, int]:
    """What `label` added since the last call: written to `tsv`, returned, and
    `now` kept as the next call's start. No state file is a start from zero
    (the database was just created). A total that fell (a database dropped
    between two calls) counts from zero, never below."""
    try:
        before = json.load(open(state))
    except (OSError, ValueError):
        before = {}
    step = {k: n - before.get(k, 0) if n >= before.get(k, 0) else n for k, n in now.items()}
    step = {k: n for k, n in sorted(step.items()) if n > 0}
    with open(tsv, "a") as f:
        for key, n in step.items():
            f.write(f"{label}\t{key}\t{n}\n")
    with open(state, "w") as f:
        json.dump(now, f)
    return step


def read(path: str | None = None) -> list:
    """[[step, source[/type], calls], …] from the kit folder's calls.tsv; []
    when there is none (a `before.json`, a kit older than this file)."""
    try:
        lines = open(path or TSV).read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].strip().isdigit():
            out.append([parts[0], parts[1], int(parts[2])])
    return out


def by_source(step: dict[str, int]) -> dict[str, int]:
    """{source: calls} of one step, the request types added up."""
    out: dict[str, int] = {}
    for key, n in step.items():
        src = key.split("/")[0]
        out[src] = out.get(src, 0) + n
    return out


def line(label: str, step: dict[str, int]) -> str:
    """One line for the log: "  calls in <step>: sec-edgar 4,210 (filer-record
    3,900), wikidata 12" — network sources only, the most asked first."""
    net = {s: n for s, n in by_source(step).items() if not s.startswith("bucket:")}
    if not net:
        return f"  calls in {label}: none"
    parts = []
    for src, n in sorted(net.items(), key=lambda kv: (-kv[1], kv[0])):
        types = sorted(((k.split("/", 1)[1], v) for k, v in step.items() if k.startswith(src + "/")),
                       key=lambda kv: (-kv[1], kv[0]))
        top = f" ({types[0][0]} {types[0][1]:,})" if types else ""
        parts.append(f"{src} {n:,}{top}")
    return f"  calls in {label}: " + ", ".join(parts)


def main() -> None:
    label = sys.argv[1]
    os.chdir(BACKEND); sys.path.insert(0, ".")
    from dotenv import load_dotenv; load_dotenv(".env")
    from app.db.arcadedb import run_sql as q
    print(line(label, record(label, totals(q))))


if __name__ == "__main__":
    main()
