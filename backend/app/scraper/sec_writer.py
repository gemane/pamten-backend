"""
The SEC-specific write layer: 13D/G/13F/Form-4 edge upserts and the voting-group
model (a filing group as its own node, roster-matched across amendments).

Moved out of runner.py in the module split the multi-scraper refactor deferred:
runner keeps orchestration, graph_writer the shared write layer, and this module
everything whose semantics come from the SEC's forms. Behaviour is unchanged;
runner re-exports these names so existing imports and test patch targets keep
working.
"""
import logging
import unicodedata
import uuid
from datetime import datetime, timezone

from app.claims import KIND_OWNS, KIND_ROLE, record_claim
from app.database import db
from app.scraper.edge_schema import OWNS_PROPS, edge_create_clause, owns_props
from app.scraper.graph_writer import _now_iso
from app.scraper.mapper import coherent_ownership_type, normalize_entity_name

log = logging.getLogger(__name__)

#: A filing group is not a legal person: it holds no LEI, has no country, and
#: cannot be geocoded or deduplicated like a company. Everything that assumes an
#: Entity is a real organisation filters this type out — see `app/quality.py`
#: and the country/dedup passes in `app/scraper/maintenance.py`.
VOTING_GROUP_TYPE = "voting_group"

#: Item 8 codes meaning a human being, mirrored from sec_edgar so the writer can
#: classify a group member from what the filer stated rather than from its name.
_INDIVIDUAL_CODES = {"IN"}


def _is_control_filing(form_type: str | None) -> bool:
    """Schedule 13D — filed with control intent — as opposed to a passive 13G.

    The distinction decides whether a set of co-filers is a governance bloc or
    an asset manager's internal plumbing. Verified on Embraer: Morgan Stanley and
    Brandes file 13G there while AB InBev's families file 13D.
    """
    return "13D" in (form_type or "").upper()

#: Two rosters are the same group when they share this much. Overlap, not
#: equality, because both extremes are wrong: keying on the filer breaks as soon
#: as a different member submits the next amendment, and keying on the exact set
#: orphans the node the moment one party joins or leaves a continuing agreement.
_GROUP_MATCH_MIN_SHARED = 2
_GROUP_MATCH_MIN_RATIO = 0.5


def _member_key(name: str, cik: str | None) -> str:
    """Both identifiers for one party, encoded "cik|name".

    EDGAR gives a CIK only to members that are registrants — of AB InBev's nine
    reporting persons exactly one is — and pre-2024 filings carry names alone.
    Keeping both, and matching on either, is what lets a member that has a CIK in
    a post-2024 XML filing still match itself in an older SGML amendment.

    Encoded as a string rather than kept as a dict because the roster is stored
    on the node, and ArcadeDB rejects a property whose list contains maps
    ("InvalidPropertyType - Property values can not contain map values"). A list
    of scalars is fine — `interest_types` on PSC edges is the precedent.
    """
    from app.scraper.sec_edgar import _cik_int
    # Fold diacritics before normalising. EDGAR writes these names in ASCII —
    # "Eugenie Patri Sebastien S.A.", "BRC S.a R.L." — while every other source
    # accents them, and `normalize_entity_name` preserves accents, so the same
    # party would otherwise key two different ways ('brc rl' vs 'brc sà rl').
    # Folded here rather than in the shared normaliser: that one drives entity
    # dedup graph-wide and changing it is a separate decision.
    folded = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    norm = normalize_entity_name(folded) or folded.strip().lower()
    return f"{_cik_int(cik).zfill(10) if cik else ''}|{norm}"


def _split_member_key(key: str) -> tuple:
    cik, _, norm = (key or "").partition("|")
    return (cik or None), norm


def _same_member(a: str, b: str) -> bool:
    """One party seen twice. Either identifier suffices, because which one a
    filing carries depends on the era it was filed in."""
    a_cik, a_name = _split_member_key(a)
    b_cik, b_name = _split_member_key(b)
    if a_cik and b_cik:
        return a_cik == b_cik
    return bool(a_name) and a_name == b_name


def _roster_overlap(a: list, b: list) -> int:
    """How many parties two rosters share."""
    unmatched = list(b)
    shared = 0
    for m in a:
        for i, other in enumerate(unmatched):
            if _same_member(m, other):
                shared += 1
                unmatched.pop(i)
                break
    return shared


def _rosters_match(a: list, b: list) -> bool:
    if not a or not b:
        return False
    shared = _roster_overlap(a, b)
    smaller = min(len(a), len(b))
    return shared >= _GROUP_MATCH_MIN_SHARED and shared / smaller >= _GROUP_MATCH_MIN_RATIO


def _voting_group_name(size: int) -> str:
    """What a filing group is called: what it is, and how many are in it."""
    return f"Voting group · {size} parties" if size != 1 else "Voting group · 1 party"


def _upsert_voting_group(subject_id: str, subject_name: str, roster: list,
                         source_id: str) -> str:
    """The node standing for a 13D filing group, found by its roster.

    Deliberately not routed through `_upsert_entity_by_name`: that resolves on
    `name_normalized`, so two groups whose names normalise alike would silently
    become one. Identity here is the set of parties, matched by overlap against
    the groups already pointing at this same subject.
    """
    now = _now_iso()
    with db.get_session() as session:
        existing = list(session.run(
            f"""MATCH (g:Entity {{type: '{VOTING_GROUP_TYPE}'}})-[:OWNS]->(s:Entity {{id: $sid}})
                RETURN g.id AS id, g.member_keys AS roster""",
            sid=subject_id))

        best, best_shared = None, 0
        for row in existing:
            other = row["roster"] or []
            if _rosters_match(roster, other):
                shared = _roster_overlap(roster, other)
                if shared > best_shared:
                    best, best_shared = row["id"], shared

        # Named for what it is and how big, not for the company it holds: the
        # panel already shows whose shares these are, and "Voting group —
        # Anheuser-Busch InBev SA/NV" reads as though the group were a
        # subsidiary of it. Uniqueness is not needed — groups are found by their
        # roster (above) and excluded from name-based dedup — so the count can
        # sit in the name, where it is the most useful thing to say.
        name = _voting_group_name(len(roster))
        # The DISPLAY name says what the thing is and how big it is, and that is
        # all a reader wants on a row. But two nine-party groups over different
        # companies would then share a `name_normalized`, which is the field
        # `resolve_entity_id` matches on — so the matching key carries the
        # subject even though the label does not. Deliberately not
        # `normalize_entity_name(name)`: display and identity answer different
        # questions here, and conflating them is how one company's bloc would
        # resolve onto another's.
        norm = f"voting group {normalize_entity_name(subject_name) or subject_name.lower()}"
        # Searchable by the company too — "anheuser voting" should find it, and
        # a bare "Voting group · 9 parties" in a result list says nothing about
        # whose shares are involved.
        search_text = f"{name} {subject_name}"

        if best:
            # Same agreement, new roster: parties join and leave, and the node
            # should follow rather than fork. The name follows the count.
            session.run("""MATCH (g:Entity {id: $id})
                           SET g.member_keys = $roster, g.name = $name,
                               g.name_normalized = $norm, g.search_text = $stext,
                               g.last_scraped_at = $now""",
                        id=best, roster=roster, name=name, norm=norm,
                        stext=search_text, now=now)
            return best
        gid = str(uuid.uuid4())
        session.run(
            f"""CREATE (g:Entity {{
                    id: $id, name: $name, name_normalized: $norm, search_text: $stext,
                    type: '{VOTING_GROUP_TYPE}', member_keys: $roster,
                    source_id: $src, verified: false, last_scraped_at: $now,
                    description: $desc
                }})""",
            id=gid, name=name, norm=norm, stext=search_text,
            roster=roster, src=source_id, now=now,
            desc=(f"Parties acting together under a shareholders' or voting agreement "
                  f"reported to the SEC on Schedule 13D concerning {subject_name}."))
        return gid


def _upsert_group_membership(member_id: str, group_id: str, member_label: str,
                             source_id: str) -> None:
    """A party belongs to a filing group. NOT ownership — the same shape and the
    same reasoning as `_upsert_affiliate` below, which already models 13F fund
    groups as `RELATED_TO` rather than inventing an ownership edge."""
    with db.get_session() as session:
        session.run(
            f"""MATCH (m:{member_label} {{id: $mid}}), (g:Entity {{id: $gid}})
                MERGE (m)-[r:RELATED_TO {{relation: 'group_member'}}]->(g)
                SET r.source_id = $src, r.last_scraped_at = $now""",
            mid=member_id, gid=group_id, src=source_id, now=_now_iso())


def _retire_superseded_bloc_edge(filer_id: str, subject_id: str, source_id: str,
                                 filer_label: str) -> None:
    """Delete the filer's own bloc edge, now that the group carries it.

    Only an edge with no stake: the bloc rows are exactly the ones written with
    `stake_percent` null (a group member can rarely dispose of anything alone),
    while a member that also reports a real holding of its own keeps it.
    """
    with db.get_session() as session:
        session.run(
            f"""MATCH (a:{filer_label} {{id: $fid}})-[r:OWNS]->(b:Entity {{id: $sid}})
                WHERE r.source_id = $src AND r.stake_percent IS NULL
                DELETE r""",
            fid=filer_id, sid=subject_id, src=source_id)


def _upsert_owns_sec(owner_id: str, owned_id: str, source_id: str,
                     ownership_type: str, file_date: str | None,
                     stake_percent: float | None, source_url: str | None = None,
                     owner_label: str = "Entity", credibility_score: int = 98,
                     until: str | None = None, voting_power_pct: float | None = None,
                     share_class: str | None = None,
                     shares: int | None = None,
                     shares_outstanding: int | None = None,
                     voting_shares: int | None = None,
                     value_usd: float | None = None,
                     filing_type: str | None = None):
    """Create or update an OWNS edge with SEC EDGAR attribution.

    Provenance stamped per-entry: source_url = the specific SEC filing document,
    source_date = the filing date, last_scraped_at = now. On a re-scrape of an
    existing edge we refresh last_scraped_at so the UI can show when we last
    confirmed the fact against the source.

    Endpoints are labelled (owner is Entity or Person, owned always Entity) so
    the id lookups use the index — a label-less two-node match full-scans every
    node (~14s on 3M) per edge.

    ``until`` records a holding that has already ended — a 13D/13G filer that
    later amended to 0% has dropped below the 5% threshold, so the stake is
    history rather than a current position. An active edge for the same pair is
    closed rather than duplicated; with no active edge the closed one is written
    directly, so re-reading old filings still builds the timeline.
    """
    if owner_id == owned_id:
        # A company cannot own itself, and the graph had nine that did — Apple,
        # Microsoft and Alphabet all "holding" 7.48% of themselves, which is
        # Vanguard's stake in each. So a filer's holding was being attributed to
        # the issuer, i.e. two different companies resolved to one node. Refused
        # loudly rather than dropped: the write is the symptom, the resolution is
        # the disease, and a silent skip would hide it again.
        log.warning("SEC: refusing a self-owning edge on %s (stake %s) — the filer "
                    "and the issuer resolved to the same node", owner_id, stake_percent)
        return
    ownership_type = coherent_ownership_type(stake_percent, ownership_type)
    record_claim(kind=KIND_OWNS, from_id=owner_id, to_id=owned_id, source_id=source_id,
                 stake_percent=stake_percent, ownership_type=ownership_type,
                 voting_power_pct=voting_power_pct,
                 share_class=share_class, shares=shares,
                 shares_outstanding=shares_outstanding, voting_shares=voting_shares,
                 since=file_date, until=until, source_url=source_url,
                 source_date=file_date, credibility_score=credibility_score,
                 filing_type=filing_type)
    # Claims-only sources assert but do not draw (see sources.edge_writes_suppressed).
    from app.scraper.sources import edge_writes_suppressed
    if edge_writes_suppressed(source_id):
        return None
    owner_label = owner_label if owner_label in ("Entity", "Person") else "Entity"
    now = datetime.now(timezone.utc).isoformat()
    # The full schema bag: every OWNS property, None where this filing did not
    # say. The CREATE clause is generated from the same schema, so the write
    # can never carry a property the merges do not know, and vice versa.
    bag = owns_props(
        stake_percent=stake_percent, voting_power_pct=voting_power_pct,
        ownership_type=ownership_type, since=file_date, until=until,
        source_id=source_id, credibility_score=credibility_score,
        source_url=source_url, source_date=file_date, last_scraped_at=now,
        share_class=share_class, shares=shares,
        shares_outstanding=shares_outstanding, voting_shares=voting_shares,
        value_usd=value_usd, filing_type=filing_type,
        stale=False,
    )
    create_clause = edge_create_clause(OWNS_PROPS)
    # Closing an edge has to match one that is ALREADY closed too, or re-reading
    # the same filings creates a second historical edge every run — the active-only
    # match never finds the one written last time.
    active_only = "AND r.until IS NULL" if until is None else ""
    with db.get_session() as session:
        existing = session.run(
            f"""
            MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
            WHERE r.source_id = $sid {active_only}
            RETURN r LIMIT 1
            """,
            oid=owner_id, nid=owned_id, sid=source_id,
        ).single()
        if existing:
            # Refresh last_scraped_at and backfill the specific record URL/date
            # onto edges created before provenance (COALESCE keeps existing
            # values when this scrape didn't yield a URL). When `until` is given
            # the same statement closes the edge, so a holding that has since
            # been exited stops showing as current.
            session.run(
                f"""
                MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
                WHERE r.source_id = $sid {active_only}
                SET r.last_scraped_at = $now,
                    r.until       = $until,
                    r.stale       = false,
                    r.stake_percent    = $stake,
                    r.voting_power_pct = $vote,
                    r.share_class      = $sclass,
                    r.shares           = COALESCE($shares, r.shares),
                    r.shares_outstanding = COALESCE($shtotal, r.shares_outstanding),
                    r.voting_shares    = COALESCE($vshares, r.voting_shares),
                    r.value_usd        = COALESCE($vusd, r.value_usd),
                    r.filing_type      = COALESCE($ftype, r.filing_type),
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date)
                """,
                oid=owner_id, nid=owned_id, sid=source_id, now=now,
                surl=source_url, sdate=file_date, until=until,
                stake=stake_percent, vote=voting_power_pct, sclass=share_class,
                shares=shares, shtotal=shares_outstanding, vshares=voting_shares,
                vusd=value_usd, ftype=filing_type,
            )
            return
        session.run(
            f"""
            MATCH (a:{owner_label} {{id: $oid}}), (b:Entity {{id: $nid}})
            CREATE (a)-[:OWNS {{{create_clause}}}]->(b)
            """,
            oid=owner_id, nid=owned_id, **bag,
        )


def _upsert_role_sec(person_id: str, entity_id: str, role: str,
                     source_id: str, source_url: str | None = None,
                     source_date: str | None = None, credibility_score: int = 98):
    """Create a HAS_ROLE edge attributed to SEC EDGAR if not already present.

    Provenance: source_url = the specific Form 3/4 filing document,
    source_date = its filing date. On a re-scrape of an existing edge we refresh
    last_scraped_at and backfill the URL/date (COALESCE keeps existing values
    when this scrape didn't yield them).
    """
    record_claim(kind=KIND_ROLE, from_id=person_id, to_id=entity_id, source_id=source_id,
                 role=role, source_url=source_url, source_date=source_date,
                 credibility_score=credibility_score)
    from app.scraper.sources import edge_writes_suppressed
    if edge_writes_suppressed(source_id):
        return None
    now = datetime.now(timezone.utc).isoformat()
    with db.get_session() as session:
        existing = session.run(
            """
            MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
            WHERE r.role = $role AND r.until IS NULL
            RETURN r LIMIT 1
            """,
            pid=person_id, eid=entity_id, role=role,
        ).single()
        if existing:
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role AND r.until IS NULL
                SET r.last_scraped_at = $now,
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date)
                """,
                pid=person_id, eid=entity_id, role=role, now=now,
                surl=source_url, sdate=source_date,
            )
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: null, until: null,
                source_id: $sid, credibility_score: $score,
                source_url: $surl, source_date: $sdate, last_scraped_at: $now
            }]->(e)
            """,
            pid=person_id, eid=entity_id, role=role,
            sid=source_id, score=credibility_score,
            surl=source_url, sdate=source_date, now=now,
        )


def mark_13f_stale(company_id: str, period: str) -> int:
    """Dim the 13F holder edges the quarter just ingested did not confirm.

    A 13F seller never states an exit — the position simply vanishes from the
    manager's next filing, and silence is not a statement. So an edge whose
    ``source_date`` (the latest filing period — ``since`` deliberately keeps
    the FIRST-seen quarter for the timeline, only source_date moves on
    refresh) predates the period just read is flagged ``stale`` (dimmed in
    the panel), never closed: writing ``until`` would assert an end date
    nobody filed. One direction only — the upsert already sets
    ``stale = false`` on every edge the new quarter touched, so a filer that
    reappears heals itself.
    """
    with db.get_session() as session:
        rows = list(session.run(
            """MATCH (a)-[r:OWNS]->(b:Entity {id: $id})
               WHERE r.filing_type = '13F' AND r.until IS NULL
                 AND r.source_date < $period
                 AND COALESCE(r.stale, false) = false
               RETURN a.id AS aid""",
            id=company_id, period=period))
        for r in rows:
            session.run(
                """MATCH (a {id: $a})-[r:OWNS]->(b:Entity {id: $b})
                   WHERE r.filing_type = '13F' AND r.until IS NULL
                     AND r.source_date < $period
                   SET r.stale = true""",
                a=r["aid"], b=company_id, period=period)
    return len(rows)
