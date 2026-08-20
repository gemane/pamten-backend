"""
Companies House PSC snapshot importer — current UK beneficial-ownership data,
straight from the source (updated daily), replacing the frozen OpenOwnership UK
PSC BODS export (stuck at 2025-03).

The snapshot is newline-delimited JSON, one PSC per line:
``{"company_number": "...", "data": {"kind": "...", ...}}``. Each PSC controls the
company named only by its **number** (no company name — those come from the
Companies House BasicCompanyData product, a companion importer), so the controlled
company is a node keyed ``gb-coh:{number}`` that this import ensures exists but
leaves un-named.

Mapping:
  * individual PSC          → Person -[:OWNS]-> company
  * corporate/legal PSC     → Entity -[:OWNS]-> company (keyed on its own UK
                              company number when it has one)
  * natures_of_control      → stake_percent (ownership-of-shares band floor),
                              voting_power_pct (voting-rights band floor),
                              ownership_type (controlling when it has voting /
                              appointment / significant-influence)
  * super-secure PSCs and PSC statements are skipped.
"""
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from typing import IO

from app.scraper.bulk_import import (
    _BatchWriter, _drop_secondary_indexes, _entity, _max_pct, _now_iso,
    _ProgressBar, _rebuild_indexes,
)
from app.scraper.mapper import derive_ownership_type, parse_full_name

log = logging.getLogger(__name__)

# Every `kind` the snapshot contains, sorted into what it becomes. Counts are per
# 4M sampled records, so the rare ones are rare register-wide, not rare in a sample.
_PERSON_KINDS = (
    "individual-person-with-significant-control",     # 3,698,654
    "individual-beneficial-owner",                    # 8,343 — Register of Overseas Entities
)
_ENTITY_KINDS = (
    "corporate-entity-person-with-significant-control",  # 284,471
    "legal-person-person-with-significant-control",      # 4,924
    # The corporate twins of `individual-beneficial-owner`, both silently dropped
    # until 2026-08-19 while the individual one was mapped. The individual/corporate
    # split is Companies House's, not a distinction the graph cares about.
    "corporate-entity-beneficial-owner",                 # 3,336
    "legal-person-beneficial-owner",                     # 139
)
# Deliberately not imported. A super-secure PSC is one whose details Companies House
# withholds for personal safety; the record carries no name to write. Named here so
# the skip is a decision, rather than whatever falls through the allow-list.
_SKIP_KINDS = (
    "super-secure-person-with-significant-control",   # 116
    "super-secure-beneficial-owner",                  # 17
)
_BAND = re.compile(r"(\d+)-to-\d+-percent")


def _band_floor(nature: str) -> int | None:
    m = _BAND.search(nature)
    return int(m.group(1)) if m else None


def _control(natures: list[str] | None) -> tuple:
    """(stake_percent, voting_power_pct, ownership_type, interest_types) from a
    PSC's natures_of_control — economic vs voting kept separate."""
    natures = natures or []
    stake = voting = None
    controlling = False
    for nat in natures:
        if "ownership-of-shares" in nat:
            stake = _max_pct(stake, _band_floor(nat))
        elif "voting-rights" in nat:
            voting = _max_pct(voting, _band_floor(nat))
            controlling = True
        elif "right-to-appoint" in nat or "significant-influence" in nat:
            controlling = True
    if controlling:
        otype = "controlling"
    elif stake is not None:
        otype = derive_ownership_type(stake)
    else:
        otype = "minority"
    return stake, voting, otype, sorted(set(natures))


def _psc_name(data: dict) -> str | None:
    if name := (data.get("name") or "").strip():
        return name
    ne = data.get("name_elements") or {}
    parts = [ne.get("forename"), ne.get("middle_name"), ne.get("surname")]
    return " ".join(p for p in parts if p).strip() or None


def _nationality(raw: str | None) -> str:
    """PSC nationality as an ISO-2 code where it can be recognised.

    Companies House records a **demonym** typed by the filer — "British", not "GB"
    — while Wikidata writes an ISO-2 code, so the field used to hold `GB` and
    `British` side by side, meaning the same thing and grouping as two.

    An unrecognised value is kept **verbatim**: it is free text and the tail is
    long, and what the register said is worth more than a tidy blank. See
    ``maintenance.normalize_person_nationalities`` for the pass that reports the
    residue so the table can be extended.
    """
    from app.scraper.maintenance import nationality_to_iso2
    return nationality_to_iso2(raw) or (raw or "").strip()


def _birth_date(data: dict) -> str:
    dob = data.get("date_of_birth") or {}
    y, m = dob.get("year"), dob.get("month")
    return f"{y:04d}-{m:02d}" if y and m else (f"{y:04d}" if y else "")


def _entity_psc_id(data: dict) -> tuple[str, str | None]:
    """(node_id, companies_house_id) for a corporate/legal PSC — keyed on its own
    UK company number when it has one, else on the PSC self-link."""
    ident = data.get("identification") or {}
    reg = (ident.get("registration_number") or "").strip()
    country = (ident.get("country_registered") or "").lower()
    if reg and ("england" in country or "wales" in country or "scotland" in country
                or "united kingdom" in country or country in ("uk", "gb")):
        return f"gb-coh:{reg}", reg
    self_link = ((data.get("links") or {}).get("self") or "").strip()
    return f"chpsc:{self_link}", None


def _iso2_country(name: str | None) -> str | None:
    """A PSC address ``country`` name → ISO-2 code. Companies House uses UK
    subdivisions ('England', 'England & Wales', 'Scotland', …) which aren't ISO
    countries — all map to GB; other names go through the shared name→code table."""
    if not name:
        return None
    n = name.strip().lower()
    if n in ("uk", "gb") or any(uk in n for uk in (
            "england", "wales", "scotland", "northern ireland",
            "united kingdom", "great britain")):
        return "GB"
    from app.scraper.bulk_import import _ISO2_COUNTRY
    return {v.lower(): k for k, v in _ISO2_COUNTRY.items()}.get(n) or (
        name.strip().upper() if len(name.strip()) == 2 else None)


def _psc_address(addr: dict | None) -> tuple[str | None, str | None, str | None]:
    """(display registered_address, hq_city, hq_country ISO-2) from a PSC record's
    correspondence ``address`` dict — the corporate PSC's own service address."""
    addr = addr or {}
    parts = [addr.get("premises"), addr.get("address_line_1"), addr.get("address_line_2"),
             addr.get("locality"), addr.get("region"), addr.get("postal_code"),
             addr.get("country")]
    display = ", ".join(p.strip() for p in parts if p and str(p).strip()) or None
    city = (addr.get("locality") or "").strip() or None
    return display, city, _iso2_country(addr.get("country"))


@dataclass(frozen=True)
class PscMapped:
    """One PSC record, mapped to graph shapes but not yet written anywhere.

    Pure: no writer, no database, no clock. That is what lets the incremental
    refresh reuse the mapping without reusing the bulk writer, and what lets the
    mapping be unit-tested at all — while it lived inside `_process` the only way
    to see its output was to hand it a batch writer and inspect the calls.
    """
    kind_cat: str                 # 'person' | 'entity'
    self_link: str                # data.links.self — identifies the APPOINTMENT
    company_id: str               # gb-coh:{number}, the company being controlled
    company_props: dict
    owner_id: str
    owner_label: str              # 'Person' | 'Entity'
    owner_props: dict
    edge_props: dict


def psc_record(rec: dict, source_id: str, credibility_score: int) -> PscMapped | None:
    """Map one snapshot record, or None when it is not one we import.

    `last_scraped_at` is deliberately absent from `edge_props`: it is a clock
    reading, and leaving it to the caller keeps this function's output a pure
    function of its input — which is what the refresh's digest depends on.
    """
    company_number = (rec.get("company_number") or "").strip()
    data = rec.get("data") or {}
    kind = data.get("kind")
    if not company_number or kind not in _PERSON_KINDS + _ENTITY_KINDS:
        return None
    name = _psc_name(data)
    if not name:
        return None
    self_link = ((data.get("links") or {}).get("self") or "").strip()

    stake, voting, otype, interest_types = _control(data.get("natures_of_control"))
    since = data.get("notified_on") or None
    until = data.get("ceased_on") or None

    if kind in _PERSON_KINDS:
        first, last = parse_full_name(name)
        owner_id, owner_label = f"chpsc:{self_link}", "Person"
        owner_props = {
            "first_name": first, "last_name": last, "full_name": name,
            "search_text": name, "nationality": _nationality(data.get("nationality")),
            "birth_date": _birth_date(data), "description": "",
            "verified": False, "alias": [], "nationalities": [],
        }
        kind_cat = "person"
    else:
        owner_id, chid = _entity_psc_id(data)
        # The corporate PSC's own correspondence address (in the record) → its address
        # + map location; otherwise it's a name-only node.
        reg_addr, hq_city, hq_country = _psc_address(data.get("address"))
        owner_label, kind_cat = "Entity", "entity"
        owner_props = {
            "name": name, "entity_type": "company",
            "country": ((data.get("identification") or {}).get("country_registered") or None),
            "companies_house_id": chid, "registered_address": reg_addr,
            "hq_address": reg_addr, "hq_city": hq_city, "hq_country": hq_country,
        }

    return PscMapped(
        kind_cat=kind_cat,
        self_link=self_link,
        company_id=f"gb-coh:{company_number}",
        # The controlled company is ensured to exist, and named later from
        # BasicCompanyData — this import knows its number and nothing else.
        company_props={"companies_house_id": company_number, "source_id": source_id},
        owner_id=owner_id,
        owner_label=owner_label,
        owner_props=owner_props,
        edge_props={
            "stake_percent": stake, "voting_power_pct": voting, "ownership_type": otype,
            "interest_types": interest_types, "direct_or_indirect": None,
            "since": since, "until": until, "source_id": source_id,
            "credibility_score": credibility_score,
            # The key the incremental refresh matches an edge on. Per appointment,
            # so exactly one edge per snapshot record — see ch_psc_incremental.
            "psc_self_link": self_link,
            "source_url": f"https://find-and-update.company-information.service.gov.uk/company/{company_number}/persons-with-significant-control",
            "source_date": since,
        },
    )


def _process(rec: dict, batch: "_BatchWriter", source_id: str, credibility_score: int) -> str | None:
    """Write one PSC record's nodes + OWNS edge. Returns 'person' | 'entity' | None."""
    mapped = psc_record(rec, source_id, credibility_score)
    if mapped is None:
        return None

    batch.entity(mapped.company_id, mapped.company_props)
    if mapped.kind_cat == "person":
        batch.person(mapped.owner_id, mapped.owner_props)
    else:
        props = mapped.owner_props
        _entity(batch, mapped.owner_id, name=props["name"], entity_type=props["entity_type"],
                country=props["country"], founded=None, lei_id=None,
                companies_house_id=props["companies_house_id"],
                source_id=source_id, credibility_score=credibility_score,
                registered_address=props["registered_address"], hq_address=props["hq_address"],
                hq_city=props["hq_city"], hq_country=props["hq_country"])

    batch.owns(mapped.owner_id, mapped.owner_label, mapped.company_id,
               {**mapped.edge_props, "last_scraped_at": _now_iso()})
    return mapped.kind_cat


def import_ch_psc(filepath: str, source_id: str, credibility_score: int,
                  limit: int | None = None, bulk_load: bool = False,
                  batch_size: int = 400,
                  only_companies: set[str] | None = None,
                  digest_out: str | None = None) -> dict:
    """Import a Companies House PSC snapshot (.zip/.txt). Returns counts.

    ``batch_size`` sets how many records flush per ``sqlscript`` round-trip. Behind
    a proxy with a short read timeout (e.g. dev-db's 60s nginx), a smaller batch
    keeps each flush well under the limit so heavy flushes don't 504-then-retry
    (the dominant cost of a slow import); connected directly to ArcadeDB, a larger
    batch cuts round-trips.

    ``only_companies`` restricts the import to that set of company numbers (the curated
    test subset). A cheap raw-bytes prefilter skips JSON parsing for non-matches, so a
    handful of companies still loads quickly — but the whole file is read.

    ``digest_out`` writes the digest sidecar the incremental refresh diffs against,
    from this same pass over the same bytes so the two can never describe different
    files. It covers **every record in the snapshot**, including those this load
    skipped: the refresh digests the whole file, so a digest of only the subset
    would make the next run see the entire register as new. That means a subset
    load with ``--digest-out`` gives up the prefilter and parses all 15.8M lines.

    It used to stop early, once every target company had been seen, on the stated
    assumption that "a CH snapshot groups a company's PSC records together". **It does
    not.** Measured over 300,000 lines of the real snapshot: 41,847 of 247,219 distinct
    companies reappear after another company has intervened — **16.9%**. Stopping at the
    first sighting therefore dropped later PSC records for about one company in six, and
    silently: the run reported success with a plausible count. Every curated database
    built this way, including the dev graph and `psc-import-test.sh`, is affected.

    A full pass over 12.9 GB costs a couple of minutes with the prefilter doing the work.
    That is the price of the subset being complete, and the refresh that rides on this
    baseline can only be correct if the baseline is."""
    if filepath.lower().endswith(".zip"):
        zf = zipfile.ZipFile(filepath)
        entry = zf.namelist()[0]
        total_bytes = zf.getinfo(entry).file_size
        raw: IO[bytes] = zf.open(entry)          # ZipExtFile — line-iterable
        log.info("CH PSC: reading %s (%s bytes)", entry, f"{total_bytes:,}")
    else:
        total_bytes = os.path.getsize(filepath)
        raw = open(filepath, "rb")  # noqa: WPS515

    counts = {"records": 0, "persons": 0, "entities": 0, "skipped": 0, "errors": 0}
    only_bytes = [c.encode() for c in only_companies] if only_companies else None
    # The incremental refresh diffs against a digest of the snapshot this load came
    # from. Writing it here, from the same pass over the same bytes, is what stops
    # baseline and digest ever describing different files. Streamed to disk — the
    # register is 15.6M rows and holding them would cost more memory than the
    # import itself.
    digest_sink = None
    if digest_out:
        from app.scraper.ch_psc_incremental import DigestSink
        digest_sink = DigestSink(digest_out)
    matched: set[str] = set()
    if bulk_load:
        _drop_secondary_indexes()
    batch = _BatchWriter(batch_size=batch_size)
    bar = _ProgressBar("CH PSC")
    done = 0
    try:
        for line in raw:                          # one JSON record per line
            done += len(line)
            line = line.strip()
            if not line:
                continue
            if limit and counts["records"] >= limit:
                break
            # Curated subset: reject non-matching lines before the JSON parse (cheap
            # bytes search), then confirm the exact company_number after parsing.
            #
            # …unless a digest is being written, in which case every line has to be
            # parsed anyway. The digest describes the SNAPSHOT, not the subset we
            # chose to import: the refresh digests the whole file and diffs the two,
            # so a subset digest would make the next run see 15.8M records "added".
            # A subset load with --digest-out therefore costs a full parse.
            prefiltered_out = only_bytes is not None and not any(cb in line for cb in only_bytes)
            if prefiltered_out and digest_sink is None:
                continue
            if not prefiltered_out:
                counts["records"] += 1
                if counts["records"] % 50000 == 0:
                    bar.render(done, total_bytes)
            try:
                rec = json.loads(line)
                if digest_sink is not None:
                    digest_sink.add_record(rec)
                if prefiltered_out:
                    continue                      # digested, but not ours to import
                if only_companies is not None:
                    cn = (rec.get("company_number") or "").strip()
                    if cn not in only_companies:
                        continue                  # substring hit in another field
                    matched.add(cn)
                cat = _process(rec, batch, source_id, credibility_score)
                if cat == "person":
                    counts["persons"] += 1
                elif cat == "entity":
                    counts["entities"] += 1
                else:
                    counts["skipped"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad line mustn't abort
                counts["errors"] += 1
                if counts["errors"] <= 5:
                    log.warning("CH PSC record error: %s", exc)
        batch.flush()
        if only_companies is not None:
            # Now that the whole file is read, this is a real answer rather than a
            # restatement of the stop condition: how many of the companies asked for
            # actually carry PSC records. A company with none is normal (dissolved,
            # exempt, or simply not filed), but a large gap means the wrong list.
            counts["requested"] = len(only_companies)
            counts["found"] = len(matched)
            missing = len(only_companies) - len(matched)
            if missing:
                log.info("CH PSC: %d of %d requested companies had no PSC records",
                         missing, len(only_companies))
        bar.finish(f"{counts['records']:,} records, "
                   f"{counts['persons']:,} persons + {counts['entities']:,} entities"
                   + (f", {counts['found']}/{counts['requested']} requested companies found"
                      if only_companies is not None else ""))
    except BaseException:
        if digest_sink is not None:
            digest_sink.abandon()      # a partial sidecar must never look like a baseline
        raise
    finally:
        raw.close()
        if bulk_load:
            _rebuild_indexes()

    if digest_sink is not None:
        counts["digest"] = digest_sink.finish()
        counts["digest_records"] = digest_sink.rows

    log.info("CH PSC import done: %s", counts)
    return counts
