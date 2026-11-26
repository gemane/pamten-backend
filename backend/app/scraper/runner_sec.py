"""
Scraper runner — the SEC EDGAR scrape of one company and its seat holders.

`run_scrape_sec_edgar` (the registered `sec_edgar` scraper): the filer, its
13D/G holders, its insiders and their holdings, its own statements; then the
seat holders asked for their Form 4 holdings (`run_sec_seat_holders`).

What this module needs of the runner — its own helpers, and the app-level names
it imports (the database, the settings, the write layer) — it reaches through
`runner` (`runner.db`, `runner._upsert_role`), not through imports of its own:
a test that replaces one of them on the runner then covers every pipeline, as
it did while this code was part of runner.py (until 2026-11-25).
"""
from datetime import datetime, timezone

from app.scraper import runner


def _vertex_holding_cik(cik: str | None) -> str | None:
    """"Person" or "Entity": which vertex type already carries this SEC CIK,
    None when neither does. A CIK is one filer, a human being or a company,
    never both."""
    cik = runner.normalize_cik(cik)
    if not cik:
        return None
    with runner.db.get_session() as session:
        if list(session.run("MATCH (p:Person {sec_cik: $cik}) RETURN p.id AS id LIMIT 1", cik=cik)):
            return "Person"
        return "Entity" if runner.resolve_entity_id(session, sec_cik=cik) else None


def _stamp_found_cik(person_id: str, cik: str) -> str:
    """Give a person the CIK their own Form 4 was filed under, which the
    person-centric lookup found by name (findings item 29). Only when no
    vertex holds that CIK yet — one CIK, one party — and returns the person's
    id, the keyed one when they were uuid-keyed."""
    if _vertex_holding_cik(cik):
        return person_id
    with runner.db.get_session() as session:
        return runner._stamp_person_cik(session, person_id, cik)


def _holder_of_found_cik(person_id: str, cik: str) -> str | None:
    """Whose holding a Form 4 is, found by a seat holder's name and filed under
    ``cik``: the person already keyed by that CIK when there is one — the
    Form 4's own reporting owner — else the seat holder, stamped with it
    (`_stamp_found_cik`). None when a company holds the CIK: a name that
    matched a company's filing is not this person's.

    Before, a CIK another person held left the seat holder unstamped AND took
    the holding: the 13D/G check asked the Wikidata twin, which held none,
    while the 13G sat on the EDGAR node — Galperin, Gracias and Tsai each got a
    Form 4 edge beside their 13G, and the person merge kept the Form 4
    (findings item 73, rebuild 2026-11-07). On the holder, the check sees the
    13D/G; the twin is left to the person dedup, which joins the two by the
    seat and the CIK as before."""
    held = _vertex_holding_cik(cik)
    if held == "Entity":
        return None
    if held == "Person":
        return _find_person("", sec_cik=runner.normalize_cik(cik)) or person_id
    return _stamp_found_cik(person_id, cik)


def _is_individual(name: str, stated: bool | None, cik: str | None) -> bool:
    """Is a 13D/G reporting person a human being? Item 8's code when it says
    (``stated``), else the vertex that already holds the CIK, else the shape
    of the name. A legal-entity suffix vetoes a person classification the
    filing got wrong (a misread Item 8 code): "Berkshire Hathaway Inc" is a
    company however the type field parsed.

    The CIK check keeps one filer one vertex type. Dondero's HC code made an
    Entity "Dondero James D" with his CIK, and his Form 4 a Person with the
    same id (findings item 21)."""
    if stated is None:
        held = _vertex_holding_cik(cik)
        stated = held == "Person" if held else runner.is_person_name(name)
    return bool(stated) and not runner.has_entity_suffix(name)


def _find_person(full_name: str, sec_cik: str | None = None) -> str | None:
    """An existing Person by SEC CIK, else by exact name — no creation."""
    with runner.db.get_session() as session:
        if sec_cik:
            rows = list(session.run("MATCH (p:Person {sec_cik: $cik}) RETURN p.id AS id LIMIT 1",
                                    cik=sec_cik))
            if rows:
                return rows[0]["id"]
        rows = list(session.run("MATCH (p:Person {full_name: $name}) RETURN p.id AS id LIMIT 1",
                                name=full_name))
        return rows[0]["id"] if rows else None


def _denominator_of(exec_rec: dict, data: dict) -> float | None:
    """The count a Form 4 holding was measured against. A record from
    `sec_insiders.stake_for` always carries `shares_outstanding` — the count of
    the holding's own class, or None when the issuer's classes did not match
    it, which must stay None (the issuer's total is another class's count).
    A record without the key predates the per-class stake: the total, as
    it always was."""
    if "shares_outstanding" in exec_rec:
        return exec_rec["shares_outstanding"]
    return data.get("shares_outstanding")


def _note_form4_yields(scraped: list[dict], holder: str, company: str) -> None:
    """The holder's Form 4 count is of record; their 13D/G statement is the
    stake (`sec_writer.stated_by_13dg`). Noted in the run, written nowhere."""
    scraped.append({"type": "owns", "name": holder, "role": "insider owner (13D/G states the holding)"})
    runner.log.info("SEC EDGAR: %r → %r stated by a 13D/G; the Form 4 count is of record, not written",
             holder, company)


def _stored_entity(company_name: str, country: str | None = None) -> dict | None:
    """The node the database already holds for the company a query names, or
    None. The same resolver the Exhibit 21, acquisitions and 13F passes use
    before they read EDGAR by number — a credible match only, so a loose name
    cannot hand the scrape a neighbour's CIK. Best-effort: an unreachable
    database means the name search, as before."""
    try:
        from app.routers.search import resolve_best_entity
        return resolve_best_entity(company_name, country)
    except Exception as exc:  # noqa: BLE001 — the lookup only saves a search
        runner.log.warning("SEC EDGAR runner: stored-CIK lookup failed for %r: %s", company_name, exc)
        return None


#: the answers of a source that found nothing it would write
FOUND_NOTHING = ("no_results", "not_a_company")


def stored_hard_ids(query: str, country: str | None = None) -> tuple:
    """(LEI, CIK, QID) of the node the query resolves to; () for none."""
    stored = _stored_entity(query, country)
    return (stored.get("lei_id"), stored.get("sec_cik"), stored.get("wikidata_id")) if stored else ()


def rerun_for_new_ids(spec, result, ids_before: tuple | None, query: str,
                      country: str | None = None) -> bool:
    """Whether `spec` runs once more after the other sources: it reads the
    stored ids (`reads_stored_ids`), it found nothing, and the node the query
    names now holds an id it did not hold before the run. Toast, Inc. was new
    in the 10-31b replay: Wikidata searched "TOAST INC" by name and refused the
    two unrelated "Toast" items, then the SEC wrote the node with its CIK —
    the id Wikidata's Toast item states (findings item 27). Unchanged ids mean
    the same answer again, so no second ask."""
    if not spec.reads_stored_ids or ids_before is None:
        return False
    if not isinstance(result, dict) or result.get("status") not in FOUND_NOTHING:
        return False
    after = stored_hard_ids(query, country)
    return any(after) and after != ids_before


def _stored_sec_cik(company_name: str, country: str | None = None) -> str | None:
    """The CIK the database already holds for the company a query names, or None."""
    return (_stored_entity(company_name, country) or {}).get("sec_cik") or None


def _confirm_stored_lei(stored: dict | None, data: dict) -> None:
    """The stored node's LEI for the filer the name search found, when the
    filer's own filings state it (`sec_lei`) — so the write lands on that
    node instead of minting a twin keyed by the CIK (Unilever, 2026-10-27).

    Asked only in the one shape a name proposes and no id settles: the node
    the query resolved to has an LEI and no CIK, EDGAR states no LEI for the
    filer, and the two names key alike. On a confirmation `data` gains the
    LEI and the filing that states it; otherwise nothing changes."""
    if not stored or data.get("lei") or not data.get("cik"):
        return
    lei = stored.get("lei_id")
    if not lei or stored.get("sec_cik"):
        return
    stored_key = stored.get("name_normalized") or runner.normalize_entity_name(stored.get("name") or "")
    if not stored_key or runner.normalize_entity_name(data.get("name") or "") != stored_key:
        return
    from app.scraper.sec_lei import confirm_filer_lei
    hit = confirm_filer_lei(data["cik"], lei, [data.get("name"), stored.get("name")])
    if hit:
        data["lei"] = lei
        data["lei_source_url"] = hit["source_url"]


@runner._with_autodedup
def run_scrape_sec_edgar(company_name: str, country: str | None = None) -> dict:
    """
    Scrape SEC EDGAR for ownership and executive data about one company.
    Requires SCRAPER_ENABLED=true AND SCRAPER_SEC_EDGAR_ENABLED=true.

    With `country`, a filer registered elsewhere is rejected before anything is
    written.

    Checked afterwards rather than asked for up front, unlike Wikidata and
    OpenCorporates, because EDGAR's search-side `State=` filter is the wrong
    field — it matches the *business address*, which for a foreign filer is
    usually its US filing office. Deutsche Bank AG lists New York and states no
    incorporation at all; Siemens AG states Germany and gives no address country.
    Filtering the search by that field would hide both from a German search. So
    the single match EDGAR returns is judged on `stateOfIncorporation`, which is
    the field that answers the question — and a filer that states none is not an
    answer to "in Germany".
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )
    if not runner.settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError(
            "SEC EDGAR scraper is disabled. "
            "Set SCRAPER_SEC_EDGAR_ENABLED=true in the environment to enable."
        )
    if not runner.get_source_enabled("sec_edgar"):
        raise PermissionError(
            "SEC EDGAR source is disabled. Enable it in the Scraper panel."
        )

    # Every SEC request the scrape and its writer lose to the network is counted
    # into the result (findings item 45): a lost Form 4 or 13D/G document is
    # dropped by its caller so the scrape goes on, and only the count tells a
    # rebuild that a company's numbers rest on a gap.
    from app.scraper.sec_client import collect_lost_requests, one_filer_record_per_scrape
    # … and a filer's record is requested once for the scrape and its writer
    # together (findings item 135: 57 % of a scrape's requests were re-reads)
    with collect_lost_requests() as lost, one_filer_record_per_scrape():
        out = _scrape_sec_edgar(company_name, country)
    if isinstance(out, dict):
        out["lost_requests"] = len(lost)
        if lost:
            runner.log.error("SEC EDGAR runner: %r lost %d request(s) to the network",
                      company_name, len(lost))
    return out


def _scrape_sec_edgar(company_name: str, country: str | None) -> dict:
    # Import here to avoid circular imports and to keep the cold-start fast
    from app.scraper.sec_edgar import scrape_company
    from app.scraper.sec_filer import fetch_filer_country

    runner.log.info("SEC EDGAR runner: starting scrape for %r", company_name)
    stored = _stored_entity(company_name, country)
    data = scrape_company(company_name, cik=(stored or {}).get("sec_cik") or None)

    if not data:
        return {
            "status":  "no_results",
            "company": company_name,
            "total":   0,
            "scraped": [],
        }

    # The submissions document was already fetched for former names and the LEI,
    # and is cached, so this costs nothing — and it settles the country question
    # before the first write.
    filer_country = fetch_filer_country(data["cik"]) if data.get("cik") else None
    if not runner.matches_requested(filer_country, country):
        runner.log.info("SEC EDGAR: %r is registered in %s, not %s — skipping",
                 data["name"], filer_country, country)
        return runner.country_mismatch(company_name, filer_country, country)
    _confirm_stored_lei(stored, data)

    # One statement per claim per run: a holder's 13D/G figure and then their
    # Form 4 figure go into the one claim, and only the last is the answer —
    # without this every run recorded a stake flap (app.stake_history).
    with runner.one_statement_per_run():
        return _write_sec_edgar(company_name, data, filer_country)


def _keep_cover_cusip(entity_id: str | None, cusip: str | None) -> None:
    """The issuer's CUSIP as a 13D/G cover states it, on the issuer's node —
    fill-if-missing, so a hand-set or 13F-adopted value is never clobbered.
    `sec-13f` then matches info-table rows exactly, and the securities bridge
    (findings item 71) reads it as the issuer's US ISIN. Every path that reads
    a cover keeps it: the issuer's own scrape, and a filer's scrape for each
    company it holds (before 2026-11-07 only the first did, so 75 dev nodes
    carried one)."""
    if not entity_id or not cusip:
        return
    with runner.db.get_session() as session:
        session.run("MATCH (e:Entity {id: $id}) SET e.cusip = COALESCE(e.cusip, $c)",
                    id=entity_id, c=cusip)


def _keep_ads_cusips(entity_id: str | None, cusips: list[str] | None) -> None:
    """A foreign issuer's ADS CUSIP(s) from its F-6 filings (`sec_f6`, findings
    item 72), added to the node's `cusips` — the list `sec-13f` searches 13F
    tables with and the securities bridge derives US ISINs from — and filling
    `cusip` when it is empty, as a 13F scrape does. Added, never replacing:
    a 13D/G cover's or a 13F table's code stays."""
    if not entity_id or not cusips:
        return
    with runner.db.get_session() as session:
        row = session.run("MATCH (e:Entity {id: $id}) RETURN e.cusips AS cs", id=entity_id).single()
        if row is None:
            return
        known = list(row.get("cs") or [])
        merged = list(dict.fromkeys([*known, *cusips]))
        if merged == known:
            return
        session.run("MATCH (e:Entity {id: $id}) SET e.cusips = $cs, e.cusip = COALESCE(e.cusip, $c)",
                    id=entity_id, cs=merged, c=cusips[0])


def _record_sec_industry(entity_id: str, cik: str | None, source_id: str) -> None:
    """EDGAR's SIC code for a filer, as the SEC's statement of its industry
    (app.industry). The submissions document is the one the name, country
    and address were read from. Nothing for an entity without a CIK."""
    if not cik:
        return
    from app.scraper.sec_filer import fetch_filer_sic
    sic = fetch_filer_sic(cik)
    if sic:
        runner.industry.record(entity_id, "us-sic", sic[0], sic[1], source_id=source_id,
                        source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
                        source_date=datetime.now(timezone.utc).date().isoformat())


def _write_ownership_filings(target_id: str, target_name: str, filings: list[dict], source_id: str,
                              scraped: list[dict]) -> None:
    """Write an issuer's 13D/G statements (`sec_13dg.fetch_ownership_filings`):
    the holder nodes, the voting groups and joint filings, and the OWNS edges,
    open or already ended. One function for the issuer's own scrape and for
    the statements the whole-graph table pass reads for a company nobody
    scraped (`sec_writer.write_statements_since`, findings item 116)."""
    for filing in filings:
        investor_name = filing.get("investor_name", "").strip()
        if not investor_name:
            continue

        # Prefer the explicit Item 8 "Type of Reporting Person" field parsed
        # from the SC 13D/13G filing (is_individual=True → IN code).
        # Fall back to the name heuristic only when the document wasn't fetched.
        is_individual = _is_individual(investor_name, filing.get("is_individual"),
                                       filing.get("investor_cik"))

        if is_individual:
            investor_node_id = runner._upsert_person_by_name(investor_name, source_id=source_id,
                                                      sec_cik=filing.get("investor_cik"))
            scraped.append({"type": "person", "name": investor_name, "role": "investor"})
        else:
            investor_node_id = runner._upsert_entity_by_name(
                name=investor_name,
                entity_type="company",
                cik=filing.get("investor_cik"),
                source_id=source_id,
            )
            scraped.append({"type": "entity", "name": investor_name, "role": "investor"})
            _record_sec_industry(investor_node_id, filing.get("investor_cik"), source_id)

        # A group of parties acting together, filed on one schedule. The bloc
        # belongs to the group, not to whoever submitted the form — hanging
        # 52.3% off BRC said BRC votes it, when in truth nine parties do and BRC
        # merely filed. Only Schedule 13D: a 13G reporting "shared voting power"
        # is an asset manager aggregating across its own subsidiaries (State
        # Street, Morgan Stanley), which is not a governance bloc at all.
        members = filing.get("group_members") or []
        if members and runner._is_control_filing(filing.get("form_type")):
            # A voting pact or a joint filing (findings item 103, the user's
            # choice of 2026-11-11): a statement naming an agreement over the
            # votes is a "Voting group"; every other joint 13D is one holder
            # with its affiliates filing together — Berkshire, Buffett, National
            # Indemnity, GEICO on DaVita — a "Joint filing".
            pact = bool(filing.get("voting_pact"))
            roster = [runner._member_key(investor_name, filing.get("investor_cik"))]
            roster += [runner._member_key(m["name"], m.get("cik")) for m in members]
            group_id = runner._upsert_voting_group(
                subject_id=target_id, subject_name=target_name, roster=roster,
                source_id=source_id, kind=runner.VOTING_PACT if pact else runner.JOINT_FILING, filer=investor_name)

            # The filer joins as a member like everybody else; it gets no edge
            # of its own, or the company would list both it and the group.
            runner._upsert_group_membership(investor_node_id, group_id,
                                     "Person" if is_individual else "Entity", source_id)
            joined = {investor_node_id}
            for m in members:
                m_individual = _is_individual(m["name"], runner._individual_by_code(m.get("type_code")),
                                              m.get("cik"))
                if m_individual:
                    mid = runner._upsert_person_by_name(m["name"], source_id=source_id,
                                                sec_cik=m.get("cik"))
                    scraped.append({"type": "person", "name": m["name"], "role": "group member"})
                else:
                    mid = runner._upsert_entity_by_name(name=m["name"], entity_type="company",
                                                 cik=m.get("cik"), source_id=source_id)
                    scraped.append({"type": "entity", "name": m["name"], "role": "group member"})
                runner._upsert_group_membership(mid, group_id,
                                         "Person" if m_individual else "Entity", source_id)
                joined.add(mid)
            # the roster is this filing's now: who it no longer names has left
            runner._prune_group_membership(group_id, joined)

            runner._upsert_owns_sec(
                owner_id=group_id, owned_id=target_id, source_id=source_id,
                owner_label="VotingGroup",
                read_from=filing.get("read_from"),
                ownership_type=filing.get("ownership_type", "unknown"),
                file_date=filing.get("file_date"),
                # No stake: the group's members hold the shares individually, and
                # a bloc percentage summed with theirs would exceed the company.
                # A joint filing's figure is one holding, but a controller's own
                # statement may already hold its vehicle's block (Lennar: Stuart
                # Miller 60.4 % beside MP Alpha's 58.6 %) — counting it waits for
                # those to be merged (findings item 107); the label is item 103's.
                stake_percent=None,
                voting_power_pct=filing.get("voting_power_pct") or filing.get("stake_percent"),
                # The bloc's count belongs on the group's edge above all: this
                # is the one place it is not a number repeated by nine parties.
                voting_shares=filing.get("voting_shares"),
                share_class=filing.get("share_class"),
                filing_type=filing.get("filing_type"),
                source_url=filing.get("source_url"),
                event_date=filing.get("event_date"),
                since_date=filing.get("since"), since_basis=filing.get("since_basis"),
                final_report=filing.get("final_report"),   # the group's last report
            )
            # Retire the edge this filing used to produce. Before groups existed
            # the bloc was written straight onto the filer, and that row is not
            # merely stale — it is the same filing, now represented by the group,
            # so leaving it would show the company both its group and BRC each
            # voting 52.3%. Scoped hard: same source, same filer, no stake of its
            # own, so a member's genuine holding is untouched.
            runner._retire_superseded_bloc_edge(investor_node_id, target_id, source_id,
                                         "Person" if is_individual else "Entity")
            runner.log.info("SEC EDGAR: wrote voting group of %d over %r (filed by %r)",
                     len(roster), target_name, investor_name)
            continue

        runner._upsert_owns_sec(
            owner_id=investor_node_id,
            owned_id=target_id,
            source_id=source_id,
            read_from=filing.get("read_from"),
            ownership_type=filing.get("ownership_type", "unknown"),
            file_date=filing.get("file_date"),
            stake_percent=filing.get("stake_percent"),
            voting_power_pct=filing.get("voting_power_pct"),
            share_class=filing.get("share_class"),
            shares=filing.get("shares"),
            shares_outstanding=filing.get("shares_outstanding"),
            denominator_date=filing.get("denominator_date"),
            event_date=filing.get("event_date"),
            since_date=filing.get("since"), since_basis=filing.get("since_basis"),
            voting_shares=filing.get("voting_shares"),
            source_url=filing.get("source_url"),
            filing_type=filing.get("filing_type"),
            # A newer filing reported no position — the holding is history.
            # The holdings path below has always done this; this one did not.
            until=filing.get("until"),
            # Item 5 ticked: the filer's last report — dimmed, not closed (item 35)
            final_report=filing.get("final_report"),
            owner_label="Person" if is_individual else "Entity",
        )
        runner.log.info(
            "SEC EDGAR: wrote OWNS %r → %r (%s)",
            investor_name, target_name, filing.get("form_type"),
        )


def _owners_were_lost(data: dict) -> bool:
    """Did this scrape lose the issuer's 13D/G owners to the network
    (`sec_client.SECTION_OWNERS` among its `failed_sections`, findings item
    41)? What it wrote of them is then incomplete, and a rule that reads "no
    statement names this holder" off the graph must wait (findings item 156)."""
    from app.scraper.sec_client import SECTION_OWNERS
    return SECTION_OWNERS in (data.get("failed_sections") or [])


def _write_sec_edgar(company_name: str, data: dict, filer_country: str | None) -> dict:
    """The write phase of `run_scrape_sec_edgar`: the filer, its holders, its
    insiders and officers, from the scraped `data`."""
    from app.scraper.sec_filer import fetch_filer_headquarters, fetch_filer_website
    source_id = runner._ensure_source(runner.SEC_EDGAR_SOURCE_NAME, runner.SEC_EDGAR_SOURCE_URL, runner.SEC_EDGAR_CREDIBILITY)
    scraped: list[dict] = []

    # Upsert the target company
    target_id = runner._upsert_entity_by_name(
        name=data["name"],
        entity_type="company",
        cik=data.get("cik"),
        source_id=source_id,
        register_name=True,                 # EDGAR's own name for this CIK
        former_names=data.get("former_names"),
        lei=data.get("lei"),
        country=filer_country,
        # Where it is RUN, from the same cached document — kept apart from
        # `country`, which is where it is registered.
        headquarters=fetch_filer_headquarters(data["cik"]) if data.get("cik") else None,
        website=fetch_filer_website(data["cik"]) if data.get("cik") else None,
    )
    scraped.append({"type": "entity", "name": data["name"], "role": "target"})
    if data.get("lei_source_url"):
        # the filing that tied the CIK to the node's LEI (`_confirm_stored_lei`)
        with runner.db.get_session() as session:
            session.run("MATCH (e:Entity {id: $id}) "
                        "SET e.lei_source_url = COALESCE(e.lei_source_url, $u)",
                        id=target_id, u=data["lei_source_url"])
    _record_sec_industry(target_id, data.get("cik"), source_id)

    # Ownership filings → investor nodes + OWNS edges
    # CUSIP from the first schedule that states one — fill-if-missing, so a
    # hand-set or 13F-adopted value is never clobbered. This is what lets
    # `sec-13f` match info-table rows exactly instead of by name.
    _keep_cover_cusip(target_id, next((f.get("issuer_cusip") for f in data.get("ownership_filings", [])
                                       if f.get("issuer_cusip")), None))
    _keep_ads_cusips(target_id, data.get("ads_cusips"))

    _write_ownership_filings(target_id, data["name"], data.get("ownership_filings", []), source_id, scraped)

    # Holdings → OWNS edges pointing OUT of this company.
    #
    # The mirror of the ownership_filings loop above. That one reads 13D/13G
    # filings naming this company as the subject (who owns it); this reads the
    # ones it FILES about others (what it owns). An asset manager has no filings
    # about itself — Vanguard is privately held and isn't a listed issuer — so
    # without this its node stays empty no matter how often it is scraped.
    held_subjects: list[str] = []
    for holding in data.get("holdings", []):
        subject_name = (holding.get("subject_name") or "").strip()
        if not subject_name:
            continue
        subject_id = runner._upsert_entity_by_name(
            name=subject_name,
            entity_type="company",
            cik=holding.get("subject_cik"),
            source_id=source_id,
        )
        scraped.append({"type": "entity", "name": subject_name, "role": "holding"})
        _keep_cover_cusip(subject_id, holding.get("subject_cusip"))
        if subject_id:
            held_subjects.append(subject_id)
        runner._upsert_owns_sec(
            owner_id=target_id,
            owned_id=subject_id,
            source_id=source_id,
            read_from=holding.get("read_from"),
            ownership_type=holding.get("ownership_type", "minority"),
            file_date=holding.get("file_date"),
            stake_percent=holding.get("stake_percent"),
            # the bloc a group member votes within (item 98) — run_sec_holdings,
            # the other writer of these rows, always passed it
            voting_power_pct=holding.get("voting_power_pct"),
            # the class the percentage is of (findings item 157) — read on the
            # filer side since then, and passed by both writers of these rows
            share_class=holding.get("share_class"),
            source_url=holding.get("source_url"),
            event_date=holding.get("event_date"),
            since_date=holding.get("since"), since_basis=holding.get("since_basis"),
            # the rulebook behind the figure ("13G/A") — run_sec_holdings, the
            # other writer of the same filer-side holdings, always passed it
            filing_type=holding.get("filing_type"),
            # Set when a later amendment reported 0% — the filer has dropped
            # below the 5% threshold, so this is history, not a live position.
            until=holding.get("until"),
            final_report=holding.get("final_report"),    # the filer's last report (item 35)
        )
        runner.log.info("SEC EDGAR: wrote OWNS %r → %r (%s%%%s)",
                 data["name"], subject_name, holding.get("stake_percent"),
                 f", ended {holding['until']}" if holding.get("until") else "")

    # A subsidiary's 13D/G that its parent's newer one replaces ends here
    # (findings item 34): this company's owners, and the companies it reports
    # — the parent's filing and the subsidiary's can each be read last.
    from app.scraper.sec_writer import close_reported_by_parent
    for owned in dict.fromkeys([target_id, *held_subjects]):
        close_reported_by_parent(owned_id=owned)
    close_reported_by_parent(parent_id=target_id)

    # The holdings in this company whose shares ceased to exist — a merger's
    # 25-NSE, a takeover's Form 15 — and those its own ownership table no
    # longer names (findings item 99). Its own list only: the companies it
    # reports are the whole-graph passes' (`close-ended-classes`,
    # `close-absent-from-table`).
    table_lost = 0
    if data.get("cik"):
        try:
            from app.scraper.sec_class_end import class_ends
            from app.scraper.sec_client import _submissions
            from app.scraper.sec_writer import close_at_class_end, read_table_and_close
            subs = _submissions(data["cik"])
            close_at_class_end(target_id, class_ends(data["cik"], subs))
            # ...and writes the 5 % holders its table states that no
            # statement covers (findings item 86). Not when this scrape lost
            # its 13D/G owners to the network (findings item 156): the table
            # step takes "no statement covers this holder" from the statements
            # just written, and with none read every holder of the table looks
            # uncovered — Morgan Stanley's partial scrape wrote "BlackRock,
            # Inc." from the proxy beside the 13G/A the retry then read from
            # BlackRock Finance, SmartStop's three table holders left their
            # DEF 14A claims behind. The retry of the partial company, or the
            # whole-graph pass, which reads the statements itself, does it.
            if _owners_were_lost(data):
                runner.log.warning("SEC EDGAR: %r's ownership table not applied — its 13D/G owners were lost "
                                   "to the network, so no holder could be told covered", data["name"])
            else:
                table = read_table_and_close(target_id, data["cik"], subs, write=True, source_id=source_id)
                if table.get("written"):
                    runner.log.info("SEC EDGAR: %r's ownership table: %s", data["name"], table["written"])
                # a holder's name search lost to the network: the holder is not
                # written, and the run says so instead of ending `ok` (item 155)
                table_lost = (table.get("written") or {}).get("lost") or 0
        except Exception as exc:  # noqa: BLE001 — the scrape stands without it
            runner.log.warning("SEC EDGAR: class ends / ownership table of %r not read: %s", data["name"], exc)

    # Executives → Person nodes + HAS_ROLE edges
    role_holders: dict[str, str] = {}      # name → person id, for the departures below
    for exec_rec in data.get("executives", []):
        name = exec_rec.get("name", "").strip()
        role = exec_rec.get("role", "Executive")
        if not name:
            continue

        # A 10% owner that files Form 4 is an ENTITY, not an officer — Berkshire
        # Hathaway Inc files Form 4s about the companies it holds. Record its
        # holding as an entity owner; never mint a Person or a director role for
        # a company. (The role writers below assume a natural person.) A CIK an
        # Entity already holds is a company too, whatever the name looks like.
        if runner.has_entity_suffix(name) or _vertex_holding_cik(exec_rec.get("person_cik")) == "Entity":
            shares = exec_rec.get("shares_owned")
            if shares and shares > 0:
                ent_id = runner._upsert_entity_by_name(name=name, entity_type="company",
                                                cik=exec_rec.get("person_cik"),
                                                source_id=source_id)
                if runner.stated_by_13dg(ent_id, target_id, source_id):
                    _note_form4_yields(scraped, name, data["name"])
                    continue
                stake = exec_rec.get("stake_percent")
                runner._upsert_owns_sec(
                    owner_id=ent_id, owned_id=target_id, source_id=source_id,
                    ownership_type=(runner.derive_ownership_type(stake) if stake is not None
                                    else "minority"),
                    file_date=exec_rec.get("source_date"), stake_percent=stake,
                    # the class the count is of, and the count of THAT class it
                    # was measured against (none when the classes did not match)
                    shares=shares, share_class=exec_rec.get("share_class"),
                    shares_outstanding=_denominator_of(exec_rec, data),
                    denominator_date=exec_rec.get("denominator_date"),
                    filing_type="Form 4", read_from=runner.READ_FIELD,
                    # a Form 3/4 states what is held AS OF the report, not since
                    # when: its date written as a start hid an insider's older
                    # holding before their latest trade
                    filing_dates_the_stake=False, source_url=exec_rec.get("source_url"),
                    owner_label="Entity")
                scraped.append({"type": "owns", "name": name, "role": "insider owner"})
            continue

        if exec_rec.get("former"):
            # The insider's newest filing says "Former …": close the seat. When
            # there is none to close — a database built after the departure —
            # write it closed: the person's own filing states the seat and its
            # end, their Form 3 its start (findings item 74, the user's option
            # (a) of 2026-11-07). Before, the rule was "never mint someone to
            # close", and Verra Mobility's CFO Craig Conti, whose "Former Chief
            # Financial Officer" Form 4 came a day before the rebuild, was in
            # the old database with a closed seat and absent from the new one.
            if not exec_rec.get("until"):
                continue
            person_id = _find_person(name, sec_cik=exec_rec.get("person_cik"))
            closed = runner._close_role_sec(person_id, target_id, exec_rec["until"], role=role,
                                     source_id=source_id,
                                     source_url=exec_rec.get("source_url"),
                                     source_date=exec_rec.get("source_date"),
                                     filing_type=runner.form_label(exec_rec.get("document_type")),
                                     read_from=runner.READ_FIELD) if person_id else 0
            if not closed:
                person_id = person_id or runner._upsert_person_by_name(name, source_id=source_id,
                                                                sec_cik=exec_rec.get("person_cik"))
                runner._upsert_role_sec(person_id, target_id, role, source_id,
                                 source_url=exec_rec.get("source_url"),
                                 source_date=exec_rec.get("source_date"),
                                 since=exec_rec.get("since"), until=exec_rec["until"],
                                 filing_type=runner.form_label(exec_rec.get("document_type")),
                                 read_from=runner.READ_FIELD)
            scraped.append({"type": "person", "name": name, "role": f"former {role}"})
            runner.log.info("SEC EDGAR: %s HAS_ROLE %r → %r (%s, until %s)", "closed" if closed else "wrote closed",
                     name, data["name"], role, exec_rec["until"])
            continue

        person_id = runner._upsert_person_by_name(name, source_id=source_id,
                                           sec_cik=exec_rec.get("person_cik"))
        runner._upsert_role_sec(person_id, target_id, role, source_id,
                         source_url=exec_rec.get("source_url"),
                         source_date=exec_rec.get("source_date"),
                         since=exec_rec.get("since"),
                         filing_type=runner.form_label(exec_rec.get("document_type")),
                         read_from=runner.READ_FIELD)
        role_holders[name] = person_id
        scraped.append({"type": "person", "name": name, "role": role})
        runner.log.info("SEC EDGAR: wrote HAS_ROLE %r → %r (%s%s)", name, data["name"], role,
                 f", since {exec_rec['since']}" if exec_rec.get("since") else "")

        # Insider (Form 4) holding → OWNS edge, so a founder/exec who holds
        # shares also shows as an owner. stake_percent is set when the issuer's
        # shares outstanding were readable; else it's a minority holding.
        shares = exec_rec.get("shares_owned")
        if shares and shares > 0:
            if runner.stated_by_13dg(person_id, target_id, source_id):
                _note_form4_yields(scraped, name, data["name"])
                continue
            stake = exec_rec.get("stake_percent")
            runner._upsert_owns_sec(
                owner_id=person_id,
                owned_id=target_id,
                source_id=source_id,
                ownership_type=(runner.derive_ownership_type(stake) if stake is not None else "minority"),
                file_date=exec_rec.get("source_date"),
                stake_percent=stake, read_from=runner.READ_FIELD,
                # Form 4 states the holding exactly; until now it decided
                # whether to write an edge and was then thrown away.
                shares=shares, share_class=exec_rec.get("share_class"),
                # Stamp the denominator too — the count of the holding's own
                # class — so a holding below the percentage precision floor (a
                # director's 1,139 shares → null stake) can still be sized and
                # filtered client-side rather than surfacing under every stake
                # filter as an unquantified owner.
                shares_outstanding=_denominator_of(exec_rec, data),
                denominator_date=exec_rec.get("denominator_date"),
                filing_type="Form 4",
                # a Form 3/4 states what is held AS OF the report, not since
                # when: its date written as a start hid an insider's older
                # holding before their latest trade
                filing_dates_the_stake=False,
                source_url=exec_rec.get("source_url"),
                owner_label="Person",
            )
            scraped.append({"type": "owns", "name": name, "role": "insider owner"})
            runner.log.info("SEC EDGAR: wrote insider OWNS %r → %r (%s shares)", name, data["name"], shares)

    _write_shared_vehicles(target_id, data, source_id, scraped, role_holders)

    # The seats' own filings: departures and personal Form 4s for the people
    # with a seat at the company — this scan's and the graph's.
    asked = _seat_holders_pass(target_id, data.get("cik"), data["name"],
                               # dated and per class; the bare total only for a scrape result that predates it
                               data.get("shares_outstanding_detail") or data.get("shares_outstanding"),
                               source_id, scraped, role_holders)

    runner.log.info(
        "SEC EDGAR runner: finished %r — %d nodes written",
        company_name, len(scraped),
    )
    # Mark the target company as on-demand scraped. SEC is depth-blind, so stamp depth 0
    # (the freshness stamp uses max(), so this never lowers a deeper Wikidata pass).
    # A section lost to the network (findings item 41) makes the run `partial`: what
    # did arrive is written, and the company is scraped again after the cooldown.
    failed = list(data.get("failed_sections") or [])
    if table_lost:
        from app.scraper.sec_client import SECTION_TABLE
        failed.append(SECTION_TABLE)
    if failed:
        runner.log.error("SEC EDGAR runner: %r is partial, lost to the network: %s",
                  company_name, ", ".join(failed))
        runner.mark_scrape_incomplete(target_id, 0)
    elif target_id:
        runner.set_scrape_target(target_id, 0)

    return {
        "status":    "partial" if failed else "ok",
        "failed_sections": failed,
        "company":   company_name,
        "cik":       data.get("cik"),
        "entity_id": target_id,
        "total":     len(scraped),
        "scraped":   scraped,
        "seats_asked": asked,       # the seat holders whose own Form 4s this run looked up (item 67)
    }


def _form4_vehicles(target_id: str) -> list[dict]:
    """The vehicles written for this issuer from its insiders' Form 3/4s
    (`_write_shared_vehicles`): {id, name, shares, share_class} of each open
    Form 4 edge an Entity without a register id holds into it."""
    with runner.db.get_session() as session:
        return [{k: r.get(k) for k in ("id", "name", "shares", "share_class")} for r in session.run(
            """MATCH (v:Entity)-[r:OWNS]->(t:Entity {id: $id})
               WHERE r.until IS NULL AND r.filing_type = 'Form 4' AND v.sec_cik IS NULL AND v.lei_id IS NULL
               RETURN v.id AS id, v.name AS name, r.shares AS shares, r.share_class AS share_class""",
            id=target_id)]


def _write_shared_vehicles(target_id: str, data: dict, source_id: str, scraped: list[dict],
                           role_holders: dict[str, str]) -> None:
    """The positions several insiders report alike (`sec_insiders.split_shared_blocks`),
    each written once, from a node of the holder the filers name, to the
    issuer (findings item 111, the user's choice of 2026-11-12: the vehicle is
    the owner). The node is found among this issuer's vehicles only, never by
    its name elsewhere: "GFI" is how the Slim family's Form 3s name Grupo
    Financiero Inbursa, and a name search would hand it any GFI. An insider
    whose filing held nothing else keeps no Form 4 edge of their own — the
    vehicle's edge carries what it said."""
    from app.scraper.sec_writer import detach_owns_sec
    vehicles = data.get("shared_vehicles") or []
    if not vehicles:
        return
    known = {(v.get("name") or "").casefold(): v["id"] for v in _form4_vehicles(target_id)}
    for v in vehicles:
        vid = runner._upsert_entity_by_name(name=v["name"], entity_type="company", source_id=source_id,
                                     candidates=[known[k]] if (k := v["name"].casefold()) in known else None,
                                     by_name=False)
        stake = v.get("stake_percent")
        runner._upsert_owns_sec(
            owner_id=vid, owned_id=target_id, source_id=source_id,
            ownership_type=(runner.derive_ownership_type(stake) if stake is not None else "minority"),
            file_date=v.get("source_date"), stake_percent=stake,
            shares=v.get("shares_owned") or v["shares"], share_class=v.get("share_class") or v.get("title"),
            shares_outstanding=v.get("shares_outstanding"), denominator_date=v.get("denominator_date"),
            filing_type="Form 4", read_from=runner.READ_FIELD, filing_dates_the_stake=False,
            source_url=v.get("source_url"), owner_label="Entity")
        scraped.append({"type": "owns", "name": v["name"], "role": "insiders' shared vehicle"})
        runner.log.info("SEC EDGAR: wrote OWNS %r → %r (%s shares, reported by %s)", v["name"], data["name"],
                 v["shares"], ", ".join(v.get("reporters") or []))
    for ex in data.get("executives") or []:
        if ex.get("shares_owned") or not ex.get("blocks"):
            continue
        # a company that reports a fund of its group (Innovex's Amberjack funds)
        # by its CIK; a person by the seat this run wrote
        if _vertex_holding_cik(ex.get("person_cik")) == "Entity":
            with runner.db.get_session() as session:
                owner = runner.resolve_entity_id(session, sec_cik=runner.normalize_cik(ex["person_cik"]))
            label = "Entity"
        else:
            owner, label = role_holders.get((ex.get("name") or "").strip()), "Person"
        if owner:
            detach_owns_sec(owner, target_id, source_id, filing_type="Form 4", owner_label=label)


def _seat_holders_pass(target_id: str, cik: str | None, company_name: str, shares_out,
                       source_id: str, scraped: list[dict], role_holders: dict[str, str] | None = None) -> list[str]:
    """What the seats at ``target_id`` say for themselves on EDGAR — the people
    with an open HAS_ROLE to the company, this scan's (``role_holders``) and the
    graph's (founders, executives, directors a knowledge base wrote). Returns
    the ids of the persons whose own Form 4s were looked up (``seats_asked`` in
    the SEC result), so a dispatcher can see which seats arrived after.

    Person-centric insider ownership: for those who don't yet have an ownership
    edge, read THEIR own Form 4s. This reaches insiders the issuer-side scan
    misses, e.g. a founder-CEO whose filings are flooded out of the company's
    recent window (Larry Fink at BlackRock). Departures — the seats EDGAR never
    closes on its own: a departing insider files nothing; the company announces
    it in an 8-K, Item 5.02, read for the people we list, never to mint anyone
    (`sec_insiders.fetch_departures`).

    Run by `_write_sec_edgar` at the end of a SEC scrape, and once more by
    `run_scrape_all` when a source after the SEC's added seats (findings item
    67): with the registers running before the knowledge bases (item 60) the
    SEC part met no Wikidata officers yet, and 22 of them lost their Form 4
    lookups, their CIKs and their item ids in the 2026-11-06 rebuild."""
    from app.scraper.sec_insiders import fetch_departures, fetch_insider_holding, without_vehicles
    known = dict(role_holders or {})
    with runner.db.get_session() as session:
        for r in session.run(
                """MATCH (p:Person)-[r:HAS_ROLE]->(e:Entity {id: $id})
                   WHERE r.until IS NULL RETURN DISTINCT p.id AS id, p.full_name AS name""",
                id=target_id):
            if r.get("name"):
                known.setdefault(r["name"], r["id"])
    if cik and known:
        for dep in fetch_departures(cik, list(known)):
            closed = runner._close_role_sec(known[dep["name"]], target_id, dep["until"],
                                     role=dep.get("role"), source_id=source_id,
                                     source_url=dep.get("source_url"),
                                     source_date=dep.get("source_date"),
                                     filing_type="8-K",
                                     read_from=runner.READ_NARRATIVE)  # Item 5.02's sentences
            if closed:
                scraped.append({"type": "person", "name": dep["name"],
                                "role": f"departed {dep.get('role') or ''}".strip()})
                runner.log.info("SEC EDGAR: 8-K Item 5.02 closed %d seat(s) of %r at %r (until %s)",
                         closed, dep["name"], company_name, dep["until"])

    asked: list[str] = []
    if cik:
        known = _seats_to_ask(target_id)
        vehicles: list[dict] | None = None
        for row in known:
            pname, pid = row["name"], row["id"]
            if not pname or not pid:
                continue
            asked.append(pid)
            holding = fetch_insider_holding(pname, cik, shares_out, person_cik=row.get("cik"))
            _mark_seat_asked(pid, target_id)
            if holding and (vehicles := vehicles if vehicles is not None else _form4_vehicles(target_id)):
                # a shared vehicle is the vehicle's, not this person's (item 111)
                holding = without_vehicles(holding, vehicles, shares_out)
            if not holding:
                continue
            if holding.get("cik") and not row.get("cik"):
                pid = _holder_of_found_cik(pid, holding["cik"])
                if pid is None:          # a company's CIK: not this person's filing
                    continue
            if runner.stated_by_13dg(pid, target_id, source_id):
                _note_form4_yields(scraped, pname, company_name)
                continue
            stake = holding.get("stake_percent")
            runner._upsert_owns_sec(
                owner_id=pid, owned_id=target_id, source_id=source_id,
                ownership_type=(runner.derive_ownership_type(stake) if stake is not None else "minority"),
                file_date=holding.get("source_date"),
                stake_percent=stake,
                shares=holding.get("shares_owned"), share_class=holding.get("share_class"),
                shares_outstanding=holding.get("shares_outstanding"),
                denominator_date=holding.get("denominator_date"),
                filing_type="Form 4", read_from=runner.READ_FIELD,
                # a Form 3/4 states what is held AS OF the report, not since
                # when: its date written as a start hid an insider's older
                # holding before their latest trade
                filing_dates_the_stake=False,
                source_url=holding.get("source_url"),
                owner_label="Person",
            )
            scraped.append({"type": "owns", "name": pname, "role": "insider owner"})
            runner.log.info("SEC EDGAR: person-centric insider OWNS %r → %r (%s shares)",
                     pname, company_name, holding.get("shares_owned"))
    return asked


def _seats_to_ask(target_id: str) -> list[dict]:
    """The seat holders `_seat_holders_pass` asks EDGAR about on this run:
    [{id, name, cik}], `MAX_INSIDER_LOOKUPS` at most.

    Each person once, only seats still held: a person with two seats took two
    of the slots, and the cap cut a different set on every run (findings item
    29). Only the living: a founder's role stays open after the death
    (`roles.end_seats_at_death`), and the newest Form 4 of a dead person is not
    what they hold — Steve Jobs († 2011) got his Form 4 of 2007 as an open
    Apple stake once his name resolved to a CIK (findings item 149).

    In turn (findings item 154): those never asked first, then those asked
    longest ago (`form4_asked_at` on the seat, `_mark_seat_asked`), by id within
    a tie. Ordered by id alone, a person asked with nothing found — no Form 4
    of their own, as most directors a knowledge base names — kept the slot on
    every run, and at a company with more than fifteen such seats whoever
    sorted behind them was never asked: 11 of 54 passes of the 2026-11-23
    rebuild asked exactly fifteen, Bank of America's and Morgan Stanley's among
    them. Sorted here, not by the database: a missing property has no place in
    its order that every version agrees on."""
    seats: dict[str, dict] = {}
    with runner.db.get_session() as session:
        for r in session.run(
                """
                MATCH (p:Person)-[r:HAS_ROLE]->(e:Entity {id: $id})
                WHERE r.until IS NULL AND p.death_date IS NULL AND NOT (p)-[:OWNS]->(e)
                RETURN p.id AS id, p.full_name AS name, p.sec_cik AS cik, r.form4_asked_at AS asked
                """,
                id=target_id):
            if not r.get("id"):
                continue
            seat = seats.setdefault(r["id"], {"id": r["id"], "name": r.get("name"), "cik": r.get("cik"), "asked": ""})
            # asked on any of the person's seats here is asked: the newest counts
            seat["asked"] = max(seat["asked"], str(r.get("asked") or ""))
    turn = sorted(seats.values(), key=lambda s: (s["asked"], s["id"]))[:runner.MAX_INSIDER_LOOKUPS]
    return [{"id": s["id"], "name": s["name"], "cik": s["cik"]} for s in turn]


def _mark_seat_asked(person_id: str, target_id: str) -> None:
    """EDGAR was asked for this person's own Form 4s at the company: the time
    goes on their open seats there, so the next run asks somebody else first
    (`_seats_to_ask`, findings item 154). On the seat, not the person — it is
    about this company's filings. A holding found is an OWNS edge and takes
    the person out of the pass by itself; one not found is asked again when
    its turn comes round, since a director may file a first Form 4 later."""
    with runner.db.get_session() as session:
        session.run(
            """MATCH (p:Person {id: $p})-[r:HAS_ROLE]->(e:Entity {id: $e})
               WHERE r.until IS NULL SET r.form4_asked_at = $now""",
            p=person_id, e=target_id, now=datetime.now(timezone.utc).isoformat())


def seat_holders_without_holdings(entity_id: str | None) -> frozenset[str]:
    """The living people holding an open seat at ``entity_id`` with no OWNS edge
    to it — the ones `_seat_holders_pass` looks up on EDGAR; empty for no node."""
    if not entity_id:
        return frozenset()
    with runner.db.get_session() as session:
        return frozenset(r["id"] for r in session.run(
            """MATCH (p:Person)-[r:HAS_ROLE]->(e:Entity {id: $id})
               WHERE r.until IS NULL AND p.death_date IS NULL AND NOT (p)-[:OWNS]->(e)
               RETURN DISTINCT p.id AS id""",
            id=entity_id) if r.get("id"))


def _node_id_now(entity_id: str) -> str:
    """The id ``entity_id`` lives under now: itself while the node exists, else
    the id its forwarding row names (`MergedId`). A run-all's SEC result keeps
    the id its own run wrote; a later source in the same run-all can re-key that
    node — Wikidata states the filer's LEI and `node_ids` renames `sec:{CIK}` to
    `lei:{LEI}` (rebuild 2026-11-07b: BofA, NYT, Wells Fargo, Morgan Stanley) —
    and the old id then reads as a node with no seats. Read uncached: the rename
    happened seconds ago in this process."""
    from app.merged_ids import resolve_current_id
    with runner.db.get_session() as session:
        if session.run("MATCH (e:Entity {id: $id}) RETURN e.id AS id LIMIT 1", id=entity_id).single():
            return entity_id
        return resolve_current_id(session, entity_id) or entity_id


def seat_rerun_target(result) -> str | None:
    """The filer node the SEC's seat-holders pass runs on once more after the
    other sources, or None: its run wrote a filer with a CIK, and that filer —
    under the id it has NOW (`_node_id_now`, findings item 67 b) — has seat
    holders without a holding the run did not look up (``seats_asked``):
    officers a knowledge base wrote after it (findings item 67). The same seats
    mean the same lookups again, so no second ask; a result without
    ``seats_asked`` (an older shape) asks nothing."""
    if not isinstance(result, dict) or result.get("status") not in ("ok", "partial"):
        return None
    if not (result.get("cik") and result.get("entity_id")) or not isinstance(result.get("seats_asked"), list):
        return None
    current = _node_id_now(result["entity_id"])
    return current if seat_holders_without_holdings(current) - set(result["seats_asked"]) else None


def rerun_for_new_seats(result) -> bool:
    """Whether `seat_rerun_target` names a filer to ask once more."""
    return seat_rerun_target(result) is not None


@runner._with_autodedup
def run_sec_seat_holders(entity_id: str, cik: str, company_name: str) -> dict:
    """The seat-holders pass on its own (`_seat_holders_pass`), for a filer the
    SEC scrape already wrote: the issuer's share count comes from the cached
    XBRL fetch, no filing is re-read. Returns what it wrote."""
    from dataclasses import asdict

    from app.scraper.sec_shares import fetch_shares_outstanding_detail
    source_id = runner._ensure_source(runner.SEC_EDGAR_SOURCE_NAME, runner.SEC_EDGAR_SOURCE_URL, runner.SEC_EDGAR_CREDIBILITY)
    detail = fetch_shares_outstanding_detail(cik)
    scraped: list[dict] = []
    asked = _seat_holders_pass(entity_id, cik, company_name, asdict(detail) if detail else None, source_id, scraped)
    runner.log.info("SEC EDGAR: seat holders of %r looked up once more after the other sources — %d written",
             company_name, len(scraped))
    return {"status": "ok", "company": company_name, "entity_id": entity_id, "total": len(scraped),
            "scraped": scraped, "seats_asked": asked}
