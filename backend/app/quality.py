"""
The quality report — data quality as numbers, not impressions.

Prompted by a strategy question: the user is weighing how much to rely on
Wikidata against the statutory sources, and the honest answer needs measuring
rather than asserting. This module computes the figures that decision turns on,
per source and overall, so that every later change to the source mix can be
judged by the same report run before and after.

What it measures, and why each line is on the report:

* **Stake coverage** — an OWNS edge with a percentage is worth more than one
  without. GLEIF's 0% here is by design (consolidation, not shareholding);
  Wikidata's near-0% is the weakness the strategy contains.
* **Corroboration** — how many relationships more than one source asserts. The
  `Claim` rows have recorded this since claims shipped; nothing aggregated them
  until now. This is the single best quality number the graph has.
* **Official identity** — entities carrying a register id (LEI / CIK / Companies
  House) versus entities only Wikidata knows. The breadth-vs-quality tension in
  one ratio.
* **Freshness** — when each source's edges were last re-confirmed. A fact nobody
  has looked at for a year is a weaker fact.
* **Contradictions** — the invariants this week's bugs violated (a stake stored
  beside `unknown`, a company owning itself, an edge citing one source with
  another's link). All were repaired; the report keeps them at zero, and a
  non-zero here is a regression alarm, not information.

Read-only throughout: the report must be safe to run against any database at any
time, including production during an import.
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.db.arcadedb import run_sql
from app.database import db

log = logging.getLogger(__name__)

#: Hosts each source's provenance links live on — the provenance-mismatch gauge.
#: A link on an edge attributed to a source whose host this maps to, pointing
#: elsewhere, is the #261 bug shape.
_SOURCE_HOSTS = {
    "SEC EDGAR": "sec.gov",
    "Wikidata": "wikidata.org",
    "GLEIF": "gleif.org",
    "UK PSC": "company-information.service.gov.uk",
    "OpenCorporates": "opencorporates.com",
}

_FRESHNESS_WINDOWS = (30, 90, 365)


def _source_names() -> dict:
    return {r["id"]: r["name"] for r in run_sql("SELECT id, name FROM Source")}


def _owns_by_source(names: dict) -> dict:
    """Per-source OWNS figures: totals, stakes, closures, freshness."""
    now = datetime.now(timezone.utc)
    cutoffs = {d: (now - timedelta(days=d)).isoformat() for d in _FRESHNESS_WINDOWS}
    per: dict = defaultdict(lambda: {
        "edges": 0, "with_stake": 0, "closed": 0,
        **{f"confirmed_{d}d": 0 for d in _FRESHNESS_WINDOWS},
    })
    with db.get_session() as s:
        rows = s.run("""MATCH ()-[r:OWNS]->()
                        RETURN r.source_id AS sid, r.stake_percent AS stake,
                               r.until AS until, r.last_scraped_at AS seen""")
        for r in rows:
            src = names.get(r["sid"], "(unattributed)")
            p = per[src]
            p["edges"] += 1
            if r["stake"] is not None:
                p["with_stake"] += 1
            if r["until"]:
                p["closed"] += 1
            seen = r["seen"] or ""
            for d in _FRESHNESS_WINDOWS:
                if seen and seen >= cutoffs[d]:
                    p[f"confirmed_{d}d"] += 1
    return dict(per)


def _corroboration() -> dict:
    """How many distinct sources assert each (from, to, kind) relationship.

    Claims are keyed per source, so grouping them by the relationship they
    describe gives the corroboration count directly — the aggregation that was
    never written. Streamed and grouped in Python: the pair count is bounded by
    the edge count, and ArcadeDB's GROUP BY over three columns plus COUNT
    DISTINCT is not worth trusting for a read-only report.
    """
    pairs: dict = defaultdict(set)
    for r in run_sql("SELECT from_id, to_id, kind, source_id FROM Claim"):
        pairs[(r["from_id"], r["to_id"], r["kind"])].add(r["source_id"])
    if not pairs:
        return {"relationships_with_claims": 0, "corroborated": 0, "corroborated_pct": 0.0,
                "by_source_count": {}}
    counts = defaultdict(int)
    for sources in pairs.values():
        counts[len(sources)] += 1
    corroborated = sum(n for k, n in counts.items() if k >= 2)
    return {
        "relationships_with_claims": len(pairs),
        "corroborated": corroborated,
        "corroborated_pct": round(corroborated / len(pairs) * 100, 1),
        "by_source_count": dict(sorted(counts.items())),
    }


def _identity() -> dict:
    """Official ids versus Wikidata-only — the breadth/quality ratio."""
    total = run_sql("SELECT count(*) AS n FROM Entity")[0]["n"]
    official = run_sql("""SELECT count(*) AS n FROM Entity WHERE lei_id IS NOT NULL
                          OR sec_cik IS NOT NULL OR companies_house_id IS NOT NULL""")[0]["n"]
    wd_only = run_sql("""SELECT count(*) AS n FROM Entity WHERE wikidata_id IS NOT NULL
                         AND lei_id IS NULL AND sec_cik IS NULL
                         AND companies_house_id IS NULL""")[0]["n"]
    return {
        "entities": total,
        "with_official_id": official,
        "with_official_id_pct": round(official / total * 100, 1) if total else 0.0,
        "wikidata_only": wd_only,
        "wikidata_only_pct": round(wd_only / total * 100, 1) if total else 0.0,
        # Neither: name-only nodes (PSC parties without a number, BODS leftovers).
        "no_id_at_all": total - official - wd_only,
    }


def _contradictions(names: dict) -> dict:
    """The invariants recent bugs violated. All were repaired; a non-zero here is
    a regression alarm. Each key names the incident that minted it."""
    with db.get_session() as s:
        stake_unknown = list(s.run(
            """MATCH ()-[r:OWNS]->() WHERE r.stake_percent IS NOT NULL
               AND r.ownership_type = 'unknown' RETURN count(r) AS n"""))[0]["n"]
        self_loops = list(s.run(
            "MATCH (a)-[r:OWNS]->(b) WHERE a.id = b.id RETURN count(r) AS n"))[0]["n"]
        mismatched = 0
        for r in s.run("""MATCH ()-[r:OWNS]->() WHERE r.source_url IS NOT NULL
                          RETURN r.source_id AS sid, r.source_url AS url"""):
            want = _SOURCE_HOSTS.get(names.get(r["sid"], ""))
            if want and want not in str(r["url"]):
                mismatched += 1
    return {
        "stake_with_unknown_type": stake_unknown,   # the grey-badge bug (#259)
        "self_owning_edges": self_loops,            # the 7.48% bug (#259/#260)
        "provenance_mismatches": mismatched,        # the wrong-link bug (#261)
    }


def quality_report() -> dict:
    """The whole report. Read-only; safe anywhere, any time."""
    names = _source_names()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "owns_by_source": _owns_by_source(names),
        "corroboration": _corroboration(),
        "identity": _identity(),
        "contradictions": _contradictions(names),
    }


def format_report(report: dict) -> str:
    """The report as the table a terminal wants, mirroring the dict exactly."""
    lines = [f"Quality report — {report['generated_at'][:19]}", ""]
    lines.append(f"{'source':<16} {'edges':>6} {'stake%':>7} {'closed':>7} "
                 + " ".join(f"{'<' + str(d) + 'd':>6}" for d in _FRESHNESS_WINDOWS))
    for src, p in sorted(report["owns_by_source"].items(), key=lambda kv: -kv[1]["edges"]):
        stake_pct = round(p["with_stake"] / p["edges"] * 100) if p["edges"] else 0
        lines.append(f"{src:<16} {p['edges']:>6} {stake_pct:>6}% {p['closed']:>7} "
                     + " ".join(f"{p[f'confirmed_{d}d']:>6}" for d in _FRESHNESS_WINDOWS))
    c = report["corroboration"]
    lines += ["", f"corroboration: {c['corroborated']} of {c['relationships_with_claims']} "
                  f"claimed relationships have ≥2 sources ({c['corroborated_pct']}%)"]
    i = report["identity"]
    lines += [f"identity: {i['with_official_id']}/{i['entities']} entities carry an official id "
              f"({i['with_official_id_pct']}%); {i['wikidata_only']} are Wikidata-only "
              f"({i['wikidata_only_pct']}%); {i['no_id_at_all']} have no id at all"]
    x = report["contradictions"]
    flag = "" if not any(x.values()) else "   ⚠ REGRESSION"
    lines += [f"contradictions: stake-with-unknown={x['stake_with_unknown_type']} "
              f"self-loops={x['self_owning_edges']} "
              f"provenance-mismatches={x['provenance_mismatches']}{flag}"]
    return "\n".join(lines)
