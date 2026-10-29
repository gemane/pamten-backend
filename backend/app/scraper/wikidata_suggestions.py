"""What the registers would correct on Wikidata — a report, never an edit.

Since 2026-10-29 the Wikidata writer takes an item's LEI (P1278) and SEC CIK
(P5531) as a pointer and stores neither: a hard id on a node comes from its
register (`register_pointer`). The comparison that decision rests on runs the
other way too. A node that carries a Wikidata item id and a register id says
what the item should state, and where the item states something else, the
register says whose id that is. BlackRock's Q219635 still states the pre-2024
parent's CIK and LEI, which GLEIF and EDGAR name "BlackRock Finance, Inc.".

This module reads the graph (nodes with a `wikidata_id`), the items' current
statements and the registers' records, and writes two files for a person to
review and submit: a CSV with one row per finding, and a QuickStatements batch
of the additions it is confident about. It never writes to Wikidata, and it
never writes to the graph.

Kinds of finding, per item and property:

- ``missing``  the item states no value; the node's register id is the
               suggestion (``add``). ``high`` confidence only when the register
               agrees with the item on more than the name — the register's name
               matches the item's label or an alias AND (the register's country
               is one the item states OR the item already states the node's
               other register id). The node may have met the item by name
               alone, and a name match is how the BlackRock mix-up began;
               anything less is ``review`` and stays out of the batch.
- ``differs``  the item states values and the node's id is not among them —
               a stale id or an id of another legal entity. ``review``: which
               it is, and whether the old value is deprecated, ended or moved
               to another item, is a person's call (and QuickStatements cannot
               change a rank).
- ``unknown``  the register does not know a value the item states.
- ``several``  the item states more than one value, the node's among them —
               an organisation item holding several legal entities' ids
               (Google, Barclays). ``info`` only.

Licences: GLEIF's data is CC0 and EDGAR's is a US-government work, so both may
go into Wikidata (CC0). Companies House (OGL) is not used here.
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import date

log = logging.getLogger(__name__)

PROPS = {"P1278": "lei_id", "P5531": "sec_cik"}
GLEIF_RECORD_PAGE = "https://search.gleif.org/#/record/{lei}"
EDGAR_FILER_PAGE = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
_BATCH = 50          # wbgetentities takes at most 50 ids per call

CSV_FIELDS = ("qid", "label", "property", "kind", "action", "confidence", "current",
              "suggested", "evidence_url", "register_name", "node_id", "node_name", "note")


def _norm(prop: str, value: str | None) -> str | None:
    from app.scraper.mapper import normalize_cik
    from app.scraper.wikidata import normalize_lei
    return normalize_lei(value) if prop == "P1278" else normalize_cik(value)


def evidence_url(prop: str, value: str) -> str:
    return (GLEIF_RECORD_PAGE.format(lei=value) if prop == "P1278"
            else EDGAR_FILER_PAGE.format(cik=value))


# ── Reading ───────────────────────────────────────────────────────────────────

def graph_nodes() -> list[dict]:
    """Every node with a Wikidata item id, with its register ids (read-only)."""
    from app.db.arcadedb import run_sql
    return list(run_sql("SELECT id, name, wikidata_id, lei_id, sec_cik FROM Entity "
                        "WHERE wikidata_id IS NOT NULL"))


def _statements(claims: dict, prop: str) -> list[str]:
    """The values an item states for a property, deprecated ones left out (a
    deprecated value is the item's own record that it is wrong)."""
    out = []
    for st in claims.get(prop) or []:
        if st.get("rank") == "deprecated":
            continue
        value = _norm(prop, ((st.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
        if value and value not in out:
            out.append(value)
    return out


def _item_facts(entity: dict) -> dict:
    claims = entity.get("claims") or {}
    label = ((entity.get("labels") or {}).get("en") or {}).get("value")
    aliases = [a.get("value") for a in (entity.get("aliases") or {}).get("en") or [] if a.get("value")]
    countries = [((st.get("mainsnak") or {}).get("datavalue") or {}).get("value", {}).get("id")
                 for st in claims.get("P17") or [] if st.get("rank") != "deprecated"]
    return {"label": label, "aliases": aliases, "country_qids": [c for c in countries if c],
            **{p: _statements(claims, p) for p in PROPS}}


def fetch_items(qids: list[str]) -> dict[str, dict]:
    """Label, English aliases, countries (P17) and the stated LEIs and CIKs of
    each item, read from the action API in batches of 50."""
    from app.scraper.wikidata import WIKIDATA_API, _api_json, _wd_get
    items: dict[str, dict] = {}
    for i in range(0, len(qids), _BATCH):
        body = _api_json(_wd_get(WIKIDATA_API, {
            "action": "wbgetentities", "ids": "|".join(qids[i:i + _BATCH]),
            "props": "labels|aliases|claims", "languages": "en", "format": "json"}, timeout=60))
        for qid, entity in (body.get("entities") or {}).items():
            if "missing" not in entity:
                items[qid] = _item_facts(entity)
    return items


def fetch_country_codes(country_qids: set[str]) -> dict[str, str]:
    """Country item → ISO 3166-1 alpha-2 (P297)."""
    from app.scraper.wikidata import WIKIDATA_API, _api_json, _wd_get
    codes: dict[str, str] = {}
    qids = sorted(country_qids)
    for i in range(0, len(qids), _BATCH):
        body = _api_json(_wd_get(WIKIDATA_API, {
            "action": "wbgetentities", "ids": "|".join(qids[i:i + _BATCH]),
            "props": "claims", "format": "json"}, timeout=60))
        for qid, entity in (body.get("entities") or {}).items():
            for st in (entity.get("claims") or {}).get("P297") or []:
                value = ((st.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                if value and st.get("rank") != "deprecated":
                    codes[qid] = value.upper()
                    break
    return codes


def gleif_facts(lei: str) -> dict | None:
    """GLEIF's names, country and registration status for an LEI — None when
    GLEIF does not know it. Read through `register_pointer`'s mapping of the
    API record, the one the pointer writes nodes with."""
    from app.scraper.register_pointer import fetch_gleif_record
    rec = fetch_gleif_record(lei)
    if not rec:
        return None
    entity = rec.get("Entity") or {}
    names = [(entity.get("LegalName") or {}).get("$")]
    names += [n.get("$") for n in (entity.get("OtherEntityNames") or {}).get("OtherEntityName") or []]
    country = (((entity.get("LegalAddress") or {}).get("Country") or {}).get("$")
               or ((entity.get("LegalJurisdiction") or {}).get("$") or "")[:2] or None)
    return {"names": [n for n in names if n], "country": country.upper() if country else None,
            "status": ((rec.get("Registration") or {}).get("RegistrationStatus") or {}).get("$")}


def edgar_facts(cik: str) -> dict | None:
    """EDGAR's name, former names and country for a CIK — None when EDGAR has
    no such filer."""
    import httpx

    from app.scraper.sec_edgar import _submissions, sec_country
    try:
        sub = _submissions(cik)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    if not sub or not sub.get("name"):
        return None
    names = [sub["name"]] + [f.get("name") for f in sub.get("formerNames") or [] if f.get("name")]
    return {"names": names, "country": sec_country(sub), "status": None}


# ── Judging ───────────────────────────────────────────────────────────────────

def _name_matches(register_names: list[str], item: dict) -> bool:
    from app.scraper.mapper import normalize_entity_name
    item_names = {normalize_entity_name(n) for n in [item.get("label"), *item.get("aliases", [])] if n}
    item_names.discard("")
    return any(normalize_entity_name(n) in item_names for n in register_names if n)


def suggestions_for(node: dict, item: dict, register, iso_of: dict[str, str]) -> list[dict]:
    """The findings for one node and its item. ``register(prop, value)`` gives
    a register's facts for an id (or None when it does not know it);
    ``iso_of`` maps the item's country items to ISO-2 codes."""
    rows = []
    item_countries = {iso_of[q] for q in item.get("country_qids", []) if q in iso_of}
    other = {"P1278": "P5531", "P5531": "P1278"}

    def row(prop, kind, action, confidence, current, suggested, facts, note):
        return {"qid": node["wikidata_id"], "label": item.get("label"), "property": prop,
                "kind": kind, "action": action, "confidence": confidence,
                "current": "; ".join(current), "suggested": suggested or "",
                "evidence_url": evidence_url(prop, suggested) if suggested else "",
                "register_name": (facts or {}).get("names", [""])[0] if facts else "",
                "node_id": node["id"], "node_name": node.get("name"), "note": note}

    for prop, field in PROPS.items():
        stated = item.get(prop) or []
        held = _norm(prop, node.get(field))
        for value in stated:
            if value != held and register(prop, value) is None:
                rows.append(row(prop, "unknown", "review", "review", stated, None, None,
                                f"{'GLEIF' if prop == 'P1278' else 'EDGAR'} does not know {value}"))
        if not held:
            continue
        facts = register(prop, held)
        if facts is None:          # the node's own id unknown to its register: nothing to say
            continue
        if not stated:
            name_ok = _name_matches(facts["names"], item)
            country_ok = bool(facts.get("country")) and facts["country"] in item_countries
            other_held = _norm(other[prop], node.get(PROPS[other[prop]]))
            other_ok = bool(other_held) and other_held in (item.get(other[prop]) or [])
            sure = name_ok and (country_ok or other_ok)
            why = [w for w, ok in (("name", name_ok), ("country", country_ok),
                                   (f"the item's {other[prop]}", other_ok)) if ok]
            rows.append(row(prop, "missing", "add", "high" if sure else "review", [], held, facts,
                            "agrees on " + (", ".join(why) if why else "nothing but the graph's link")))
        elif held not in stated:
            whose = []
            for value in stated:
                f = register(prop, value)
                if f:
                    whose.append(f"{value} is {f['names'][0]}"
                                 + (f" ({f['status']})" if f.get("status") not in (None, "ISSUED") else ""))
            rows.append(row(prop, "differs", "review", "review", stated, held, facts,
                            "; ".join(whose) or "the stated values are unknown to the register"))
        elif len(stated) > 1:
            rows.append(row(prop, "several", "info", "info", stated, None, facts,
                            f"{len(stated)} values; the node's is {held}"))
    return rows


# ── The report ────────────────────────────────────────────────────────────────

def build_report(limit: int | None = None) -> dict:
    """Read the graph, the items and the registers; return the findings and a
    count per kind. Every register record is fetched once."""
    nodes = sorted(graph_nodes(), key=lambda n: n["wikidata_id"])
    if limit:
        nodes = nodes[:limit]
    items = fetch_items(sorted({n["wikidata_id"] for n in nodes}))
    iso_of = fetch_country_codes({q for it in items.values() for q in it["country_qids"]})
    cache: dict[tuple[str, str], dict | None] = {}

    def register(prop: str, value: str) -> dict | None:
        if (prop, value) not in cache:
            cache[(prop, value)] = gleif_facts(value) if prop == "P1278" else edgar_facts(value)
        return cache[(prop, value)]

    rows = []
    for node in nodes:
        item = items.get(node["wikidata_id"])
        if item is None:
            log.info("Wikidata item %s of %s is missing or deleted", node["wikidata_id"], node["id"])
            continue
        rows.extend(suggestions_for(node, item, register, iso_of))
    counts: dict[str, int] = {"nodes": len(nodes), "items": len(items)}
    for r in rows:
        key = r["kind"] + ("_high" if r["confidence"] == "high" else "")
        counts[key] = counts.get(key, 0) + 1
    return {"rows": rows, "counts": counts}


def quickstatements(rows: list[dict], today: date) -> list[str]:
    """QuickStatements (v1) lines for the confident additions, each referenced
    by the register's record page (P854) and the day it was read (P813)."""
    return [f'{r["qid"]}\t{r["property"]}\t"{r["suggested"]}"\tS854\t"{r["evidence_url"]}"'
            f"\tS813\t+{today.isoformat()}T00:00:00Z/11"
            for r in rows if r["kind"] == "missing" and r["confidence"] == "high"]


def write_report(report: dict, out_dir: str, today: date | None = None) -> dict:
    """The CSV of every finding and the QuickStatements batch; their paths."""
    today = today or date.today()
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"wikidata-suggestions-{today.isoformat()}.csv")
    qs_path = os.path.join(out_dir, f"wikidata-suggestions-{today.isoformat()}.qs.txt")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(report["rows"])
    lines = quickstatements(report["rows"], today)
    with open(qs_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    return {"csv": csv_path, "quickstatements": qs_path, "statements": len(lines)}
