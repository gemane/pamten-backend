"""Snapshot of the dev database for the before/after comparison of a rebuild.
Usage: snapshot.py <out.json>   (read-only)"""
import os, sys, json
os.chdir("/home/administrator/github/pamten-backend/backend"); sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv(".env")
from app.db.arcadedb import run_sql as q, run_query as cy
out = sys.argv[1]
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
# Per-node ids and names (2026-10-28): a count drop (Person −33) could not be
# traced to WHO went missing; with these the diff names them.
snap["entities"] = sorted(([r["id"], r.get("name"), r.get("source_id"), r.get("sec_cik"), r.get("lei_id"), r.get("wikidata_id")]
                           for r in q("SELECT id, name, source_id, sec_cik, lei_id, wikidata_id FROM Entity")), key=lambda x: x[0])
snap["persons"] = sorted(([r["id"], r.get("full_name"), r.get("source_id"), r.get("sec_cik"), r.get("wikidata_id")]
                          for r in q("SELECT id, full_name, source_id, sec_cik, wikidata_id FROM Person")), key=lambda x: x[0])
# Every OWNS edge (2026-10-29b): the GLEIF +102 of the last rebuild could only
# be reconstructed, not diffed edge by edge. Cypher, since SQL in.id/out.id read 0 rows.
snap["owns_edges"] = sorted(([r["a"], r["b"], r.get("s"), r.get("u")] for r in cy(
    "MATCH (a)-[r:OWNS]->(b) RETURN a.id AS a, b.id AS b, r.source_id AS s, r.until AS u")),
    key=lambda x: [str(v) for v in x])
snap["users"] = [r["email"] for r in q("SELECT email FROM User")]
json.dump(snap, open(out, "w"), indent=1, default=str)
print("snapshot:", out, "| entities", snap["counts"].get("Entity"), "| persons", snap["counts"].get("Person"),
      "| OWNS", snap["counts"].get("OWNS"), "| claims", snap["counts"].get("Claim"), "| runs", len(snap["runs"]))
