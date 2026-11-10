"""Snapshot of the dev database for the before/after comparison of a rebuild.
Usage: snapshot.py <out.json>   (read-only)

`take(q, cy)` builds the snapshot from a SQL and a Cypher runner, so a test can
hand it fakes; `main()` runs it against the dev database."""
import json
import os
import sys

BACKEND = "/home/administrator/github/pamten-backend/backend"


def take(q, cy) -> dict:
    snap = {}
    types = [t["name"] for t in q("SELECT name, type FROM schema:types")]
    snap["counts"] = {}
    for t in sorted(types):
        try: snap["counts"][t] = q(f"SELECT count(*) AS n FROM {t}")[0]["n"]
        except Exception as e: snap["counts"][t] = str(e)[:60]
    def by(sql, *keys):
        return {"|".join(str(r.get(k)) for k in keys): r["n"] for r in q(sql)}
    snap["owns_by_source"] = by("SELECT source_id AS s, count(*) AS n FROM OWNS GROUP BY source_id", "s")
    snap["owns_active"] = q("SELECT count(*) AS n FROM OWNS WHERE until IS NULL")[0]["n"]
    snap["owns_by_filing"] = by("SELECT filing_type AS f, count(*) AS n FROM OWNS GROUP BY filing_type", "f")
    snap["owns_until_reason"] = by("SELECT until_reason AS r, count(*) AS n FROM OWNS GROUP BY until_reason", "r")
    snap["owns_read_from"] = by("SELECT read_from AS r, count(*) AS n FROM OWNS GROUP BY read_from", "r")
    snap["owns_shortcut"] = by("SELECT shortcut AS r, count(*) AS n FROM OWNS GROUP BY shortcut", "r")
    snap["owns_stale"] = by("SELECT stale AS r, count(*) AS n FROM OWNS GROUP BY stale", "r")
    snap["role_by_source"] = by("SELECT source_id AS s, count(*) AS n FROM HAS_ROLE GROUP BY source_id", "s")
    snap["role_until_reason"] = by("SELECT until_reason AS r, count(*) AS n FROM HAS_ROLE GROUP BY until_reason", "r")
    snap["claims_by_source_kind"] = by("SELECT source_id AS s, kind AS k, count(*) AS n FROM Claim GROUP BY source_id, kind", "s", "k")
    snap["claims_owns_by_filing"] = by("SELECT filing_type AS f, count(*) AS n FROM Claim WHERE kind = 'owns' GROUP BY filing_type", "f")
    snap["entity_by_source"] = by("SELECT source_id AS s, count(*) AS n FROM Entity GROUP BY source_id", "s")
    snap["entity_by_type"] = by("SELECT type AS s, count(*) AS n FROM Entity GROUP BY type", "s")
    snap["person_by_source"] = by("SELECT source_id AS s, count(*) AS n FROM Person GROUP BY source_id", "s")
    snap["entity_with"] = {k: q(f"SELECT count(*) AS n FROM Entity WHERE {k} IS NOT NULL")[0]["n"] for k in (
        "lei_id", "sec_cik", "wikidata_id", "companies_house_id", "register_id", "country", "countries",
        "hq_country", "hq_city", "hq_lat", "reg_lat", "dissolved_on", "nace_code", "incorporated_on",
        "founded", "created_at", "logo_url")}
    snap["person_with"] = {k: q(f"SELECT count(*) AS n FROM Person WHERE {k} IS NOT NULL")[0]["n"] for k in (
        "wikidata_id", "sec_cik", "nationalities", "image_url", "death_date", "created_at")}
    snap["ids_by_prefix"] = {}
    for label in ("Entity", "Person"):
        c = {}
        for r in q(f"SELECT id FROM {label}"):
            i = r["id"]; p = i.split(":")[0] + ":" if ":" in i else ("uuid" if len(i) == 36 and i.count("-") == 4 else "other")
            c[p] = c.get(p, 0) + 1
        snap["ids_by_prefix"][label] = c
    snap["uuid_with_hard_id"] = {label: q(f"SELECT count(*) AS n FROM {label} WHERE id NOT LIKE '%:%' AND (wikidata_id IS NOT NULL OR sec_cik IS NOT NULL{' OR lei_id IS NOT NULL' if label == 'Entity' else ''})")[0]["n"] for label in ("Entity", "Person")}
    snap["dissolved_open_edges"] = cy("MATCH (e:Entity)-[r]-() WHERE e.dissolved_on IS NOT NULL AND r.until IS NULL AND (type(r) = 'OWNS' OR type(r) = 'HAS_ROLE') RETURN e.name AS name, count(r) AS n")
    snap["sec_entities"] = sorted(r["name"] for r in q("SELECT name FROM Entity WHERE sec_cik IS NOT NULL"))
    snap["wikidata_entities"] = sorted(r["name"] for r in q("SELECT name FROM Entity WHERE wikidata_id IS NOT NULL"))
    snap["lei_and_cik"] = sorted(f"{r['name']} {r['id']}" for r in q("SELECT name, id FROM Entity WHERE lei_id IS NOT NULL AND sec_cik IS NOT NULL"))
    snap["unilever"] = q("SELECT id, name, country, countries, lei_id, sec_cik FROM Entity WHERE name_normalized LIKE '%unilever%'")
    snap["runs_by_source"] = by("SELECT source AS s, count(*) AS n FROM ScrapeRun GROUP BY source", "s")
    snap["runs"] = q("SELECT source, target, status, started_at, total, reason FROM ScrapeRun ORDER BY started_at")
    # calls to each outside source and the bucket since the database was created,
    # i.e. this rebuild's from step 1 on ({source: {kind: n}}, app/source_calls.py)
    calls: dict = {}
    for r in q("SELECT source, kind, sum(count) AS n FROM SourceCall GROUP BY source, kind"):
        calls.setdefault(r["source"], {})[r["kind"]] = int(r.get("n") or 0)
    snap["source_calls"] = dict(sorted(calls.items()))
    # Per-node ids and names (2026-10-28): a count drop (Person −33) could not be
    # traced to WHO went missing; with these the diff names them.
    snap["entities"] = sorted(([r["id"], r.get("name"), r.get("source_id"), r.get("sec_cik"), r.get("lei_id"), r.get("wikidata_id")]
                               for r in q("SELECT id, name, source_id, sec_cik, lei_id, wikidata_id FROM Entity")), key=lambda x: x[0])
    snap["persons"] = sorted(([r["id"], r.get("full_name"), r.get("source_id"), r.get("sec_cik"), r.get("wikidata_id")]
                              for r in q("SELECT id, full_name, source_id, sec_cik, wikidata_id FROM Person")), key=lambda x: x[0])
    # Every OWNS edge (2026-10-29b): the GLEIF +102 of the last rebuild could only
    # be reconstructed, not diffed edge by edge. Cypher, since SQL in.id/out.id read 0 rows.
    # With its filing type (findings item 51): a 13F holder that comes and goes is the
    # relevance sample's boundary moving, which the diff can only tell from a lost
    # edge when it sees what wrote the edge. Older snapshots carry four fields.
    snap["owns_edges"] = sorted(([r["a"], r["b"], r.get("s"), r.get("u"), r.get("f")] for r in cy(
        "MATCH (a)-[r:OWNS]->(b) RETURN a.id AS a, b.id AS b, r.source_id AS s, r.until AS u, r.filing_type AS f")),
        key=lambda x: [str(v) for v in x])
    # The 13D filing groups (findings item 56): a VotingGroup owns the bloc's
    # OWNS edge under a uuid minted anew by every rebuild, and it is neither an
    # Entity nor a Person, so the diff had no name for it — 37 edges "gone" and
    # 37 "new" at every verify. Its identity is the issuer plus the roster (the
    # writer matches groups by roster overlap; one issuer can carry several
    # blocs — 24 distinct name keys for 37 groups on dev), so the members go in.
    snap["voting_groups"] = sorted(([r["id"], r.get("name"), r.get("nn"), sorted(m for m in (r.get("members") or []) if m)]
                                    for r in cy("MATCH (g:VotingGroup) OPTIONAL MATCH (m)-[:MEMBER_OF]->(g) "
                                                "RETURN g.id AS id, g.name AS name, g.name_normalized AS nn, "
                                                "collect(coalesce(m.name, m.full_name)) AS members")),
                                   key=lambda x: x[0])
    # Every SUCCEEDED_BY edge (findings item 89): the 11-08b verify had only the
    # count (43 of an expected 86) and had to read dev for the list.
    snap["succeeded_by"] = sorted(([r["a"], r["b"], r.get("s")] for r in cy(
        "MATCH (a)-[r:SUCCEEDED_BY]->(b) RETURN a.id AS a, b.id AS b, r.source_id AS s")),
        key=lambda x: [str(v) for v in x])
    # The two whole-graph checks (findings item 97): companies whose disclosed
    # stakes add up over 100 % and ownership cycles. Neither was looked at
    # after a rebuild; a one-off check on 2026-11-10 found 6 and 3 (items
    # 93/94/98–101). Both read the live, drawn edges the panel reads.
    live = cy("MATCH (a)-[o:OWNS]->(b) WHERE o.until IS NULL AND COALESCE(o.shortcut, false) <> true "
              "RETURN a.id AS a, b.id AS b, coalesce(a.name, a.full_name) AS an, b.name AS bn, "
              "o.stake_percent AS p, o.share_class AS c, o.filing_type AS f, o.stale AS st, "
              "o.direct_or_indirect AS d")
    snap["over_100"] = over_100(live)
    snap["cycles"] = cycles(live)
    snap["users"] = [r["email"] for r in q("SELECT email FROM User")]
    return snap


def over_100(rows: list) -> list:
    """[issuer id, name, disclosed %, [[owner id, name, %, filing type, stale]…]]
    for every company the panel flags `exceeds_100`: the app's own summary over
    the owners the profile lists — self-loops dropped, one row per owner (its
    highest stake). Pins and suppressions are not applied (a manual layer)."""
    from app.routers.search import _ownership_summary
    by: dict = {}
    for r in rows:
        if r["a"] == r["b"]:
            continue
        cur = by.setdefault(r["b"], {}).get(r["a"])
        if cur is None or (r.get("p") or -1) > (cur.get("p") or -1):
            by[r["b"]][r["a"]] = r
    out = []
    for issuer, owners in by.items():
        summary = _ownership_summary([{"owner": {"id": o["a"]},
                                       "relationship": {"stake_percent": o.get("p"), "share_class": o.get("c"),
                                                        # an indirect holding beside a direct
                                                        # owner is not added (item 101)
                                                        "direct_or_indirect": o.get("d")}}
                                      for o in owners.values()])
        if summary.get("exceeds_100"):
            # the rows the summary added: an indirect stake beside a direct
            # owner's stated one is not (item 101)
            stated = any(isinstance(x.get("p"), (int, float)) and x.get("d") != "indirect"
                         for x in owners.values())
            added = [o for o in owners.values() if not (stated and o.get("d") == "indirect")]
            rows_ = sorted(([o["a"], o.get("an"), o["p"], o.get("f"), bool(o.get("st"))]
                            for o in added if isinstance(o.get("p"), (int, float))),
                           key=lambda x: (-x[2], x[0]))
            name = next(iter(owners.values())).get("bn")
            out.append([issuer, name, round(sum(x[2] for x in rows_), 2), rows_])
    return sorted(out, key=lambda x: x[0])


def cycles(rows: list) -> list:
    """Every ownership cycle as its sorted member ids: the strongly connected
    components of more than one node, and each self-loop on its own. Tarjan,
    iterative (the live graph is ~12k edges; recursion would hit the limit)."""
    succ: dict = {}
    loops = set()
    for r in rows:
        if r["a"] == r["b"]:
            loops.add(r["a"])
        succ.setdefault(r["a"], set()).add(r["b"])
        succ.setdefault(r["b"], set())
    index, low, on, stack, comps, n = {}, {}, set(), [], [], 0
    for root in sorted(succ):
        if root in index:
            continue
        work = [(root, iter(sorted(succ[root])))]
        index[root] = low[root] = n; n += 1; stack.append(root); on.add(root)
        while work:
            v, it = work[-1]
            w = next(it, None)
            if w is not None:
                if w not in index:
                    index[w] = low[w] = n; n += 1; stack.append(w); on.add(w)
                    work.append((w, iter(sorted(succ[w]))))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    x = stack.pop(); on.discard(x); comp.append(x)
                    if x == v:
                        break
                if len(comp) > 1:
                    comps.append(sorted(comp))
    comps += [[x] for x in loops]
    return sorted(comps)


def main() -> None:
    out = sys.argv[1]
    os.chdir(BACKEND); sys.path.insert(0, ".")
    from dotenv import load_dotenv; load_dotenv(".env")
    from app.db.arcadedb import run_sql as q, run_query as cy
    snap = take(q, cy)
    json.dump(snap, open(out, "w"), indent=1, default=str)
    print("snapshot:", out, "| entities", snap["counts"].get("Entity"), "| persons", snap["counts"].get("Person"),
          "| OWNS", snap["counts"].get("OWNS"), "| claims", snap["counts"].get("Claim"), "| runs", len(snap["runs"]))
    # step 8 prints both checks in full: each one is a finding or a known case
    print(f"over 100 %: {len(snap['over_100'])}")
    for issuer, name, total, owners in snap["over_100"]:
        print(f"  {total:7.2f}  {name}  {issuer}")
        for oid, oname, pct, ft, stale in owners[:6]:
            print(f"           {pct:8.3f} {ft or '-':<6} {'stale' if stale else '':<5} {oname} ({oid})")
    print(f"ownership cycles: {len(snap['cycles'])}")
    names = {e[0]: e[1] for e in snap["entities"]}
    for comp in snap["cycles"]:
        print("  " + (" <-> ".join(f"{names.get(i, '?')} ({i})" for i in comp) if len(comp) > 1
                      else f"self-loop {names.get(comp[0], '?')} ({comp[0]})"))


if __name__ == "__main__":
    main()
