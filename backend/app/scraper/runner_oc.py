"""
Scraper runner — the OpenCorporates scrape (`run_scrape_open_corporates`, the
registered `open_corporates` scraper).

What this module needs of the runner — its own helpers, and the app-level names
it imports (the database, the settings, the write layer) — it reaches through
`runner` (`runner.db`, `runner._upsert_role`), not through imports of its own:
a test that replaces one of them on the runner then covers every pipeline, as
it did while this code was part of runner.py (until 2026-11-25).
"""

from app.scraper import runner


# ── OpenCorporates helpers ────────────────────────────────────────────────────

def _upsert_role_oc(person_id: str, entity_id: str, role: str,
                    start_date: str | None, end_date: str | None,
                    source_id: str, source_url: str | None = None, credibility_score: int = 85):
    """Create a HAS_ROLE edge attributed to OpenCorporates if not already present.

    Stamps per-entry provenance: source_url = the OpenCorporates company page,
    source_date = the officer's start date, last_scraped_at = now (refreshed on
    re-scrape).
    """
    runner._record_edge_parties(entity_id)
    now = runner._now_iso()
    runner.record_claim(kind=runner.KIND_ROLE, from_id=person_id, to_id=entity_id, source_id=source_id,
                 role=role, since=start_date, until=end_date, source_url=source_url,
                 source_date=now[:10],
                 credibility_score=credibility_score, read_from=runner.READ_FIELD)
    with runner.db.get_session() as session:
        # The same position over a touching period (graph_writer._same_seat);
        # an open one, or a closed spell this entry re-states.
        existing = runner._same_seat(session, person_id, entity_id, role, start_date, end_date)
        if existing:
            # OpenCorporates' resignation date reaches the open seat — it was
            # ignored here, so a resigned officer stayed current. Only a seat
            # that began by then. The start: the earlier of the two.
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role
                  AND (($stored_until IS NULL AND r.until IS NULL) OR r.until = $stored_until)
                SET r.last_scraped_at = $now,
                    r.source_url = COALESCE($surl, r.source_url),
                    r.since = $start,
                    r.until = CASE WHEN r.until IS NULL AND $until IS NOT NULL
                                    AND ($start IS NULL OR $start <= $until)
                               THEN $until ELSE r.until END
                """,
                pid=person_id, eid=entity_id, role=existing["role"], now=now,
                stored_until=existing.get("until"), surl=source_url, until=end_date,
                start=runner.combined_start(existing.get("since"), start_date),
            )
            runner._relabel_if_more_credible(session, person_id, entity_id,
                                      existing["role"], role, existing["cred"],
                                      credibility_score, source_id, runner.READ_FIELD)
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: $since, until: $until,
                source_id: $sid, credibility_score: $score,
                source_url: $surl, source_date: $sdate, last_scraped_at: $now,
                read_from: $rfrom
            }]->(e)
            """,
            pid=person_id, eid=entity_id, role=role, rfrom=runner.READ_FIELD,
            since=start_date, until=end_date,
            sid=source_id, score=credibility_score,
            # undated: listed as of today (the evidence date), not "no date"
            sdate=start_date or now[:10],
            surl=source_url, now=now,
        )


# ── OpenCorporates public entry point ─────────────────────────────────────────

@runner._with_autodedup
def run_scrape_open_corporates(company_name: str, country: str | None = None) -> dict:
    """
    Scrape OpenCorporates for registration details and officers for one company.
    Requires SCRAPER_ENABLED=true AND SCRAPER_OPENCORPORATES_ENABLED=true.

    `country` is passed to the API as `jurisdiction_code`, so the search runs
    inside that country. The returned code is still checked — one line, no extra
    call — because a filter that silently stops working is the kind of thing that
    is only noticed once wrong data is in the graph.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )
    if not runner.settings.SCRAPER_OPENCORPORATES_ENABLED:
        raise PermissionError(
            "OpenCorporates scraper is disabled. "
            "Set SCRAPER_OPENCORPORATES_ENABLED=true in the environment to enable."
        )
    if not runner.get_source_enabled("open_corporates"):
        raise PermissionError(
            "OpenCorporates source is disabled. Enable it in the Scraper panel."
        )

    from app.scraper.open_corporates import scrape_company

    runner.log.info("OpenCorporates runner: starting scrape for %r", company_name)
    data = scrape_company(company_name, country)

    if not data:
        return {
            "status":  "no_results",
            "company": company_name,
            "total":   0,
            "scraped": [],
        }

    oc_country = (data.get("jurisdiction_code") or "")[:2].upper() or None
    if not runner.matches_requested(oc_country, country):
        runner.log.info("OpenCorporates: %r is registered in %s, not %s — skipping",
                 data.get("name"), oc_country, country)
        return runner.country_mismatch(company_name, oc_country, country)

    source_id = runner._ensure_source(runner.OPENCORPORATES_SOURCE_NAME, runner.OPENCORPORATES_SOURCE_URL, runner.OPENCORPORATES_CREDIBILITY)
    scraped: list[dict] = []

    # Verifiable per-record URL for this company on OpenCorporates
    company_url = runner._opencorporates_url(
        data.get("jurisdiction_code"), data.get("company_number"),
    )

    # Upsert the target company
    target_id = runner._upsert_entity_by_name(
        name=data["name"],
        entity_type="company",
        source_id=source_id,
    )
    runner._stamp_registration(target_id, data.get("jurisdiction_code"), data.get("company_number"))
    scraped.append({"type": "entity", "name": data["name"], "role": "target"})
    # The register's incorporation date was fetched and thrown away, so time
    # travel never knew when the company began. It only moves EARLIER (the
    # same rule as the bulk importers) and it is the legal entity's start,
    # not the business's: `founded` (the year of origin) is left to Wikidata.
    inc = (data.get("incorporation_date") or "")[:10]
    if len(inc) == 10 and inc[4] == "-" and inc[7] == "-":
        with runner.db.get_session() as session:
            session.run(
                "MATCH (e:Entity {id: $id}) "
                "SET e.incorporated_on = CASE WHEN e.incorporated_on IS NULL OR $d < e.incorporated_on "
                "THEN $d ELSE e.incorporated_on END",
                id=target_id, d=inc)

    # Registered address → geocoded onto the entity itself.
    address = data.get("registered_address") or {}
    if address.get("city") or address.get("country"):
        runner._geocode_registered_and_attach(target_id, address)
        city    = address.get("city", "")
        country = address.get("country", "")
        scraped.append({"type": "location", "city": city, "country": country,
                        "role": "registered_address"})

    # Officers → Person or Entity nodes + HAS_ROLE edges
    for officer in data.get("officers", []):
        name = officer.get("name", "").strip()
        role = officer.get("role", "Officer")
        if not name:
            continue

        if runner.is_person_name(name):
            person_id = runner._upsert_person_by_name(name, source_id=source_id)
            _upsert_role_oc(
                person_id, target_id, role,
                officer.get("start_date"), officer.get("end_date"),
                source_id, source_url=company_url,
            )
            scraped.append({"type": "person", "name": name, "role": role})
        else:
            runner._upsert_entity_by_name(name=name, entity_type="company", source_id=source_id)
            scraped.append({"type": "entity", "name": name, "role": role})

        runner.log.info("OpenCorporates: wrote %r → %r (%s)", name, data["name"], role)

    runner.log.info(
        "OpenCorporates runner: finished %r — %d nodes written",
        company_name, len(scraped),
    )
    return {
        "status":             "ok",
        "company":            company_name,
        "jurisdiction_code":  data.get("jurisdiction_code"),
        "company_number":     data.get("company_number"),
        "total":              len(scraped),
        "scraped":            scraped,
    }
