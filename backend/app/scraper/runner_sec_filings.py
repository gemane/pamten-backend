"""
Scraper runner — the SEC pipelines beside the company scrape, each its own
manual command: Exhibit 21 subsidiaries and their history, acquisitions, the
proxy statement, Form D, 13F holders, and a filer's own holdings.

What this module needs of the runner — its own helpers, and the app-level names
it imports (the database, the settings, the write layer) — it reaches through
`runner` (`runner.db`, `runner._upsert_role`), not through imports of its own:
a test that replaces one of them on the runner then covers every pipeline, as
it did while this code was part of runner.py (until 2026-11-25).
"""
import functools
import re
from datetime import datetime, timezone

from app.scraper import runner, runner_sec


# ── SEC EDGAR helpers ─────────────────────────────────────────────────────────


# ── SEC EDGAR public entry point ──────────────────────────────────────────────

def _upsert_affiliate(filer_id: str, affiliate_id: str, source_id: str,
                      source_url: str | None = None, source_date: str | None = None):
    """Record that two entities belong to the same fund group.

    RELATED_TO carries a free-text `relation`, which is why it fits: the edge says
    exactly what the filing says and nothing more. Deliberately NOT an OWNS edge —
    a 13F cover page naming another manager establishes group membership, not
    ownership or control, and writing it as ownership would invent a fact.

    MERGE on (relation) so re-scraping refreshes provenance instead of stacking
    duplicate edges.
    """
    now = datetime.now(timezone.utc).isoformat()
    with runner.db.get_session() as session:
        session.run(
            """
            MATCH (a:Entity {id: $aid}), (b:Entity {id: $bid})
            MERGE (a)-[r:RELATED_TO {relation: 'affiliate'}]->(b)
            SET r.source_id       = COALESCE(r.source_id, $sid),
                r.source_url      = COALESCE($surl, r.source_url),
                r.source_date     = COALESCE($sdate, r.source_date),
                r.last_scraped_at = $now
            """,
            aid=filer_id, bid=affiliate_id, sid=source_id,
            surl=source_url, sdate=source_date, now=now,
        )


def _write_affiliates(filer_id: str, affiliates: list[dict], source_id: str) -> int:
    """Create the entity nodes for a filer's affiliated managers and link them."""
    written = 0
    for aff in affiliates or []:
        name = (aff.get("name") or "").strip()
        if not name:
            continue
        affiliate_id = runner._upsert_entity_by_name(name=name, entity_type="company",
                                              cik=aff.get("cik"), source_id=source_id)
        if affiliate_id == filer_id:
            continue                      # a filer listing itself
        _upsert_affiliate(filer_id, affiliate_id, source_id,
                          source_url=aff.get("source_url"), source_date=aff.get("source_date"))
        written += 1
    return written


def _names_the_parent(co_holder: str, parent: str | None) -> bool:
    """A co-holder named like the row's own tree parent, give or take the plural
    of "Holding(s)": Chubb's Indonesian row names "Chubb INA International
    Holding Ltd." — its parent row reads "… Holdings Ltd." (findings item 100).
    Only against the parent the tree already gives the row."""
    def plain(x):
        return re.sub(r"\bholdings\b", "holding", x or "", flags=re.I)
    return bool(parent) and _same_filer(plain(co_holder), plain(parent))


def _same_filer(a: str | None, b: str | None) -> bool:
    """The filer under another spelling ("Chubb Limited" vs "CHUBB LIMITED",
    "Inter & Co, Inc." vs "Inter&Co, Inc"): legal forms and punctuation dropped."""
    from app.scraper.mapper import normalize_entity_name
    def key(x): return re.sub(r"[^a-z0-9]", "", normalize_entity_name(x))
    return bool(a and b) and key(a) == key(b)


def _one_filer_record(fn):
    """The company's filer record is requested once for the whole call
    (`sec_client.one_filer_record_per_scrape`): reading a subsidiary list asked
    for the filer's own record three times — once to find the filing, again
    for its name and its address (32 requests for 11 filers, measured
    2026-11-24; findings item 135)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from app.scraper.sec_client import one_filer_record_per_scrape
        with one_filer_record_per_scrape():
            return fn(*args, **kwargs)
    return wrapper


@runner._with_autodedup
@_one_filer_record
def run_sec_ex21(company: str, force: bool = False) -> dict:
    """Ingest one issuer's statutory subsidiary list from its latest annual
    filing's Exhibit 21 (10-K) or Exhibit 8.1 (20-F).

    The statutory replacement for the role Wikidata's community subsidiary
    lists played before claims-only: name + jurisdiction per subsidiary,
    straight from the annual report. Enriches, does not discover — the
    company must already be in the graph with a CIK (run the SEC EDGAR
    scrape first, like 13F).

    Honesty rules carried onto the data:
    - filers list only SIGNIFICANT subsidiaries; absence proves nothing.
    - the exhibit states existence + jurisdiction, rarely a stake — edges are
      ownership_type "controlling" with no invented percentage; a stated one
      (an ownership column, "(58%)" in the name) is stored. A cell naming
      co-holders ("66.66% 33.33% (Chubb Bermuda Insurance Ltd.)") draws each
      co-holder's stake too, when the co-holder is itself on the list or is
      the filer — a name is never looked up in the wider graph for this.
    - where the filer SHOWS the group tree — an indentation tree (Chubb,
      Eversource, NYT), a "Subsidiaries of X" heading (Tenet's USPI section)
      — the subsidiary is drawn under that parent, marked direct with the
      layout basis, and NOT under the filer: a filer→subsidiary edge an
      earlier flat read drew is withdrawn (`detach_owns_sec`) rather than
      kept as an "indirect" holding. A parent the list does not itself name
      (Altaba's "Yahoo! Inc.") is ignored and counted. A flat list says
      nothing about depth and gets no marker.
    - subsidiaries come without hard ids; they are resolved by name first
      (an existing GLEIF/PSC node wins), created with their registered
      country otherwise. Legal names + jurisdiction make them better dedup
      keys than Wikidata's labels ever were.

    A company with no 10-K or 20-F yet — listed within the last year — is read
    from its IPO's S-1 / F-1 Exhibit 21.1 (findings item 77); its first annual
    filing replaces that list and dims what it no longer names.

    Freshness gates on the filing itself: one annual filing = one ingest,
    re-run refetches only the (cheap) metadata until a NEWER filing appears.
    `force` re-reads regardless."""
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true to enable.")
    if not runner.settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError("SEC EDGAR scraper is disabled. "
                              "Set SCRAPER_SEC_EDGAR_ENABLED=true to enable.")

    from app.routers.search import resolve_best_entity
    from app.scraper.run_log import record_run
    from app.scraper.sec_filer import fetch_filer_identity
    from app.scraper.sec_ex21 import (_at_home, _is_filer, fetch_subsidiaries, jurisdiction_country,
                                       jurisdiction_subdivision, own_names)
    from app.scraper.sec_writer import detach_owns_sec

    entity = resolve_best_entity(company, None)
    if not entity:
        return {"status": "no_results", "company": company, "total": 0, "scraped": []}
    company_id = entity["id"]
    if not entity.get("sec_cik"):
        return {"status": "needs_sec_scrape", "company": company,
                "entity_id": company_id, "total": 0,
                "detail": "The entity has no SEC CIK yet — run the SEC EDGAR "
                          "scrape (13D/G + Form 4) first."}

    with record_run("sec-ex21", company) as run:
        run["entity_id"] = company_id
        source_id = runner._ensure_source(runner.SEC_EDGAR_SOURCE_NAME, runner.SEC_EDGAR_SOURCE_URL,
                                   runner.SEC_EDGAR_CREDIBILITY)
        # Every name the filer goes by ITSELF: the node's may be GLEIF's or
        # Wikidata's spelling, the list's is the filer's own (NYT's "The New
        # York Times Company", EDGAR's "NEW YORK TIMES CO"). Not the aliases
        # bag — a Wikidata alias can name another company (findings item 58).
        # Where it is registered tells its own row from a namesake abroad.
        edgar_name, home = fetch_filer_identity(entity["sec_cik"])
        home = home or entity.get("jurisdiction_code") or entity.get("country")
        filer_names = own_names(entity, edgar_name)
        data = fetch_subsidiaries(entity["sec_cik"], registrant=filer_names, home=home)
        if not data:
            from app.scraper.sec_ex21 import annual_filings
            if not annual_filings(entity["sec_cik"]):
                # SoftBank, Vanguard, FMR: 13F/13G filers with no 10-K or 20-F
                # at all — "no exhibit" said the filing lacked one
                run["status"], run["note"] = "skipped", "no annual filing"
                return {"status": "no_annual_filing", "company": company,
                        "entity_id": company_id, "total": 0,
                        "detail": "The company files no 10-K or 20-F with the SEC."}
            run["status"], run["note"] = "skipped", "no subsidiary exhibit"
            return {"status": "no_exhibit", "company": company,
                    "entity_id": company_id, "total": 0,
                    "detail": "No subsidiary exhibit in the latest annual filing."}

        # One annual filing = one ingest. The gate key is the exhibit URL (it
        # embeds the accession), stamped only on a completed run.
        with runner.db.get_session() as session:
            row = session.run("MATCH (e:Entity {id: $id}) "
                              "RETURN e.sec_ex21_ingested AS ingested",
                              id=company_id).single()
        if not force and row and row.get("ingested") == data["url"]:
            run["status"], run["note"] = "skipped", "filing already ingested"
            return {"status": "fresh", "company": company, "entity_id": company_id,
                    "total": 0, "filing_date": data["filing_date"],
                    "detail": "This annual filing is already ingested; a newer "
                              "one opens the gate by itself. --force re-reads."}
        if data.get("none_stated"):
            # The filer's own word (findings item 82a): the exhibit says "None."
            # — a fact about the company, not a list the reader missed. The gate
            # closes on this filing as on a list, so the next run is `fresh`.
            with runner.db.get_session() as session:
                session.run("MATCH (e:Entity {id: $id}) SET e.sec_ex21_ingested = $u",
                            id=company_id, u=data["url"])
            run["status"], run["note"] = "skipped", "no subsidiaries stated"
            return {"status": "no_subsidiaries", "company": company, "entity_id": company_id,
                    "total": 0, "filing_date": data["filing_date"], "url": data["url"],
                    "detail": "The latest annual filing states the company has no (significant) subsidiaries."}

        written, skipped_unmapped, co_owner_edges = 0, 0, 0
        nested = unresolved_parents = detached = 0
        scraped: list[dict] = []
        # a 20-F can point to an earlier F-1's Exhibit 21.1 (list_from_earlier_filing)
        filing_type = "EX-21" if data["form"] == "10-K" or data.get("exhibit") == "21" else "EX-8.1"
        basis_of = {"indent": "ex21_indent", "heading": "ex21_heading", "column": "ex21_column"}
        # Nodes first, edges second: a co-holder a cell names is resolved
        # among the LISTED subsidiaries (by the name as filed) or as the filer
        # itself — never looked up in the wider graph, where a name alone
        # could land on a stranger.
        ids: dict[str, str] = {}
        sub_ids: list[str | None] = []
        # A name in two countries is two nodes, one per country (the user's
        # call, 2026-10-08): Perfect Corp.'s Japanese, US and French "Perfect
        # Corp.", but also Ziff Davis' "… Performance Marketing, Inc." in
        # Delaware and the Philippines, which may be one company's branch.
        # The writer refuses the namesake abroad for every row that states a
        # jurisdiction (graph_writer._resolve_by_name). A parent or co-holder
        # named so is the first of them.
        for sub in data["subsidiaries"]:
            sub_ids.append(None)
            if _is_filer(sub["name"], filer_names) and _at_home(sub.get("jurisdiction"), home):
                continue        # the filer's own row, from a reader that kept it
            country = jurisdiction_country(sub["jurisdiction"])
            if country is None:
                skipped_unmapped += 1   # counted, not dropped silently
            # Keep the finer grain the filing stated ("Florida, USA" -> US-FL),
            # so the panel shows "Registered in: Florida" instead of only the
            # country. Sparse by nature — US/CA/GB/AE/KN, else None.
            sub_id = runner._upsert_entity_by_name(
                name=sub["name"], entity_type="company",
                country=country,
                jurisdiction_code=jurisdiction_subdivision(sub["jurisdiction"]),
                source_id=source_id,
                # the row's "f/k/a" and "d/b/a" notes (findings item 80)
                former_names=sub.get("former_names"), aliases=sub.get("aliases"))
            if not sub_id or sub_id == company_id:
                continue
            sub_ids[-1] = sub_id
            ids.setdefault(sub["name"].casefold(), sub_id)
            scraped.append({"id": sub_id, "name": sub["name"],
                            "type": "company", "country": country})

        _READ_OF_BASIS = {"column": runner.READ_TABLE, "indent": runner.READ_LAYOUT, "heading": runner.READ_LAYOUT}

        def owns(owner_id: str, sub_id: str, stake: float | None,
                 ownership_type: str | None = "controlling", **structure) -> None:
            runner._upsert_owns_sec(
                owner_id=owner_id, owned_id=sub_id, source_id=source_id,
                ownership_type=ownership_type, file_date=data["filing_date"],
                # Some filers state it (Astronics: an Ownership Percentage
                # column). Stated → stored; absent → None, never invented.
                stake_percent=stake, filing_type=filing_type,
                # A subsidiary LIST: held as of the filing, not acquired then.
                filing_dates_the_stake=False, source_url=data["url"], **structure)

        holders: set[str] = {company_id}
        for sub, sub_id in zip(data["subsidiaries"], sub_ids):
            if not sub_id:
                continue
            stake = sub.get("stake_percent")
            basis = basis_of.get(sub.get("parent_basis") or "")
            parent_id = ids.get((sub.get("parent") or "").casefold())
            # Who the listed holding hangs off: the intermediate parent the
            # layout shows, else the filer. A parent the list does not carry
            # is not resolved elsewhere — the filer stays the holder.
            holder = parent_id if parent_id and parent_id != sub_id else company_id
            if sub.get("parent") and holder == company_id:
                unresolved_parents += 1
                # the row's stake is its named parent's (Almacenes Éxito's
                # column says so), not the filer's: no stake beats a false one
                stake = None
            # The layout's marker: a resolved parent, or a row the tree puts
            # straight under the filer. A row whose named parent the list does
            # not carry is under SOMEONE else — not direct under the filer.
            structure = ({"direct_or_indirect": "direct", "structure_basis": basis}
                         if basis and (holder != company_id or not sub.get("parent")) else {})
            # The edge is as surely read as its least sure part: a row from a
            # table whose place in the tree came from indentation is a
            # `layout` edge; a parent column is a table cell like the rest.
            grade = runner.weakest_reading(sub.get("read_from"),
                                    _READ_OF_BASIS.get(sub.get("parent_basis") or "") if structure else None)
            for co in sub.get("co_owners") or []:
                co_id = ids.get(co["name"].casefold())
                if co_id is None and _same_filer(co["name"], entity.get("name")):
                    co_id = company_id
                if co_id is None and holder != company_id and _names_the_parent(co["name"], sub.get("parent")):
                    co_id = holder
                if not co_id or co_id == sub_id:
                    continue
                if co_id == holder:
                    # The holder named among the co-holders: its named share is
                    # its stake. "87.99% 12.01% (Chubb Limited)" in a FLAT list —
                    # the first share is an unlisted holder's, the filer's own
                    # line carries 12.01 %; and under a nested parent alike:
                    # Chubb Seguros Colombia sits under Chubb INA International
                    # Holdings, whose share the cell states as 17.0140717 %
                    # (findings item 100: it kept the first, Vigilant's 46.34 %).
                    stake = co["stake_percent"]
                    continue
                # A co-holder is whatever its share makes it — the 0.0000003%
                # nominal second shareholder Mexican law requires is a
                # minority holder, not a controlling one. The writer derives
                # the type from the stake when none is given.
                owns(co_id, sub_id, co["stake_percent"], ownership_type=None,
                     direct_or_indirect="direct", structure_basis="ex21_stated",
                     read_from=sub.get("read_from"))
                co_owner_edges += 1
            owns(holder, sub_id, stake, read_from=grade, **structure)
            holders.add(holder)
            written += 1
            if holder != company_id:
                nested += 1
                if detach_owns_sec(company_id, sub_id, source_id, filing_type) != "none":
                    detached += 1

        # What this list no longer names: dimmed, never closed.
        from app.scraper.sec_writer import mark_ex21_stale
        stale = mark_ex21_stale(holders, data["url"], data["filing_date"])
        # a subsidiary listed now may have filed a 13D/G its parent's newer
        # one replaces — the list can be read after both (findings item 34)
        from app.scraper.sec_writer import close_reported_by_parent
        for holder in sorted(holders):
            close_reported_by_parent(parent_id=holder)
        # a subsidiary listed now may hold an old 13D/G on the filer itself —
        # Texaco's on Chevron (findings item 99 part 3)
        from app.scraper.sec_writer import close_held_by_subsidiary
        close_held_by_subsidiary(issuer_id=company_id)
        with runner.db.get_session() as session:
            session.run("MATCH (e:Entity {id: $id}) SET e.sec_ex21_ingested = $u",
                        id=company_id, u=data["url"])
        run["total"] = written
        notes = [f"{nested} under an intermediate parent" if nested else "",
                 f"{co_owner_edges} co-holder edges" if co_owner_edges else "",
                 f"{detached} filer edges withdrawn" if detached else "",
                 f"{stale} no longer listed (dimmed)" if stale else "",
                 (f"list of {data['filing_date']}, re-affirmed by the 20-F of {data['confirmed_on']}"
                  if data.get("confirmed_by") else ""),
                 # no 10-K / 20-F yet: the IPO's registration statement (findings item 77)
                 (f"list of the {data['form']} of {data['filing_date']}, no annual filing yet"
                  if data.get("registration") else ""),
                 # A filing several registrants file together: the list is
                 # the group's, and only the filer's branch of it was written
                 (f"the filing's list is {data['group_of']}'s; {written} in this filer's branch"
                  if data.get("group_of") else "")]
        if any(notes):
            run["note"] = ", ".join(n for n in notes if n)
        return {"status": "ok", "company": company, "entity_id": company_id,
                "form": data["form"], "filing_date": data["filing_date"],
                "total": written, "unmapped_jurisdictions": skipped_unmapped,
                "nested": nested, "unresolved_parents": unresolved_parents,
                "detached": detached, "co_owner_edges": co_owner_edges,
                "stale": stale, "scraped": scraped,
                **({"group_of": data["group_of"]} if data.get("group_of") else {}),
                **({"registration": True} if data.get("registration") else {}),
                **({"source_url": data["url"], "confirmed_by": data["confirmed_by"],
                    "confirmed_on": data["confirmed_on"]} if data.get("confirmed_by") else {})}


@runner._with_autodedup
def run_sec_acquisitions(company: str, force: bool = False,
                         max_filings: int | None = None) -> dict:
    """Ingest the acquisitions one filer's own 10-K / 10-Q XBRL facts state:
    acquirer → acquired company, with the CLOSING DATE as a stated start and
    the consideration paid (`app.scraper.sec_acquisitions`).

    Enriches, does not discover — the company must be in the graph with a
    CIK (run the SEC EDGAR scrape first, like Exhibit 21). The acquired
    company is resolved by name like an Exhibit 21 row (the filing carries no
    identifier for it; an existing GLEIF/PSC node wins, else it is created
    without a country — XBRL states none).

    Honesty rules carried onto the data:
    - only a consideration-TRANSFERRED fact is a closing; a member the filer
      tags with an agreed or expected price is a pending deal, counted and
      never written (`pending_only`);
    - the filer is the GROUP: its XBRL does not say which group company
      bought. The edge is drawn from the filer, undated in structure, and
      flagged a `shortcut` when the graph already carries a chain from the
      filer to the company (`maintenance.mark_shortcut_for_pair`);
    - the Exhibit 21 row for the same pair keeps the edge's answer; the deal
      adds the date and the price (`sec_writer.write_acquisition_sec`);
    - member names vary between filings: newest filing first, one write per
      normalised name (`repeated` counts the rest).

    Freshness gates on the newest instance read: a newer 10-K / 10-Q opens
    the gate by itself; `force` re-reads (from the cache)."""
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true to enable.")
    if not runner.settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError("SEC EDGAR scraper is disabled. "
                              "Set SCRAPER_SEC_EDGAR_ENABLED=true to enable.")

    from app.routers.search import resolve_best_entity
    from app.scraper.run_log import record_run
    from app.scraper.maintenance import mark_shortcut_for_pair
    from app.scraper.sec_acquisitions import FILING_TYPE, MAX_FILINGS, fetch_acquisitions

    entity = resolve_best_entity(company, None)
    if not entity:
        return {"status": "no_results", "company": company, "total": 0, "scraped": []}
    company_id = entity["id"]
    if not entity.get("sec_cik"):
        return {"status": "needs_sec_scrape", "company": company,
                "entity_id": company_id, "total": 0,
                "detail": "The entity has no SEC CIK yet — run the SEC EDGAR "
                          "scrape (13D/G + Form 4) first."}

    with record_run("sec-acquisitions", company) as run:
        run["entity_id"] = company_id
        source_id = runner._ensure_source(runner.SEC_EDGAR_SOURCE_NAME, runner.SEC_EDGAR_SOURCE_URL,
                                   runner.SEC_EDGAR_CREDIBILITY)
        data = fetch_acquisitions(entity["sec_cik"], max_filings or MAX_FILINGS)
        if not data["filings"]:
            run["status"], run["note"] = "skipped", "no 10-K or 10-Q with XBRL"
            return {"status": "no_filings", "company": company, "entity_id": company_id,
                    "total": 0, "detail": "The company files no 10-K or 10-Q with XBRL "
                                          "financial statements."}
        with runner.db.get_session() as session:
            row = session.run("MATCH (e:Entity {id: $id}) "
                              "RETURN e.sec_acquisitions_ingested AS ingested",
                              id=company_id).single()
        if not force and row and row.get("ingested") == data["newest_url"]:
            run["status"], run["note"] = "skipped", "filings already ingested"
            return {"status": "fresh", "company": company, "entity_id": company_id,
                    "total": 0, "detail": "The newest 10-K / 10-Q is already ingested; a "
                                          "newer one opens the gate by itself. --force re-reads."}

        written = enriched = undated = repeated = shortcuts = 0
        pending_only: set[str] = set()
        skipped_aggregate = 0
        seen: set[str] = set()
        scraped: list[dict] = []
        for filing in data["filings"]:            # newest first: the current statement wins
            pending_only.update(filing["pending_only"])
            skipped_aggregate += filing["skipped_aggregate"]
            for acq in filing["acquisitions"]:
                key = runner.normalize_entity_name(acq["name"])
                if key in seen:
                    repeated += 1
                    continue
                seen.add(key)
                sub_id = runner._upsert_entity_by_name(name=acq["name"], entity_type="company",
                                                source_id=source_id)
                if not sub_id or sub_id == company_id:
                    continue
                outcome = runner.write_acquisition_sec(
                    company_id, sub_id, source_id, closing=acq["closing"],
                    value_usd=acq["value_usd"], file_date=filing["filing_date"],
                    source_url=filing["url"], filing_type=FILING_TYPE[filing["form"]])
                if outcome == "enriched":
                    enriched += 1
                else:
                    written += 1
                if not acq["closing"]:
                    undated += 1
                if mark_shortcut_for_pair(company_id, sub_id):
                    shortcuts += 1
                scraped.append({"id": sub_id, "name": acq["name"], "since": acq["closing"],
                                "value_usd": acq["value_usd"], "concept": acq["concept"],
                                "form": filing["form"], "filing_date": filing["filing_date"],
                                "accession": filing["accession"], "outcome": outcome})
        with runner.db.get_session() as session:
            session.run("MATCH (e:Entity {id: $id}) SET e.sec_acquisitions_ingested = $u",
                        id=company_id, u=data["newest_url"])
        run["total"] = written
        notes = [f"{enriched} already listed (dated and priced)" if enriched else "",
                 f"{undated} without a closing date" if undated else "",
                 f"{shortcuts} redundant with a chain (shortcut)" if shortcuts else "",
                 f"{len(pending_only)} pending, not written" if pending_only else "",
                 f"{skipped_aggregate} aggregate members skipped" if skipped_aggregate else ""]
        if any(notes):
            run["note"] = ", ".join(n for n in notes if n)
        return {"status": "ok", "company": company, "entity_id": company_id,
                "filings": len(data["filings"]), "total": written, "enriched": enriched,
                "undated": undated, "repeated": repeated, "pending_only": len(pending_only),
                "skipped_aggregate": skipped_aggregate, "no_instance": data["no_instance"],
                "shortcuts": shortcuts, "scraped": scraped}


@runner._with_autodedup
@_one_filer_record
def run_sec_ex21_history(company: str, max_filings: int | None = None) -> dict:
    """Date an issuer's subsidiary edges by their OLDEST listing: "owned since
    at least".

    An Exhibit 21 says what is held at year-end, never since when, so the
    subsidiary edges it writes are undated. Reading the company's older annual
    filings (back to ~2001) gives each current subsidiary the first year of its
    unbroken run of listings — dated by the fiscal year-end that list
    describes: a lower bound that is still true (see
    ``sec_ex21.earliest_listing``). A subsidiary listed only this year gets
    this year's year-end: "since 2026 or earlier" is true too. Only moves a
    start date EARLIER, never later.

    Enriches, does not discover: the company needs a CIK and its Exhibit 21
    edges (run ``sec-ex21`` first). About two EDGAR requests per annual filing.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true to enable.")
    if not runner.settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError("SEC EDGAR scraper is disabled. "
                              "Set SCRAPER_SEC_EDGAR_ENABLED=true to enable.")
    from app.routers.search import resolve_best_entity
    from app.scraper.run_log import record_run
    from app.scraper.sec_ex21 import (HISTORY_MAX_FILINGS, earliest_listing,
                                       fetch_subsidiary_history)
    from app.scraper.sec_writer import set_since_lower_bound

    entity = resolve_best_entity(company, None)
    if not entity:
        return {"status": "no_results", "company": company, "total": 0}
    company_id = entity["id"]
    if not entity.get("sec_cik"):
        return {"status": "needs_sec_scrape", "company": company, "entity_id": company_id,
                "total": 0, "detail": "The entity has no SEC CIK yet — run the SEC EDGAR "
                                      "scrape first."}
    # The subsidiaries SEC's lists name, found by their CLAIMS: the pair's one
    # shared edge may carry another source's answer and filing type (a PSC
    # stake outranks a list that states none — app.scraper.owns_merge). The
    # exhibit may have drawn a TREE, so this walks it: every active edge whose
    # pair an Exhibit 21 claim backs, from the filer down, each dated as
    # (its holder, the subsidiary).
    from app.db.arcadedb import run_sql
    edges: list[dict] = []
    seen, frontier = {company_id}, [company_id]
    while frontier:
        owner = frontier.pop()
        listed = {r["to_id"] for r in run_sql(
            "SELECT to_id FROM Claim WHERE from_id = :id AND kind = 'owns' "
            "AND (filing_type = 'EX-21' OR filing_type = 'EX-8.1')", {"id": owner})}
        with runner.db.get_session() as session:
            rows = list(session.run(
                """MATCH (c:Entity {id: $id})-[r:OWNS]->(s:Entity) WHERE r.until IS NULL
                   RETURN s.id AS sid, s.name AS name""", id=owner))
        for e in rows:
            if e.get("sid") in listed and e["sid"] not in seen:
                seen.add(e["sid"])
                frontier.append(e["sid"])
                edges.append({"owner": owner, "sid": e["sid"], "name": e.get("name")})
    if not edges:
        return {"status": "no_subsidiaries", "company": company, "entity_id": company_id,
                "total": 0, "detail": "No Exhibit 21 subsidiaries in the graph yet — run "
                                      "sec-ex21 first."}

    with record_run("sec-ex21-history", company) as run:
        run["entity_id"] = company_id
        history = fetch_subsidiary_history(entity["sec_cik"], max_filings or HISTORY_MAX_FILINGS)
        read = [h for h in history if h["names"] is not None]
        if not read:
            run["status"], run["note"] = "failed", "no readable annual subsidiary list"
            return {"status": "no_history", "company": company, "entity_id": company_id,
                    "total": 0, "filings": len(history)}
        dated = unmatched = newly = 0
        oldest = None
        for e in edges:
            found = earliest_listing(history, e["name"] or "")
            if not found:
                unmatched += 1          # named differently in the exhibit than in the graph
                continue
            newly += found["basis"] == "newly_listed"
            if set_since_lower_bound(e["owner"], e["sid"], found["as_of"], found["url"], found["basis"]):
                dated += 1
                oldest = min(oldest or found["as_of"], found["as_of"])
        run["total"] = dated
        return {"status": "ok", "company": company, "entity_id": company_id,
                "total": dated, "subsidiaries": len(edges), "unmatched": unmatched,
                "newly_listed": newly,
                "filings": len(history), "readable": len(read),
                "oldest_filing": read[-1]["filing_date"], "earliest_dated": oldest}


def _proxy_flags() -> None:
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true to enable.")
    if not runner.settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError("SEC EDGAR scraper is disabled. "
                              "Set SCRAPER_SEC_EDGAR_ENABLED=true to enable.")


def _finish_proxy_run(run: dict, result: dict, lost: list[str]) -> None:
    """The run row of a proxy-statement call: `skipped` when the company files
    no DEF 14A or is not in the graph, `failed` when EDGAR could not be read,
    the note naming why; lost requests noted as on every SEC run (#528)."""
    from app.scraper.run_log import mark_partial
    error = result.get("error") or result.get("db_error")
    if error and not result.get("owners"):
        run["status"] = ("skipped" if error.startswith("No DEF 14A") or result.get("db_error")
                         else "failed")
        run["note"] = error
    elif result.get("note"):
        run["note"] = result["note"]
    mark_partial(run, {"lost_requests": len(lost)})


@runner._with_autodedup
def run_proxy_statement(company: str) -> dict:
    """Read a company's latest DEF 14A ownership table (read-only) — the run
    behind POST /scraper/proxy-statement/run.

    Findings item 42: the endpoint called the reader directly — it ignored
    SCRAPER_ENABLED and SCRAPER_SEC_EDGAR_ENABLED, which every other SEC run
    honours, and left no ScrapeRun row, so a failed read showed nowhere."""
    _proxy_flags()
    from app.routers.search import resolve_best_entity
    from app.scraper.proxy_statement import fetch_proxy_ownership
    from app.scraper.run_log import record_run
    from app.scraper.sec_client import collect_lost_requests

    entity = resolve_best_entity(company, None)
    with record_run("sec-proxy", company) as run:
        run["entity_id"] = entity["id"] if entity else None
        with collect_lost_requests() as lost:
            # the CIK the graph holds beats a name search (findings item 47)
            result = fetch_proxy_ownership(company, cik=(entity or {}).get("sec_cik"))
        run["total"] = sum(1 for o in result.get("owners") or []
                           if o.get("voting_power_pct") is not None)
        _finish_proxy_run(run, result, lost)
    return result


@runner._with_autodedup
def run_proxy_write(company: str, entity_id: str | None = None) -> dict:
    """Read the latest DEF 14A and add voting power to existing OWNS edges —
    the run behind POST /scraper/proxy-statement/write (findings item 42: the
    same flags and run row as every SEC write)."""
    _proxy_flags()
    from app.scraper.proxy_write import write_proxy_ownership
    from app.scraper.run_log import record_run
    from app.scraper.sec_client import collect_lost_requests

    with record_run("sec-proxy-write", company) as run:
        with collect_lost_requests() as lost:
            result = write_proxy_ownership(company, entity_id)
        run["entity_id"] = result.get("entity_id") or entity_id
        run["total"] = len(result.get("updated") or [])
        _finish_proxy_run(run, result, lost)
    return result


@runner._with_autodedup
def run_sec_formd(company: str, force: bool = False) -> dict:
    """Ingest one issuer's board and officers from its newest SEC Form D.

    The private-company window: Form D is filed by the ISSUER for a Reg D
    raise and is the only statutory family naming a private company's board
    (SpaceX's lists Musk, Shotwell, Gracias). Enriches, does not discover —
    the company needs a CIK, like 13F and Exhibit 21.

    Written: HAS_ROLE per related person (Form D's own vocabulary — Executive
    Officer / Director / Promoter), source_date = the filing date (Form D
    states no tenure dates, so the filing's age is the honesty marker); the
    issuer's jurisdiction of incorporation fills country + jurisdiction_code
    if missing. Corporate "related persons" (a GP LLC named as an officer)
    are skipped and counted — a company minted as a Person is the Berkshire
    bug. One filing = one ingest, keyed on the accession; --force re-reads."""
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true to enable.")
    if not runner.settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError("SEC EDGAR scraper is disabled. "
                              "Set SCRAPER_SEC_EDGAR_ENABLED=true to enable.")

    from app.routers.search import resolve_best_entity
    from app.scraper.run_log import record_run
    from app.scraper.sec_ex21 import jurisdiction_country, jurisdiction_subdivision
    from app.scraper.sec_formd import latest_form_d

    entity = resolve_best_entity(company, None)
    if not entity:
        return {"status": "no_results", "company": company, "total": 0, "scraped": []}
    company_id = entity["id"]
    if not entity.get("sec_cik"):
        return {"status": "needs_sec_scrape", "company": company,
                "entity_id": company_id, "total": 0,
                "detail": "The entity has no SEC CIK yet — run the SEC EDGAR "
                          "scrape first."}

    with record_run("sec-formd", company) as run:
        run["entity_id"] = company_id
        source_id = runner._ensure_source(runner.SEC_EDGAR_SOURCE_NAME, runner.SEC_EDGAR_SOURCE_URL,
                                   runner.SEC_EDGAR_CREDIBILITY)
        data = latest_form_d(entity["sec_cik"])
        if not data:
            run["status"], run["note"] = "skipped", "no Form D on file"
            return {"status": "no_filing", "company": company,
                    "entity_id": company_id, "total": 0,
                    "detail": "No Form D (with XML) on file for this issuer."}

        with runner.db.get_session() as session:
            row = session.run("MATCH (e:Entity {id: $id}) "
                              "RETURN e.sec_formd_ingested AS ingested",
                              id=company_id).single()
        if not force and row and row.get("ingested") == data["accession"]:
            run["status"], run["note"] = "skipped", "filing already ingested"
            return {"status": "fresh", "company": company, "entity_id": company_id,
                    "total": 0, "filing_date": data["filing_date"],
                    "detail": "This Form D is already ingested; a newer filing "
                              "opens the gate by itself. --force re-reads."}

        # Issuer enrichment: the stated jurisdiction, fill-if-missing.
        juris = data.get("jurisdiction")
        if juris:
            with runner.db.get_session() as session:
                session.run(
                    "MATCH (e:Entity {id: $id}) "
                    "SET e.country = COALESCE(e.country, $c), "
                    "    e.jurisdiction_code = COALESCE(e.jurisdiction_code, $jc)",
                    id=company_id, c=jurisdiction_country(juris),
                    jc=jurisdiction_subdivision(juris))

        written, corporate_skipped = 0, 0
        scraped: list[dict] = []
        for person in data["persons"]:
            name = person["name"]
            if runner.has_entity_suffix(name) or not runner.is_person_name(name):
                corporate_skipped += 1
                continue
            person_id = runner._upsert_person_by_name(name, source_id=source_id)
            for role in person["roles"]:
                runner._upsert_role_sec(person_id, company_id, role, source_id,
                                 source_url=data["url"],
                                 source_date=data["filing_date"],
                                 filing_type="Form D",
                                 read_from=runner.READ_FIELD)  # Form D primary_doc.xml
                written += 1
            scraped.append({"type": "person", "name": name,
                            "role": ", ".join(person["roles"])})

        with runner.db.get_session() as session:
            session.run("MATCH (e:Entity {id: $id}) SET e.sec_formd_ingested = $a",
                        id=company_id, a=data["accession"])
        run["total"] = written
        return {"status": "ok", "company": company, "entity_id": company_id,
                "form": data["form"], "filing_date": data["filing_date"],
                "total": written, "corporate_skipped": corporate_skipped,
                "scraped": scraped}


@runner._with_autodedup
def run_sec_13f(company: str, limit: int = 100, window_days: int | None = None,
                force: bool = False) -> dict:
    """Ingest one issuer's institutional holders from Form 13F info tables.

    The inverse question to `run_sec_holdings` (what a manager holds): who holds
    this company. 13F fills the sub-5% blind spot — 13D/G only exists above the
    threshold, so Nvidia's 0.9% of SpaceX can never appear there, but it is in
    every quarter's 13F.

    Enriches, does not discover: the company must already be in the graph (any
    source), because the write needs a node to point at and the matching needs
    its names. Percentages are computed, never transcribed — 13F reports counts
    and dollars, and stake = shares / shares outstanding via `_pct_of`, with its
    precision floor (a real but tiny position gets no percentage rather than a
    false 0.0%). votingAuthority is deliberately NOT written: it states the
    manager's authority over its own held shares, not a bloc's share of the
    issuer's votes, and mapping it onto `voting_power_pct` would mark every
    index fund as a voting bloc.

    The discovered CUSIP is stamped on the company (fill-if-missing), so the
    next run matches info-table rows exactly instead of by name.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true to enable.")
    if not runner.settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError("SEC EDGAR scraper is disabled. "
                              "Set SCRAPER_SEC_EDGAR_ENABLED=true to enable.")

    from app.routers.search import resolve_best_entity
    from app.scraper.run_log import record_run
    from app.scraper.sec_13dg_text import _pct_of
    from app.scraper.sec_13f import (_13F_WINDOW_DAYS, fetch_13f_holders, latest_13f_deadline,
                                     next_13f_deadline)
    from app.scraper.sec_client import _cik10
    from app.scraper.sec_shares import fetch_shares_outstanding
    from app.scraper.sec_writer import mark_13f_stale

    entity = resolve_best_entity(company, None)
    if not entity:
        return {"status": "no_results", "company": company, "total": 0, "scraped": []}
    company_id = entity["id"]
    names = [n for n in [entity.get("name"), *(entity.get("aliases") or [])] if n]

    # 13F comes AFTER the schedules scrape, by design: that scrape stamps the
    # CIK (the denominator lookup needs it) and harvests the CUSIP that lets
    # info-table rows match exactly instead of by name.
    if not entity.get("sec_cik"):
        return {"status": "needs_sec_scrape", "company": company,
                "entity_id": company_id, "total": 0,
                "detail": "The entity has no SEC CIK yet — run the SEC EDGAR "
                          "scrape (13D/G + Form 4) first."}

    with runner.db.get_session() as session:
        row = session.run("MATCH (e:Entity {id: $id}) "
                          "RETURN e.sec_13f_scraped_at AS at",
                          id=company_id).single()
    last_run = row.get("at") if row else None

    # Quarterly by deadline, not by TTL: 13Fs are due 45 days after quarter
    # end, so a re-run before the next deadline reads the same filings. Fresh
    # means no new deadline has passed since the last run — the day after a
    # deadline the gate opens by itself, however recent the last run was.
    if not force and last_run:
        today = datetime.now(timezone.utc).date()
        if datetime.fromisoformat(last_run).date() >= latest_13f_deadline(today):
            return {"status": "fresh", "company": company,
                    "entity_id": company_id, "total": 0, "last_run": last_run,
                    "next_deadline": next_13f_deadline(today).isoformat()}

    # A refresh reads only what is NEW since the last completed run (plus a
    # week for stragglers), not the full discovery window — but ONLY when the
    # caller left the window to us (None). An explicit window is a request
    # and wins: shrinking a caller's 135 to 8 days is how the cusip re-read
    # for SpaceX kept missing NVIDIA's August filing. 0 stays all-time.
    if window_days is None:
        window_days = _13F_WINDOW_DAYS
        if last_run:
            days_since = (datetime.now(timezone.utc)
                          - datetime.fromisoformat(last_run)).days
            window_days = min(window_days, days_since + 7)

    with record_run("sec-13f", company) as run:
        run["entity_id"] = company_id
        source_id = runner._ensure_source(runner.SEC_EDGAR_SOURCE_NAME, runner.SEC_EDGAR_SOURCE_URL,
                                   runner.SEC_EDGAR_CREDIBILITY)
        known_cusips = [c for c in (entity.get("cusips") or
                                    ([entity.get("cusip")] if entity.get("cusip") else []))
                        if c]
        # Filers the graph already knows jump the fetch queue: their filings
        # are read even when relevance ranks them 4,000th of 5,000.
        with runner.db.get_session() as session:
            priority_ciks = {r.get("c") for r in session.run(
                "MATCH (x:Entity) WHERE x.sec_cik IS NOT NULL RETURN x.sec_cik AS c")
                if r.get("c")}
        data = fetch_13f_holders(entity.get("name") or company, known_names=names,
                                 cusips=known_cusips or None, limit=limit,
                                 window_days=window_days,
                                 priority_ciks=priority_ciks)
        # Local copy: the second pass below may add filers, and the fetched
        # dict must not be mutated (shared fixtures in tests taught us why).
        holders = list(data["holders"])

        # The denominator, once per run. Padded: the companyconcept endpoint
        # 404s an unpadded CIK. A company with no XBRL (SpaceX is private)
        # yields None and every stake stays a share count without a percentage
        # — which is honest, and the UI shows the counts.
        outstanding = None
        if entity.get("sec_cik"):
            outstanding = fetch_shares_outstanding(_cik10(entity["sec_cik"]))

        # Union-stamp every CUSIP seen: one issuer legitimately circulates
        # under several (SpaceX: 84615Q103 beside 69608A108 — private-company
        # CUSIPs have no single authority), and each stored one becomes a
        # search term next run.
        all_cusips = sorted({*known_cusips, *(data.get("cusips_seen") or [])})
        extra_filers = 0
        if all_cusips != sorted(known_cusips):
            with runner.db.get_session() as session:
                session.run("MATCH (e:Entity {id: $id}) "
                            "SET e.cusips = $cs, e.cusip = COALESCE(e.cusip, $c)",
                            id=company_id, cs=all_cusips,
                            c=data.get("cusip_seen"))
            # NEW identifiers were just discovered, meaning the search above
            # missed every filer using them ("SPACE EXPLORATION TECHN CORP" is
            # how NVIDIA files SpaceX, under a cusip nobody else used). Search
            # again with the full set in the same run: without this, the first
            # ingest of a company silently lacks its biggest holders and only
            # a --force re-run would find them.
            second = fetch_13f_holders(entity.get("name") or company,
                                       known_names=names,
                                       cusips=all_cusips, limit=limit,
                                       window_days=window_days,
                                       priority_ciks=priority_ciks)
            have = {h["filer_cik"] for h in holders}
            extra = [h for h in second["holders"] if h["filer_cik"] not in have]
            extra_filers = len({h["filer_cik"] for h in extra})
            if extra:
                runner.log.info("SEC EDGAR 13F: cusip second pass found %d filers the "
                         "first search missed", len(extra))
                holders = [*holders, *extra]
            # The run's figures cover both passes (findings item 57): on a fresh
            # database the first pass searched Berkshire's stored class-A CUSIP
            # and the name (8,283 filings on EDGAR) while the class-B CUSIP it
            # learned from the tables had 8,459 — the second pass's total — and
            # the run reported the smaller search as if it were the whole. Reads
            # are summed (both passes fetched), the total is the larger search's.
            data = {**data,
                    "filings_fetched": data.get("filings_fetched", 0) + second.get("filings_fetched", 0),
                    "filings_total": max(data.get("filings_total", 0), second.get("filings_total", 0)),
                    "passes": 2}

        written = 0
        for h in holders:
            filer_id = runner._upsert_entity_by_name(
                name=h["filer_name"], entity_type="company",
                cik=h["filer_cik"], source_id=source_id)
            if not filer_id:
                continue
            pct = _pct_of(h["shares"], outstanding)
            runner._upsert_owns_sec(
                owner_id=filer_id, owned_id=company_id, source_id=source_id,
                read_from=runner.READ_FIELD,  # the 13F information table's XML
                ownership_type=(runner.derive_ownership_type(pct) if pct is not None
                                else "minority"),
                file_date=h.get("period"),
                stake_percent=pct,
                shares=h["shares"], shares_outstanding=outstanding,
                share_class=h.get("share_class"), value_usd=h.get("value_usd"),
                filing_type="13F",
                # A quarter-end snapshot: held at the period end, not bought then.
                filing_dates_the_stake=False,
                # ...but held BY then: the earliest quarter a holder appears in is
                # a lower bound (combined in the writer, so a later quarter never
                # moves it), and time travel shows the holding from it on.
                since_date=h.get("period"), since_basis="first_reported",
                source_url=h.get("source_url"))
            written += 1

        # Dim what this quarter did not confirm — only when the run actually
        # ingested a period, so an empty refresh window dims nothing.
        stale_marked = 0
        if written and data.get("period"):
            stale_marked = mark_13f_stale(company_id, data["period"])

        # The quarterly gate's date — stamped only on a completed run, so a
        # crashed run never counts as fresh.
        with runner.db.get_session() as session:
            session.run("MATCH (e:Entity {id: $id}) SET e.sec_13f_scraped_at = $now",
                        id=company_id, now=datetime.now(timezone.utc).isoformat())

        run["total"] = written
        # What was selected, in the run log (findings item 40): a rebuild that
        # loses the big holders must show it here, not only in a later diff.
        run["note"] = _13f_selection_note(data, extra_filers)
        runner.log.info("SEC 13F: %d holder edges for %r (%d/%d filings read)",
                 written, company, data["filings_fetched"], data["filings_total"])
        return {"status": "ok", "company": company, "entity_id": company_id,
                "total": written, "stale_marked": stale_marked,
                "cusip": data["cusip_seen"],
                "period": data["period"], "shares_outstanding": outstanding,
                "filings_fetched": data["filings_fetched"],
                "filings_total": data["filings_total"],
                "passes": data.get("passes", 1),
                "priority_known": data.get("priority_known", 0),
                "priority_found": data.get("priority_found", 0),
                "priority_failed": data.get("priority_failed", 0)}


def _13f_selection_note(data: dict, extra_filers: int = 0) -> str:
    """The 13F run's selection in one line for its run-log row (findings item
    40): filings read of those on EDGAR — both passes' reads over the larger
    search's total when the CUSIP second pass ran (findings item 57) — and how
    many graph-known filers the by-CIK search found; the second pass adds
    `extra_filers`."""
    passes = " in two passes" if data.get("passes", 1) > 1 else ""
    note = (f"{data.get('filings_fetched', 0)}/{data.get('filings_total', 0)} filings read{passes}; "
            f"known filers: {data.get('priority_found', 0)} of "
            f"{data.get('priority_known', 0)} found by CIK")
    if data.get("priority_failed"):
        note += f", {data['priority_failed']} CIK searches failed"
    if extra_filers:
        note += f"; second pass +{extra_filers} filers"
    return note


@runner._with_autodedup
def run_sec_holdings(cik: str, limit: int = 100, succeeds_cik: str | None = None) -> dict:
    """Ingest what one SEC filer owns — the 13D/13G stakes it discloses in others.

    Keyed on CIK rather than a name because that is what identifies a filer, and
    because the company this matters for is often not findable by name: Vanguard's
    live book sits under VANGUARD CAPITAL MANAGEMENT LLC (0002100119), not the
    VANGUARD GROUP INC (0000102909) most people would search for.

    ``succeeds_cik`` records that this filer took over from another — a
    SUCCEEDED_BY edge, predecessor → successor. Given explicitly rather than
    inferred: filing patterns can suggest a handover, but asserting a corporate
    relationship from them would be a guess written into the graph as fact.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true to enable.")
    if not runner.settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError("SEC EDGAR scraper is disabled. "
                              "Set SCRAPER_SEC_EDGAR_ENABLED=true to enable.")

    from app.scraper.sec_filer import (fetch_filer_country, fetch_filer_headquarters, fetch_filer_name,
                                       fetch_filer_website)
    from app.scraper.sec_holdings import fetch_filer_holdings

    filer_name = fetch_filer_name(cik)
    if not filer_name:
        return {"status": "no_results", "cik": cik, "total": 0, "scraped": []}

    source_id = runner._ensure_source(runner.SEC_EDGAR_SOURCE_NAME, runner.SEC_EDGAR_SOURCE_URL, runner.SEC_EDGAR_CREDIBILITY)
    # Free: the submissions document was already fetched for the name and is cached.
    filer_id = runner._upsert_entity_by_name(name=filer_name, entity_type="company",
                                      cik=cik, source_id=source_id, register_name=True,
                                      country=fetch_filer_country(cik),
                                      headquarters=fetch_filer_headquarters(cik),
                                      website=fetch_filer_website(cik))
    runner_sec._record_sec_industry(filer_id, cik, source_id)

    holdings = fetch_filer_holdings(cik, limit=limit)
    written = closed = 0
    touched: list[str] = []
    for h in holdings:
        subject_name = (h.get("subject_name") or "").strip()
        if not subject_name:
            continue
        subject_id = runner._upsert_entity_by_name(
            name=subject_name, entity_type="company", cik=h.get("subject_cik"),
            source_id=source_id,
            country=fetch_filer_country(h["subject_cik"]) if h.get("subject_cik") else None,
            headquarters=fetch_filer_headquarters(h["subject_cik"]) if h.get("subject_cik") else None,
            website=fetch_filer_website(h["subject_cik"]) if h.get("subject_cik") else None)
        runner_sec._record_sec_industry(subject_id, h.get("subject_cik"), source_id)
        runner_sec._keep_cover_cusip(subject_id, h.get("subject_cusip"))
        runner._upsert_owns_sec(
            owner_id=filer_id, owned_id=subject_id, source_id=source_id,
            read_from=h.get("read_from"),  # XML `field` or old cover `form` (item 44)
            ownership_type="minority", file_date=h.get("file_date"),
            stake_percent=h.get("stake_percent"), source_url=h.get("source_url"),
            voting_power_pct=h.get("voting_power_pct"), until=h.get("until"),
            filing_type=h.get("filing_type"), event_date=h.get("event_date"),
            since_date=h.get("since"), since_basis=h.get("since_basis"),
            final_report=h.get("final_report"),     # the filer's last report (item 35)
        )
        written += 1
        if h.get("until"):
            closed += 1
        touched.append(subject_id)
    # the parent's newer 13D/G replaces its subsidiary's (findings item 34)
    from app.scraper.sec_writer import close_reported_by_parent
    for owned in dict.fromkeys(t for t in touched if t):
        close_reported_by_parent(owned_id=owned)

    # Group structure from the 13F cover page — one extra request, and a far
    # better signal than name matching.
    from app.scraper.sec_13f import fetch_affiliated_managers
    affiliates = _write_affiliates(filer_id, fetch_affiliated_managers(cik), source_id)

    succession = None
    if succeeds_cik:
        pred_name = fetch_filer_name(succeeds_cik)
        if pred_name:
            pred_id = runner._upsert_entity_by_name(name=pred_name, entity_type="company",
                                             cik=succeeds_cik, source_id=source_id)
            runner._upsert_succession(pred_id, filer_id, source_id,
                               credibility_score=runner.SEC_EDGAR_CREDIBILITY)
            succession = {"predecessor": pred_name, "successor": filer_name}

    runner.log.info("SEC EDGAR: %s — %d holdings written (%d already ended)",
             filer_name, written, closed)
    return {
        "status": "ok", "cik": cik, "filer": filer_name,
        "total": written, "ended": closed, "affiliates": affiliates,
        "succession": succession,
        "scraped": [{"type": "entity", "name": h["subject_name"], "role": "holding"}
                    for h in holdings],
    }
