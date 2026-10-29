"""A register writes the company a knowledge base points at.

Wikidata states an LEI (P1278) and an SEC CIK (P5531) for many companies, and
until 2026-10-29 the Wikidata writer stored them on the node: a node it created
was keyed `lei:{LEI}` from Wikidata's word alone, and a node it found by name
was COALESCE-filled with the item's ids. A Wikidata item is an organisation,
not a legal entity, so its ids can belong to another legal entity than the
node: BlackRock's item Q219635 still states the pre-2024 parent's CIK and LEI
(the company renamed BlackRock Finance, Inc. when a new holding company took
the name), the writer joined it to the new parent's GLEIF node by name and
stamped the old CIK on it, and since the SEC name for a CIK wins, the listed
BlackRock was renamed "BlackRock Finance, Inc." while its own filings drew a
second node.

So a hard id on a node comes only from its register. Wikidata's ids are a
pointer: the writer looks the company up by them, and when the graph does not
hold it, the register is asked — GLEIF's API by LEI, EDGAR's submissions by
CIK — and writes the node itself, under its own name, id and source. Wikidata
then only adds what it adds anywhere: its item id, description, logo, edges.
A pointer the register does not know writes nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

GLEIF_RECORD_URL = "https://api.gleif.org/api/v1/lei-records/{lei}"


def _wrap(value) -> dict | None:
    """A scalar in the golden copy's `{"$": value}` shape (`gleif_succession._v`)."""
    return None if value in (None, "") else {"$": value}


def _api_day(stamp: str | None) -> str | None:
    """A GLEIF API date as the day the golden copy states.

    The API gives UTC instants: an entity created at midnight in Vienna reads
    "2006-02-12T23:00:00Z", where the golden copy says 2006-02-13. A local
    midnight east of Greenwich lands after noon of the day before in UTC, one
    west of it before noon of the same day, so an instant after noon is the
    next day's date."""
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp[:10] if len(stamp) >= 10 else None
    return (t + timedelta(days=1) if t.hour >= 12 else t).date().isoformat()


def _api_address(addr: dict | None) -> dict | None:
    if not addr:
        return None
    lines = [ln for ln in (addr.get("addressLines") or []) if ln]
    return {k: v for k, v in {
        "FirstAddressLine": _wrap(lines[0] if lines else None),
        "AdditionalAddressLine": [_wrap(ln) for ln in lines[1:]] or None,
        "City": _wrap(addr.get("city")),
        "Region": _wrap(addr.get("region")),
        "Country": _wrap(addr.get("country")),
        "PostalCode": _wrap(addr.get("postalCode")),
    }.items() if v}


def golden_copy_record(attributes: dict) -> dict:
    """A GLEIF API record's `attributes` in the golden copy's LEI-CDF shape,
    so the bulk importer's own reading (`gleif_lei_cdf._entity_props`) writes
    the node — one mapping of GLEIF's fields, not two."""
    entity = attributes.get("entity") or {}
    registration = attributes.get("registration") or {}

    def names(items):
        return [{"$": n["name"], "@type": n.get("type")} for n in items or [] if n.get("name")]

    return {
        "LEI": _wrap(attributes.get("lei")),
        "Entity": {
            "LegalName": _wrap((entity.get("legalName") or {}).get("name")),
            "OtherEntityNames": {"OtherEntityName": names(entity.get("otherNames"))},
            "TransliteratedOtherEntityNames": {
                "TransliteratedOtherEntityName": names(entity.get("transliteratedOtherNames"))},
            "LegalAddress": _api_address(entity.get("legalAddress")),
            "HeadquartersAddress": _api_address(entity.get("headquartersAddress")),
            "RegistrationAuthority": {
                "RegistrationAuthorityID": _wrap((entity.get("registeredAt") or {}).get("id")),
                "RegistrationAuthorityEntityID": _wrap(entity.get("registeredAs")),
            },
            "LegalJurisdiction": _wrap(entity.get("jurisdiction")),
            "LegalForm": {
                "EntityLegalFormCode": _wrap((entity.get("legalForm") or {}).get("id")),
                "OtherLegalForm": _wrap((entity.get("legalForm") or {}).get("other")),
            },
            "EntityStatus": _wrap(entity.get("status")),
            "EntityCreationDate": _wrap(_api_day(entity.get("creationDate"))),
            "EntityExpirationDate": _wrap(_api_day((entity.get("expiration") or {}).get("date"))),
        },
        "Registration": {
            "InitialRegistrationDate": _wrap(_api_day(registration.get("initialRegistrationDate"))),
            "RegistrationStatus": _wrap(registration.get("status")),
            "ValidationSources": _wrap(registration.get("corroborationLevel")),
        },
    }


def fetch_gleif_record(lei: str) -> dict | None:
    """The LEI's record from GLEIF's API in the golden-copy shape, or None when
    GLEIF does not know the LEI. A failure to ask raises."""
    import httpx

    from app.scraper.gleif_incremental import _HEADERS
    r = httpx.get(GLEIF_RECORD_URL.format(lei=lei), headers=_HEADERS, timeout=30, follow_redirects=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    attributes = ((r.json() or {}).get("data") or {}).get("attributes")
    return golden_copy_record(attributes) if attributes else None


def _gleif_enabled() -> bool:
    # The catalogue switch alone: GLEIF has no environment switch (its bulk
    # imports run from the CLI). The 2026-10-29 rebuild wrote no node from an
    # LEI because this also asked for one that was never set.
    from app.scraper.sources import get_source_enabled
    return get_source_enabled("bods_gleif")


def _sec_enabled() -> bool:
    from app.config import settings
    from app.scraper.sources import get_source_enabled
    return bool(settings.SCRAPER_SEC_EDGAR_ENABLED and get_source_enabled("sec_edgar"))


def write_from_gleif(lei: str) -> str | None:
    """The node GLEIF states for this LEI, written by the GLEIF importer's own
    reading of the record; its id, or None when GLEIF does not know the LEI.
    A node merged away answers with its survivor, which already exists."""
    from app.merged_ids import canonical_id
    from app.scraper.bulk_import import _BatchWriter
    from app.scraper.gleif_lei_cdf import _entity_props
    from app.scraper.runner import BODS_GLEIF_CREDIBILITY, GLEIF_SOURCE_NAME, GLEIF_SOURCE_URL, _ensure_source

    survivor = canonical_id(f"lei:{lei}")
    if survivor != f"lei:{lei}":
        return survivor
    rec = fetch_gleif_record(lei)
    if not rec:
        log.info("GLEIF does not know the LEI %s a knowledge base pointed at", lei)
        return None
    source_id = _ensure_source(GLEIF_SOURCE_NAME, GLEIF_SOURCE_URL, BODS_GLEIF_CREDIBILITY)
    built = _entity_props(rec, source_id, BODS_GLEIF_CREDIBILITY)
    if not built:
        return None
    node_id, props = built
    batch = _BatchWriter()
    batch.entity(node_id, props)
    batch.flush()
    return node_id


def write_from_edgar(cik: str) -> str | None:
    """The filer EDGAR states for this CIK, written as the SEC writer writes a
    filer it scrapes (EDGAR's name for the CIK, its country and business
    address); its id, or None when EDGAR has no such CIK."""
    import httpx

    from app.scraper.graph_writer import _upsert_entity_by_name
    from app.scraper.runner import (SEC_EDGAR_CREDIBILITY, SEC_EDGAR_SOURCE_NAME,
                                    SEC_EDGAR_SOURCE_URL, _ensure_source)
    from app.scraper.sec_edgar import _submissions, sec_country, sec_headquarters, sec_website
    try:
        sub = _submissions(cik)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            log.info("EDGAR does not know the CIK %s a knowledge base pointed at", cik)
            return None
        raise
    name = (sub or {}).get("name")
    if not name:
        return None
    source_id = _ensure_source(SEC_EDGAR_SOURCE_NAME, SEC_EDGAR_SOURCE_URL, SEC_EDGAR_CREDIBILITY)
    return _upsert_entity_by_name(name=name, entity_type="company", cik=cik, source_id=source_id,
                                  register_name=True, country=sec_country(sub),
                                  headquarters=sec_headquarters(sub), website=sec_website(sub))


def write_from_pointer(leis: list[str], ciks: list[str]) -> str | None:
    """The node a register writes for the first id it knows — an LEI before a
    CIK (GLEIF states the legal entity; a CIK names a filer) — or None when no
    register that is switched on knows any of them."""
    if leis and _gleif_enabled():
        for lei in leis:
            if node_id := write_from_gleif(lei):
                return node_id
    if ciks and _sec_enabled():
        for cik in ciks:
            if node_id := write_from_edgar(cik):
                return node_id
    return None


def contradiction(node: dict, leis: list[str], ciks: list[str]) -> str | None:
    """Why a node is not the legal entity a knowledge base's ids describe, or
    None. The node's LEI or CIK — written by its register — is not among the
    ones the item states. An id the item does not state is no contradiction,
    and nor is a node without one."""
    if node.get("lei_id") and leis and node["lei_id"] not in leis:
        return f"LEI {node['lei_id']} is not the stated {', '.join(leis)}"
    if node.get("sec_cik") and ciks and node["sec_cik"] not in ciks:
        return f"CIK {node['sec_cik']} is not the stated {', '.join(ciks)}"
    return None
