"""Proxy-statement writer: read a company's latest DEF 14A and record each owner's
voting power — a claim per owner, and the figure on the pair's active OWNS edge.

Findings item 46: the writer used to SET `voting_power_pct` on the edge with no
Claim behind it (no source, no filing type, no reading grade — the Sources panel
could not say where the figure came from, and a rebuild lost it), rename the
owner to the proxy's spelling ("Brin Sergey" → "Sergey Brin") with no claim or
name history, and, without an `entity_id`, look the company up as `(c:Company)`,
a vertex type that does not exist — so a name-only call never found it.
"""
import re as _re
from app.db.arcadedb import run_query, run_command
from app.db.anchors import label_or_entity
from app.scraper.edge_schema import READ_TABLE
from app.scraper.names import _person_name_variants, _entity_name_variants

#: the claim's record kind — its own SEC filing family (claims._SEC_FAMILIES),
#: so it never shares a key with the pair's 13D/G or Form 4 claim
FILING_TYPE = "DEF 14A"


def _find_company(company: str, entity_id: str | None) -> dict | None:
    """The graph's node for the company: by id, else the same resolver /search
    and every on-demand scrape use."""
    if entity_id:
        rows = run_query(
            "MATCH (c:Entity {id: $id}) RETURN c.id AS id, c.name AS name, c.sec_cik AS sec_cik LIMIT 1",
            {"id": entity_id},
        )
        return rows[0] if rows else None
    from app.routers.search import resolve_best_entity
    found = resolve_best_entity(company, None)
    return ({"id": found["id"], "name": found.get("name"), "sec_cik": found.get("sec_cik")}
            if found else None)


def write_proxy_ownership(company: str, entity_id: str | None = None) -> dict:
    """
    Read the most recent DEF 14A proxy statement and record each owner's voting
    power: an SEC EDGAR claim (`filing_type` "DEF 14A", `read_from` "table",
    the filing as `source_url`, its date as `source_date`) and the figure on
    the pair's active OWNS edge.

    Only an owner the graph already links to the company is written — the
    proxy's table names people, not the stake's evidence trail, so it adds the
    voting figure to an edge a register or filing drew, never a new edge. An
    owner the filing puts "below 1 %" (`*`) gets nothing: there is no number.
    Owner names are matched by: exact full_name / name, normalised name, SEC
    reversed-name variants, nickname expansion, and entity article/punctuation
    variants. The stored name is never changed — it is its own source's spelling.
    Edges that cannot be matched are reported in 'not_found_in_db'.
    """
    from app.claims import KIND_OWNS, record_claim
    from app.scraper.graph_writer import _ensure_source
    from app.scraper.mapper import coherent_ownership_type, normalize_entity_name
    from app.scraper.proxy_statement import fetch_proxy_ownership
    from app.scraper.runner import (SEC_EDGAR_CREDIBILITY, SEC_EDGAR_SOURCE_NAME,
                                    SEC_EDGAR_SOURCE_URL)

    node = _find_company(company, entity_id)
    if not node:
        hint = " (try passing entity_id directly)" if not entity_id else ""
        return {"owners": [], "db_error": f"Company not found in DB: '{company}'{hint}"}
    company_id, company_name = node["id"], node.get("name") or company

    # the CIK the graph holds beats a name search (findings item 47)
    proxy = fetch_proxy_ownership(company, cik=node.get("sec_cik"))
    if proxy.get("error") and not proxy.get("owners"):
        return {**proxy, "entity_id": company_id}

    source_id    = _ensure_source(SEC_EDGAR_SOURCE_NAME, SEC_EDGAR_SOURCE_URL, SEC_EDGAR_CREDIBILITY)
    filing_date  = (proxy.get("filing_date") or "")[:10] or None
    filing_url   = proxy.get("filing_url")

    updated:   list[dict] = []
    not_found: list[str]  = []
    skipped:   list[str]  = []

    for owner in proxy.get("owners", []):
        name = owner["name"]
        pct  = owner.get("voting_power_pct")
        if pct is None:
            skipped.append(name)
            continue

        # Combine person (reordering + nickname) and entity (article/punct) variants
        variants = list(dict.fromkeys(
            _person_name_variants(name) + _entity_name_variants(name)
        ))
        # Normalised form: also strip leading "The" before normalising
        name_no_the = _re.sub(r"^the\s+", "", name.strip(), flags=_re.IGNORECASE)
        name_norm     = normalize_entity_name(name)
        name_norm_the = normalize_entity_name(name_no_the)

        # ── Find active OWNS edge owner → company ──────────────────────────
        # Anchored on the labelled company; the owner is reached over the edge
        # and its label comes back with it, so the write below can name it.
        match_rows = run_query(
            """MATCH (n)-[r:OWNS]->(c:Entity {id: $cid})
               WHERE r.until IS NULL
                 AND (n.full_name IN $variants OR n.name IN $variants
                      OR n.name_normalized IN $norms)
               RETURN n.id AS oid, labels(n)[0] AS olabel,
                      coalesce(n.full_name, n.name) AS matched_name,
                      r.stake_percent   AS stake,
                      r.ownership_type  AS ownership_type
               LIMIT 1""",
            {"cid": company_id, "variants": variants,
             "norms": list(dict.fromkeys([name_norm, name_norm_the]))},
        )
        if not match_rows:
            not_found.append(name)
            continue

        row          = match_rows[0]
        oid          = row["oid"]
        olabel       = label_or_entity(row.get("olabel"))

        # ── The claim: where the figure comes from ────────────────────────
        record_claim(
            kind=KIND_OWNS, from_id=oid, to_id=company_id, source_id=source_id,
            voting_power_pct=pct, source_url=filing_url, source_date=filing_date,
            credibility_score=SEC_EDGAR_CREDIBILITY, filing_type=FILING_TYPE,
            read_from=READ_TABLE,
        )

        # ── The figure on the edge ────────────────────────────────────────
        #
        # An UPDATE of the one property, not a delete-and-recreate: recreating
        # from a hand-written list once stripped `source_url`, `psc_self_link`
        # and five more (#256). The edge's evidence date is kept if it has one.
        run_command(
            f"""MATCH (n:{olabel} {{id: $oid}})-[r:OWNS]->(c:Entity {{id: $cid}})
               WHERE r.until IS NULL
               SET r.voting_power_pct = $pct,
                   r.ownership_type   = $otype,
                   r.source_date      = COALESCE(r.source_date, $pdate)""",
            {
                "oid":   oid,
                "cid":   company_id,
                "pct":   pct,
                "pdate": filing_date,
                # A stake and `unknown` cannot both be true; re-derive rather than
                # carry the contradiction forward.
                "otype": coherent_ownership_type(row.get("stake"), row.get("ownership_type")),
            },
        )
        updated.append({
            "proxy_name":       name,
            "db_name":          row["matched_name"],
            "voting_power_pct": pct,
        })

    return {
        "company":              company_name,
        "entity_id":            company_id,     # the run row names it (findings item 42)
        "filing_date":          proxy.get("filing_date"),
        "filing_url":           filing_url,
        "share_class_structure": proxy.get("share_class_structure"),
        "updated":              updated,
        "not_found_in_db":      not_found,
        "skipped_no_pct":       skipped,
    }
