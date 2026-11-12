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

from app.roles import canonical_role, combined_start, end_seats_at_death
from app.claims import KIND_OWNS, KIND_ROLE, claims_for, close_role_claim, record_claim
from app.database import db
from app.db.anchors import label_or_entity
from app.scraper.edge_schema import OWNS_PROPS, READ_FIELD, STALE_CLEAR, STALE_CLEAR_CYPHER, STALE_CLEAR_SQL, edge_create_clause, owns_props
from app.scraper.graph_writer import _now_iso, _record_edge_parties, _relabel_if_more_credible, _same_seat
from app.scraper.mapper import coherent_ownership_type, normalize_entity_name
from app.scraper.owns_merge import ANSWER_FIELDS, SINCE_FIELDS, combine_since, outranks

log = logging.getLogger(__name__)

#: A filing group is not a legal person: it holds no LEI, has no country, and
#: cannot be geocoded or deduplicated like a company. Since 2026-10-26 it is
#: its own vertex type, `VotingGroup`, so nothing that assumes an Entity is a
#: legal organisation has to filter it out (eight queries did; the rest
#: forgot). `type` on the node is the API's node kind — what the client draws
#: and labels by — and reads `voting_group` on every VotingGroup.
VOTING_GROUP_TYPE = "voting_group"

#: Item 8's answer for a group member, so the writer classifies it from what
#: the filer stated rather than from its name (None: the name decides).
from app.scraper.sec_edgar import _individual_by_code  # noqa: E402,F401


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


#: what a 13D filing group is (findings item 103, the user's choice of
#: 2026-11-11): a voting pact names an agreement over the votes; every other
#: joint statement is one holder with its affiliates filing together
VOTING_PACT, JOINT_FILING = "voting_pact", "joint_filing"


def _voting_group_name(size: int, kind: str = VOTING_PACT, filer: str | None = None) -> str:
    """What a filing group is called: what it is, and how many are in it — a
    joint filing by its filer ("Joint filing · Berkshire Hathaway Inc. and 7
    affiliates")."""
    if kind == JOINT_FILING and filer:
        others = size - 1
        return f"Joint filing · {filer}" + (f" and {others} affiliate{'s' if others != 1 else ''}" if others else "")
    return f"Voting group · {size} parties" if size != 1 else "Voting group · 1 party"


def _upsert_voting_group(subject_id: str, subject_name: str, roster: list,
                         source_id: str, kind: str = VOTING_PACT, filer: str | None = None) -> str:
    """The node standing for a 13D filing group, found by its roster.

    Deliberately not routed through `_upsert_entity_by_name`: that resolves on
    `name_normalized`, so two groups whose names normalise alike would silently
    become one. Identity here is the set of parties, matched by overlap against
    the groups already pointing at this same subject.
    """
    now = _now_iso()
    with db.get_session() as session:
        existing = list(session.run(
            """MATCH (g:VotingGroup)-[:OWNS]->(s:Entity {id: $sid})
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
        name = _voting_group_name(len(roster), kind, filer)
        # The DISPLAY name says what the thing is and how big it is, and that is
        # all a reader wants on a row; the matching key carries the subject, so
        # two nine-party groups over different companies never share one
        # (search's exact-name path reads it). Deliberately not
        # `normalize_entity_name(name)`: display and identity answer different
        # questions here.
        norm = f"voting group {normalize_entity_name(subject_name) or subject_name.lower()}"
        # Searchable by the company too — "anheuser voting" should find it, and
        # a bare "Voting group · 9 parties" in a result list says nothing about
        # whose shares are involved.
        search_text = f"{name} {subject_name}"

        if best:
            # Same agreement, new roster: parties join and leave, and the node
            # should follow rather than fork. The name follows the count.
            session.run("""MATCH (g:VotingGroup {id: $id})
                           SET g.member_keys = $roster, g.name = $name,
                               g.name_normalized = $norm, g.search_text = $stext,
                               g.kind = $kind, g.last_scraped_at = $now""",
                        id=best, roster=roster, name=name, norm=norm,
                        stext=search_text, kind=kind, now=now)
            return best
        gid = str(uuid.uuid4())
        session.run(
            f"""CREATE (g:VotingGroup {{
                    id: $id, name: $name, name_normalized: $norm, search_text: $stext,
                    type: '{VOTING_GROUP_TYPE}', kind: $kind, member_keys: $roster,
                    source_id: $src, last_scraped_at: $now,
                    created_at: $now, description: $desc
                }})""",
            id=gid, name=name, norm=norm, stext=search_text, kind=kind,
            roster=roster, src=source_id, now=now,
            desc=(f"Parties acting together under a shareholders' or voting agreement "
                  f"reported to the SEC on Schedule 13D concerning {subject_name}." if kind == VOTING_PACT else
                  f"{filer or 'A holder'} and its affiliates, reporting one holding of {subject_name} "
                  f"together on Schedule 13D — no agreement over the votes is stated."))
        return gid


def _upsert_group_membership(member_id: str, group_id: str, member_label: str,
                             source_id: str) -> None:
    """A party belongs to a filing group: `MEMBER_OF`, its own edge type
    since 2026-10-26 (RELATED_TO {relation: 'group_member'} before, beside
    the 13F affiliates). NOT ownership — the bloc's holding is the group's."""
    with db.get_session() as session:
        session.run(
            f"""MATCH (m:{member_label} {{id: $mid}}), (g:VotingGroup {{id: $gid}})
                MERGE (m)-[r:MEMBER_OF]->(g)
                SET r.source_id = $src, r.last_scraped_at = $now""",
            mid=member_id, gid=group_id, src=source_id, now=_now_iso())


def _prune_group_membership(group_id: str, keep_ids: set[str]) -> int:
    """Drop the membership of every party a newer filing no longer names.
    `_upsert_voting_group` replaces the roster (and the name, "8 parties")
    with the filing's, but the lines of parties who left stayed: Symbotic's
    group was named for 8 and listed 12 (2026-10-26). `keep_ids` are the
    nodes this filing joined to the group. Returns the lines removed."""
    removed = 0
    with db.get_session() as session:
        rows = list(session.run(
            """MATCH (m)-[r:MEMBER_OF]->(g:VotingGroup {id: $gid})
               RETURN m.id AS mid, labels(m)[0] AS mlabel""", gid=group_id))
        for r in rows:
            if r["mid"] in keep_ids:
                continue
            session.run(f"""MATCH (m:{label_or_entity(r.get("mlabel"))} {{id: $mid}})
                                   -[r:MEMBER_OF]->(g:VotingGroup {{id: $gid}}) DELETE r""",
                        mid=r["mid"], gid=group_id)
            removed += 1
    return removed


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
                  AND r.until IS NULL
                  AND (r.filing_type IS NULL OR r.filing_type STARTS WITH '13D')
                DELETE r""",
            fid=filer_id, sid=subject_id, src=source_id)


BENEFICIAL_OWNERSHIP_FORMS = ("13D", "13G")


def is_beneficial_ownership_filing(filing_type: str | None) -> bool:
    """A Schedule 13D or 13G, original or amendment, however the type is spelt
    (`13G/A`, `SC 13D`, `SCHEDULE 13G/A`): the holder's own statement of
    beneficial ownership."""
    ft = (filing_type or "").upper().replace("SCHEDULE ", "").replace("SC ", "").strip()
    return ft.startswith(BENEFICIAL_OWNERSHIP_FORMS)


def stated_by_13dg(owner_id: str, owned_id: str, source_id: str) -> bool:
    """Whether this source holds the pair as a 13D/G statement that has not
    ended — in which case a Form 4 count must not overwrite it.

    Of record versus beneficial: a Form 4 reports what is registered to the
    insider, a 13D/G what they beneficially own — voting and dispositive power,
    the number the SEC's own 5 % and 10 % lines are measured against, with the
    filer's disclaimers applied. Musk's June 2026 Form 4 leaves him 1,123 M
    Tesla shares (28.4 %); his 13G/A of the same day states 699.6 M (19.9 %),
    excluding the 423.7 M restricted shares of the 2025 CEO award that sit
    under an irrevocable proxy to Tesla's secretary until they vest in 2033.
    The Form 4 reader cannot read that footnote; the 13D/G already has. So
    where the holder has filed a 13D/G, that is the stake, and the Form 4 sets
    it only for insiders who file none. The 13D/G is the whole statement —
    count, percentage, date — because a Form 4 count beside a 13D/G percentage
    would not add up (`shares` / `shares_outstanding` ≠ `stake_percent`).
    A 13D/G that reported an exit (`until`) no longer speaks for the pair.

    The issuer's own ownership table states beneficial ownership too: a
    holding `write_table_holders` wrote from it (a table claim WITH a stake —
    the voting-power claim `proxy_write` records has none) is kept the same
    way (findings item 86).
    """
    return any(c.get("source_id") == source_id and not c.get("until")
               and (is_beneficial_ownership_filing(c.get("filing_type"))
                    or (is_table_filing(c.get("filing_type")) and c.get("stake_percent") is not None))
               for c in claims_for(from_id=owner_id, to_id=owned_id, kind=KIND_OWNS))


#: the filing types of a holding read from the issuer's ownership table — its
#: proxy, or a 10-K/A whose Part III stands in for it (= `proxy_statement.TABLE_FORMS`)
TABLE_FILING_TYPES = frozenset({"DEF 14A", "10-K/A"})


def is_table_filing(filing_type: str | None) -> bool:
    """A holding read from the issuer's ownership table (findings item 86)."""
    return (filing_type or "").strip().upper() in TABLE_FILING_TYPES


def _write_owns_sec(owner_id: str, owned_id: str, source_id: str,
                     ownership_type: str, file_date: str | None,
                     stake_percent: float | None, source_url: str | None = None,
                     owner_label: str = "Entity", credibility_score: int = 98,
                     until: str | None = None, voting_power_pct: float | None = None,
                     share_class: str | None = None,
                     shares: int | None = None,
                     shares_outstanding: int | None = None,
                     denominator_date: str | None = None,
                     event_date: str | None = None,
                     voting_shares: int | None = None,
                     value_usd: float | None = None,
                     filing_type: str | None = None,
                     filing_dates_the_stake: bool = True,
                     direct_or_indirect: str | None = None,
                     structure_basis: str | None = None,
                     since_date: str | None = None,
                     since_basis: str | None = None,
                     read_from: str | None = None,
                     since_source_url: str | None = None):
    """Create or update an OWNS edge with SEC EDGAR attribution.

    ``read_from``: how reliably this filing's values were read
    (`edge_schema.READ_GRADES`) — a 13F's XML field, an Exhibit 21 table cell,
    a parent inferred from indentation, a percentage found in a cover page's
    text. Written on the claim and, when SEC holds the answer, on the edge; a
    re-read of the same filing by the same source replaces it (the new read's
    grade is the edge's).

    ``since_date`` / ``since_basis``: the start the caller worked out from the
    filing (`sec_edgar._stake_start`: an original 13D/G states it, an
    amendment only bounds it — basis ``amendment``). It wins over the
    ``filing_dates_the_stake`` default and is COMBINED with the start an edge
    already has — the earliest wins — so a re-read never moves it later.

    ``filing_dates_the_stake``: whether ``file_date`` says when the holding
    BEGAN. True for 13D/13G, which are due within days of crossing 5%. False
    for the list-style filings — an Exhibit 21/8.1 subsidiary list and a 13F
    quarterly holdings report state what is held AS OF a date, not since when:
    News Corp's FY2026 Exhibit 21 made all 200 of its subsidiaries look
    "acquired in 2026". Then the date is kept as the source/as-of date only
    and ``since`` stays empty (the timeline shows the holding as undated)
    until a real start date is known.

    Provenance stamped per-entry: source_url = the specific SEC filing document,
    source_date = the filing date, last_scraped_at = now. On a re-scrape of an
    existing edge we refresh last_scraped_at so the UI can show when we last
    confirmed the fact against the source.

    Endpoints are labelled (owner is Entity or Person, owned always Entity) so
    the id lookups use the index — a label-less two-node match full-scans every
    node (~14s on 3M) per edge.

    One active edge per pair: when another source already drew this pair, SEC
    takes over that edge (``_share_owns_edge``, rules in
    ``app.scraper.owns_merge``) instead of drawing its own beside it.

    ``direct_or_indirect`` / ``structure_basis``: where the filer SHOWED the
    subsidiary's place in the group (an Exhibit 21 indentation tree or a
    "Subsidiaries of X" heading — `sec_ex21.parse_exhibit`), the marker is
    written with its basis. A marker GLEIF stated (no basis) is never
    overwritten by one inferred from layout.

    ``until`` records a holding that has already ended — a 13D/13G filer that
    later amended to 0% has dropped below the 5% threshold, so the stake is
    history rather than a current position. An active edge for the same pair is
    closed rather than duplicated; with no active edge the closed one is written
    directly, so re-reading old filings still builds the timeline.
    """
    _record_edge_parties(owner_id, owned_id)
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
    if since_date:
        since, basis = since_date, since_basis
    else:
        since, basis = (file_date if filing_dates_the_stake else None), None
    record_claim(kind=KIND_OWNS, from_id=owner_id, to_id=owned_id, source_id=source_id,
                 stake_percent=stake_percent, ownership_type=ownership_type,
                 voting_power_pct=voting_power_pct,
                 share_class=share_class, shares=shares,
                 shares_outstanding=shares_outstanding, voting_shares=voting_shares,
                 denominator_date=denominator_date, event_date=event_date,
                 since=since, since_basis=basis, until=until, source_url=source_url,
                 source_date=file_date, credibility_score=credibility_score,
                 filing_type=filing_type, structure_basis=structure_basis,
                 read_from=read_from, since_source_url=since_source_url)
    # Claims-only sources assert but do not draw (see sources.edge_writes_suppressed).
    from app.scraper.sources import edge_writes_suppressed
    if edge_writes_suppressed(source_id):
        return None
    owner_label = label_or_entity(owner_label)        # Entity, Person or VotingGroup
    now = datetime.now(timezone.utc).isoformat()
    # The full schema bag: every OWNS property, None where this filing did not
    # say. The CREATE clause is generated from the same schema, so the write
    # can never carry a property the merges do not know, and vice versa.
    bag = owns_props(
        stake_percent=stake_percent, voting_power_pct=voting_power_pct,
        ownership_type=ownership_type, since=since, since_basis=basis, until=until,
        since_source_url=since_source_url if since else None,
        source_id=source_id, credibility_score=credibility_score,
        source_url=source_url, source_date=file_date, last_scraped_at=now,
        share_class=share_class, shares=shares,
        shares_outstanding=shares_outstanding, voting_shares=voting_shares,
        denominator_date=denominator_date, event_date=event_date,
        value_usd=value_usd, filing_type=filing_type,
        direct_or_indirect=direct_or_indirect, structure_basis=structure_basis,
        read_from=read_from, stale=False,
    )
    create_clause = edge_create_clause(OWNS_PROPS)
    # Closing an edge has to match one that is ALREADY closed too, or re-reading
    # the same filings creates a second historical edge every run — the active-only
    # match never finds the one written last time.
    #
    # But closing must find THE period it closes, not every period of the pair:
    # matching all of them moved an old spell's end to a new exit (present in
    # the gap between), and an old exit re-read closed a newer open spell with
    # an end before its start. So: the closed edge already carrying this end
    # (a re-read), else the open one — and never one that starts after it.
    if until is None:
        candidates = ["r.until IS NULL"]
    else:
        candidates = ["r.until = $until AND (r.since IS NULL OR r.since <= $until)",
                      "r.until IS NULL AND (r.since IS NULL OR r.since <= $until)"]
    with db.get_session() as session:
        existing, active_only = None, ""
        for cond in candidates:
            existing = session.run(
                f"""
                MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
                WHERE r.source_id = $sid AND {cond}
                RETURN r LIMIT 1
                """,
                oid=owner_id, nid=owned_id, sid=source_id, until=until,
            ).single()
            if existing:
                active_only = f"AND {cond}"
                break
        if not existing and until is None:
            # The spell the dissolution rule closed (app.dissolution) is still
            # the one a filing from before the end describes: re-read, it is
            # updated, not drawn again beside the closed one — once per scrape,
            # as the rebuild's Altaba re-reads would have. The update states
            # the filing's open end; the scrape scope closes it again
            # (graph_writer._end_dissolved_after_scrape).
            existing = _dissolution_closed(session, owner_label, owner_id, owned_id,
                                           since or file_date, source_id=source_id)
            if existing:
                active_only = f"AND r.until_reason = '{existing['r'].get('until_reason')}'"
        if existing:
            # The start: combined with what the edge has, earliest wins (an
            # amendment's lower bound never replaces an original's stated day).
            held = existing["r"]
            started = combine_since({f: held.get(f) for f in SINCE_FIELDS},
                                    {"since": since, "since_basis": basis,
                                     "since_source_url": since_source_url if since else None})
            # Refresh last_scraped_at and backfill the specific record URL/date
            # onto edges created before provenance (COALESCE keeps existing
            # values when this scrape didn't yield a URL). The stake and its
            # type move together: a re-read that corrects one corrects both
            # (a 0.0000003% co-holder written as "controlling" stayed so
            # through every re-read while this only reset the stake).
            #
            # ArcadeDB applies SET assignments in order, each seeing the
            # previous ones: the basis must be written BEFORE the marker, or
            # its condition sees the marker just set and leaves the basis
            # empty — an inferred marker that then reads as a stated one. When `until` is given
            # the same statement closes the edge, so a holding that has since
            # been exited stops showing as current.
            session.run(
                f"""
                MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
                WHERE r.source_id = $sid {active_only}
                SET r.last_scraped_at = $now,
                    r.until       = $until,
                    r.stale       = false, r.stale_since = null,
                    r.stale_reason = null, r.stale_source_url = null,
                    r.stake_percent    = $stake,
                    r.ownership_type   = $otype,
                    r.voting_power_pct = $vote,
                    r.share_class      = $sclass,
                    r.shares           = COALESCE($shares, r.shares),
                    r.shares_outstanding = COALESCE($shtotal, r.shares_outstanding),
                    r.denominator_date   = $ddate,
                    r.event_date         = $edate,
                    r.voting_shares    = COALESCE($vshares, r.voting_shares),
                    r.value_usd        = COALESCE($vusd, r.value_usd),
                    r.filing_type      = COALESCE($ftype, r.filing_type),
                    r.read_from        = $rfrom,
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date),
                    r.since            = $csince,
                    r.since_basis      = $cbasis,
                    r.since_source_url = $csurl,
                    r.structure_basis    = CASE WHEN $doi IS NOT NULL AND
                                                     (r.direct_or_indirect IS NULL OR r.structure_basis IS NOT NULL)
                                                THEN $sbasis ELSE r.structure_basis END,
                    r.direct_or_indirect = CASE WHEN $doi IS NOT NULL AND
                                                     (r.direct_or_indirect IS NULL OR r.structure_basis IS NOT NULL)
                                                THEN $doi ELSE r.direct_or_indirect END
                """,
                oid=owner_id, nid=owned_id, sid=source_id, now=now,
                surl=source_url, sdate=file_date, until=until,
                stake=stake_percent, otype=ownership_type,
                vote=voting_power_pct, sclass=share_class,
                shares=shares, shtotal=shares_outstanding, vshares=voting_shares,
                ddate=denominator_date, edate=event_date,
                vusd=value_usd, ftype=filing_type, rfrom=read_from,
                doi=direct_or_indirect, sbasis=structure_basis,
                csince=started["since"], cbasis=started["since_basis"],
                csurl=started["since_source_url"],
            )
            return
        # Another source already holds this pair: take over ITS edge rather than
        # drawing a second one beside it (see app.scraper.owns_merge).
        # An exit only ever ends a period that had begun by then — the pair's
        # open edge from 2020 is not what a 2015 exit closes; that one is
        # written as its own, ended period below.
        began = "" if until is None else "AND (r.since IS NULL OR r.since <= $until)"
        shared = session.run(
            f"""
            MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
            WHERE r.until IS NULL {began} RETURN r LIMIT 1
            """,
            oid=owner_id, nid=owned_id, until=until,
        ).single()
        if not shared and until is None:
            # another source's spell the dissolution rule closed — see above
            shared = _dissolution_closed(session, owner_label, owner_id, owned_id, since or file_date)
        if shared:
            _share_owns_edge(session, owner_label, owner_id, owned_id, shared["r"], bag, now)
            return
        session.run(
            f"""
            MATCH (a:{owner_label} {{id: $oid}}), (b:Entity {{id: $nid}})
            CREATE (a)-[:OWNS {{{create_clause}}}]->(b)
            """,
            oid=owner_id, nid=owned_id, **bag,
        )


def _dissolution_closed(session, owner_label: str, owner_id: str, owned_id: str,
                        dated: str | None, source_id: str | None = None):
    """The pair's edge a rule closed (`_RULE_CLOSED`: the dissolution rule,
    or the parent's statement taking over — findings item 34), when a filing
    dated `dated` falls inside it (no date: it does) — or None. The dates are
    compared in Python: Cypher coerces ISO strings."""
    by_source = "AND r.source_id = $sid" if source_id else ""
    for row in session.run(
            f"""
            MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
            WHERE r.until_reason IN $reasons {by_source} RETURN r
            """,
            oid=owner_id, nid=owned_id, sid=source_id, reasons=list(_RULE_CLOSED)):
        end = row["r"].get("until")
        if not dated or not end or str(dated)[:10] <= str(end)[:10]:
            return row
    return None


FINAL_REPORT = "final_report"


def _upsert_owns_sec(*args, final_report: str | None = None, **kwargs):
    """Create or update an OWNS edge with SEC EDGAR attribution — see
    `_write_owns_sec`, which takes the same arguments.

    ``final_report``: the day a 13D/G that is the filer's LAST report speaks
    for (Item 5 ticked, "5 percent or less" — findings item 35). The holding
    stays open with its stake, as the filing states it, but is dimmed from that
    day with `stale_reason = final_report`: the filer reports no more, and
    nothing says it sold. BlackRock, Inc.'s 13G/A on Tesla of 2026-10-27 (4.96 %,
    as of 2026-09-30) stood as a current holding. A later report that is not
    final clears the mark with the rest of the stale fields."""
    result = _write_owns_sec(*args, **kwargs)
    if final_report:
        import inspect
        bound = inspect.signature(_write_owns_sec).bind(*args, **kwargs)
        bound.apply_defaults()
        a = bound.arguments
        _mark_final_report(a["owner_id"], a["owned_id"], a["source_id"],
                           label_or_entity(a["owner_label"]), final_report,
                           a["file_date"], a["source_url"])
    return result


def _mark_final_report(owner_id: str, owned_id: str, source_id: str, owner_label: str,
                       day: str, file_date: str | None, source_url: str | None) -> None:
    """Dim the pair's open SEC edge as the filer's final report — only when
    the edge carries THIS filing (its `source_date`); a newer statement already
    on it is the current word. Dates are compared in Python."""
    with db.get_session() as session:
        for row in session.run(
                f"""MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
                    WHERE r.source_id = $sid AND r.until IS NULL RETURN r.source_date AS sd""",
                oid=owner_id, nid=owned_id, sid=source_id):
            if file_date and str(row.get("sd") or "")[:10] != str(file_date)[:10]:
                return
        session.run(
            f"""MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
                WHERE r.source_id = $sid AND r.until IS NULL
                SET r.stale = true, r.stale_reason = $why, r.stale_since = $day,
                    r.stale_source_url = $url""",
            oid=owner_id, nid=owned_id, sid=source_id, why=FINAL_REPORT,
            day=str(day)[:10], url=source_url)


#: why a holding's spell ended because its parent's statement took over
#: (`close_reported_by_parent`)
REPORTED_BY_PARENT = "reported_by_parent"
#: why a holding's spell ended because the shares it is about ceased to exist
#: (`close_at_class_end`, findings item 99)
CLASS_ENDED = "class_ended"
#: why a holding's spell ended because the holder is the issuer's own
#: subsidiary (`close_held_by_subsidiary`, findings item 99 part 3)
HOLDER_IS_SUBSIDIARY = "holder_is_subsidiary"
#: the statements that say a company is another's subsidiary
_SUBSIDIARY_STATEMENTS = ("EX-21", "EX-8.1", "RR")
#: a statement of at least this much is the acquirer's own: one holder of the
#: whole class is what a takeover leaves, and its holding goes on
WHOLE_CLASS_PCT = 99.5
#: spells closed by a rule rather than by a filing: a re-read of a filing from
#: inside one updates it instead of drawing a new open edge beside it, and the
#: rule closes it again after the scrape (app.dissolution, the parent rule,
#: the class end, the issuer's table)
#: why a holding's spell ended because the issuer's own ownership table no
#: longer names the holder (`close_absent_from_table`, findings item 99)
NOT_IN_TABLE = "not_in_issuer_table"
_RULE_CLOSED = ("dissolution", "merged", "split", REPORTED_BY_PARENT, CLASS_ENDED, NOT_IN_TABLE)


def _statement_day(edge: dict) -> str:
    """The day a 13D/G statement was made: its filing date."""
    return str(edge.get("source_date") or "")[:10]


def close_reported_by_parent(owned_id: str | None = None, parent_id: str | None = None) -> int:
    """Close a subsidiary's 13D/G holding once its parent reports the same
    company in a newer 13D/G of its own (findings item 34).

    A parent holding company files for its subsidiaries' shares (Item 7 / type
    HC): BlackRock, Inc. (CIK 0002012383, the parent since the 2024 holding
    reorganisation) reported DaVita on 2025-04-25; the old parent, CIK
    0001364742 — renamed BlackRock Finance, Inc. and listed in the new parent's
    Exhibit 21 — had reported it on 2024-01-31. A newer statement replaces only
    the same filer's older one, so DaVita listed both (5.2 % + 5.58 %), and 32
    companies on dev did.

    The subsidiary is S when the parent P holds an open CONTROLLING edge to it
    (Exhibit 21, 8.1, GLEIF) — a minority stake in S is no group. S's edge to X
    ends when P's 13D/G statement on X is filed after S's: on P's `since` when
    that falls after S's statement (P's first report), else on P's statement
    day; `until_reason` REPORTED_BY_PARENT, no stale mark. Untouched: a
    statement S filed after P's (Vanguard Capital Management, 2026-04, after
    the Vanguard Group's 0 % of 2026-03 — the disaggregated filer is the
    current word) and one of the same day (a joint filing naming both,
    Berkshire and National Indemnity). A 13F is another rulebook and is not
    compared. S's claim stays: S did say it.

    Scope: the owners of ``owned_id``, the companies ``parent_id``'s
    subsidiaries report, or the whole graph (neither). Returns how many
    holdings it closed. Dates are compared in Python (Cypher coerces ISO
    strings)."""
    from app.scraper.maintenance import restatement_family
    sid = "sec-edgar"
    with db.get_session() as session:
        if owned_id:
            targets = [owned_id]
        elif parent_id:
            targets = sorted({r["x"] for r in session.run(
                """MATCH (p:Entity {id: $p})-[c:OWNS]->(s:Entity)-[b:OWNS]->(x:Entity)
                   WHERE c.until IS NULL AND c.ownership_type = 'controlling'
                     AND b.until IS NULL AND b.source_id = $sid
                   RETURN DISTINCT x.id AS x""", p=parent_id, sid=sid)})
        else:
            targets = sorted({r["x"] for r in session.run(
                """MATCH (s:Entity)-[b:OWNS]->(x:Entity)
                   WHERE b.until IS NULL AND b.source_id = $sid AND b.filing_type IS NOT NULL
                   RETURN x.id AS x, count(b) AS n""", sid=sid) if r["n"] > 1})
        closed = 0
        for x in targets:
            held = {r["s"]: dict(r["r"]) for r in session.run(
                """MATCH (s:Entity)-[r:OWNS]->(x:Entity {id: $x})
                   WHERE r.until IS NULL AND r.source_id = $sid AND r.filing_type IS NOT NULL
                   RETURN s.id AS s, r""", x=x, sid=sid)
                if restatement_family(r["r"].get("filing_type"))}
            if len(held) < 2:
                continue
            for s_id, mine in held.items():
                parents = [r["p"] for r in session.run(
                    """MATCH (p:Entity)-[c:OWNS]->(s:Entity {id: $s})
                       WHERE c.until IS NULL AND c.ownership_type = 'controlling'
                       RETURN DISTINCT p.id AS p""", s=s_id)]
                newer = [held[p] for p in parents
                         if p in held and p != s_id and _statement_day(held[p]) > _statement_day(mine)]
                if not newer:
                    continue
                theirs = min(newer, key=_statement_day)
                start = str(theirs.get("since") or "")[:10]
                end = start if start and start > _statement_day(mine) else _statement_day(theirs)
                session.run(
                    """MATCH (s:Entity {id: $s})-[r:OWNS]->(x:Entity {id: $x})
                       WHERE r.until IS NULL AND r.source_id = $sid
                       SET r.until = $end, r.until_reason = $why, """ + STALE_CLEAR_CYPHER,
                    s=s_id, x=x, sid=sid, end=end, why=REPORTED_BY_PARENT)
                log.info("SEC: %s's 13D/G holding of %s ends %s — reported by its parent since",
                         s_id, x, end)
                closed += 1
            # a spell this rule closed that a re-read reopened, and that no
            # parent statement replaces any longer, carries no stale reason
            session.run(
                """MATCH (s:Entity)-[r:OWNS]->(x:Entity {id: $x})
                   WHERE r.until IS NULL AND r.until_reason = $why
                   SET r.until_reason = null""", x=x, why=REPORTED_BY_PARENT)
    return closed


def close_at_class_end(owned_id: str, ends: list[dict]) -> int:
    """End the 13D/G holdings of `owned_id` whose shares ceased to exist
    (findings item 99): every open SEC statement filed before a class end of
    `sec_class_end.class_ends` closes on that day, `until_reason` CLASS_ENDED,
    `until_source_url` the Form 25 / 15, no stale mark — SoftBank's 83.8 % of
    Sprint on 2020-04-01, LinkedIn's holders on 2016-12-08.

    Kept: a statement filed after the end (a holding of the successor class,
    or a later class of the same issuer), and one stating at least
    WHOLE_CLASS_PCT — the acquirer's (Roche's 100 % of Foundation Medicine).
    An acquirer's EARLIER, partial statement does end (eBay's 28.1 % of
    PayPal, 2002): the figure it stated ended with the class, and the whole
    holding is the acquirer's Exhibit 21's or GLEIF's to state. A 13F is
    another rulebook and is not touched. The claim stays: the filer did say
    it. Returns how many holdings it closed. Dates compared in Python."""
    from app.db.arcadedb import run_sql
    from app.scraper.maintenance import restatement_family
    if not ends:
        return 0
    ends = sorted(ends, key=lambda e: e["date"])
    closed = 0
    for e in run_sql("SELECT @rid AS rid, filing_type, source_date, since, stake_percent FROM OWNS "
                     "WHERE until IS NULL AND source_id = 'sec-edgar' AND @in.id = :id",
                     {"id": owned_id}) or []:
        if not restatement_family(e.get("filing_type")):
            continue
        if isinstance(e.get("stake_percent"), (int, float)) and e["stake_percent"] >= WHOLE_CLASS_PCT:
            continue
        day = _statement_day(e) or str(e.get("since") or "")[:10]
        end = next((x for x in ends if day and x["date"] > day), None)
        if end is None:
            continue
        run_sql(f"UPDATE {e['rid']} SET until = :d, until_reason = :why, until_source_url = :u, "
                + STALE_CLEAR_SQL,
                {"d": end["date"], "why": CLASS_ENDED, "u": end["url"]})
        log.info("SEC: a 13D/G holding of %s ends %s — its class ended (%s)", owned_id, end["date"], end["form"])
        closed += 1
    return closed


def close_held_by_subsidiary(issuer_id: str | None = None) -> list[str]:
    """End a 13D/G holding whose holder is now the issuer's own subsidiary
    (findings item 99 part 3, the user's word of 2026-11-11): Texaco's 13D of
    2000-10-15 on Chevron, 16.5 % — filed with the merger agreement, Texaco a
    Chevron subsidiary since 2001 — stayed open and drew a two-company cycle.
    The evidence is the issuer's own subsidiary statement about the holder (its
    Exhibit 21 / 8.1, GLEIF's relationship record), live; a statement filed
    BEFORE it ends on that statement's date, `until_reason`
    HOLDER_IS_SUBSIDIARY, `until_source_url` the list — an upper bound: the
    holding as an outside shareholder ended by then. A statement filed after the
    list (a subsidiary that holds its parent's shares now) is kept, so is a 13F
    (another rulebook). ``issuer_id`` scopes the pass to one company; without it
    the whole graph. Returns a line per closed holding. Dates compared in Python."""
    from app.db.arcadedb import run_command, run_query
    from app.scraper.maintenance import restatement_family
    rows = run_query(
        "MATCH (h:Entity)-[r:OWNS]->(c:Entity)-[s:OWNS]->(h) "
        "WHERE r.until IS NULL AND r.source_id = 'sec-edgar' AND s.until IS NULL "
        "AND (s.filing_type IN $subs OR s.source_id = 'gleif') "
        + ("AND c.id = $cid " if issuer_id else "")
        + "RETURN h.id AS h, c.id AS c, h.name AS hn, c.name AS cn, r.filing_type AS ft, "
          "r.source_date AS source_date, r.since AS since, s.source_date AS list_day, s.since AS sub_since, "
          "s.source_url AS list_url, s.filing_type AS list_ft",
        {"subs": list(_SUBSIDIARY_STATEMENTS), "cid": issuer_id}) or []
    lines: list[str] = []
    for e in rows:
        if not restatement_family(e.get("ft")):
            continue
        day = _statement_day(e) or str(e.get("since") or "")[:10]
        # a stated start of the subsidiary relation is the better date
        end = str(e.get("sub_since") or e.get("list_day") or "")[:10]
        if not day or not end or day >= end:
            continue
        run_command(
            "MATCH (h:Entity {id: $h})-[r:OWNS]->(c:Entity {id: $c}) "
            "WHERE r.until IS NULL AND r.source_id = 'sec-edgar' AND r.filing_type = $ft "
            "SET r.until = $end, r.until_reason = $why, r.until_source_url = $url, " + STALE_CLEAR_CYPHER,
            {"h": e["h"], "c": e["c"], "ft": e["ft"], "end": end, "why": HOLDER_IS_SUBSIDIARY,
             "url": e.get("list_url")})
        lines.append(f"{e.get('hn')} → {e.get('cn')} ({e['ft']} {day}) ends {end}: "
                     f"{e.get('cn')}'s {e.get('list_ft')} names it a subsidiary")
        log.info("SEC: %s", lines[-1])
    return lines


def close_absent_from_tables(limit: int | None = None) -> dict:
    """`read_table_and_close` for every company with an SEC CIK that open
    13D/G holdings point at — the whole graph (see `close_ended_classes` for
    why). Returns {companies, tables, read, failed, closed, lines}."""
    from app.db.arcadedb import run_sql
    from app.scraper.maintenance import restatement_family
    rows = run_sql("SELECT @in.id AS id, @in.sec_cik AS cik, @in.name AS name, filing_type FROM OWNS "
                   "WHERE until IS NULL AND source_id = 'sec-edgar' AND @in.sec_cik IS NOT NULL") or []
    issuers = sorted({(r["id"], r["cik"], r.get("name")) for r in rows
                      if restatement_family(r.get("filing_type")) or is_table_filing(r.get("filing_type"))})
    if limit:
        issuers = issuers[:limit]
    out: dict = {"companies": len(issuers), "tables": 0, "read": 0, "failed": 0, "closed": 0, "lines": []}
    for eid, cik, name in issuers:
        try:
            res = read_table_and_close(eid, cik)
        except Exception as exc:  # noqa: BLE001 — one company's filing lost is not the run's
            out["failed"] += 1
            log.warning("SEC: ownership table of %s (CIK %s) not read: %s", name, cik, exc)
            continue
        out["read"] += 1
        if res["filing"]:
            out["tables"] += 1
        if res["closed"]:
            out["closed"] += res["closed"]
            out["lines"].append(f"{name}: {res['closed']} closed — not in its {res['filing']['form']} "
                                f"of {res['filing']['date']} ({res['rows']} holders listed)")
    return out


def close_ended_classes(limit: int | None = None) -> dict:
    """`close_at_class_end` for every company with an SEC CIK that open
    13D/G holdings point at — the whole graph, for a rebuild or a server
    where holder-side scrapes (BlackRock's, SoftBank's own filings) wrote into
    companies nobody scrapes. One submissions request per company plus one per
    Form 15 / 25 it lists. Returns {companies, read, failed, closed, lines}."""
    from app.db.arcadedb import run_sql
    from app.scraper.sec_class_end import class_ends
    rows = run_sql("SELECT @in.id AS id, @in.sec_cik AS cik, @in.name AS name, filing_type FROM OWNS "
                   "WHERE until IS NULL AND source_id = 'sec-edgar' AND @in.sec_cik IS NOT NULL") or []
    from app.scraper.maintenance import restatement_family
    issuers = sorted({(r["id"], r["cik"], r.get("name")) for r in rows if restatement_family(r.get("filing_type"))})
    if limit:
        issuers = issuers[:limit]
    out: dict = {"companies": len(issuers), "read": 0, "failed": 0, "closed": 0, "lines": []}
    for eid, cik, name in issuers:
        try:
            ends = class_ends(cik)
        except Exception as exc:  # noqa: BLE001 — one company's list lost is not the run's
            out["failed"] += 1
            log.warning("SEC: class ends of %s (CIK %s) not read: %s", name, cik, exc)
            continue
        out["read"] += 1
        n = close_at_class_end(eid, ends)
        if n:
            out["closed"] += n
            out["lines"].append(f"{name}: {n} closed on {ends[0]['date']} ({ends[0]['form']}, {ends[0]['class']})"
                                if len(ends) == 1 else f"{name}: {n} closed ({len(ends)} class ends)")
    return out


#: words that say nothing about WHICH holder a name is
_NAME_STOP = frozenset({"the", "and", "of", "inc", "llc", "lp", "llp", "ltd", "plc", "corp", "co", "company",
                        "corporation", "group", "holdings", "holding", "trust", "fund", "funds", "capital",
                        "management", "investors", "investment", "investments", "partners", "advisors",
                        "advisers", "associates", "global", "international", "sa", "nv", "ag", "se",
                        "na", "us", "usa", "ii", "iii", "iv", "jr", "sr", "entities", "affiliated"})
#: how long before the table's filing a statement must have been filed to be
#: judged by it — the table speaks as of its record date, weeks earlier
TABLE_LEAD_DAYS = 60


def _name_tokens(name: str | None) -> frozenset:
    """A name's distinctive words."""
    from app.scraper.mapper import normalize_entity_name
    return frozenset(t for t in normalize_entity_name(name or "").split()
                     if len(t) > 1 and t not in _NAME_STOP)


def named_in_table(aliases: list[str], table_names: list[str]) -> bool:
    """True when any of a holder's names shares a distinctive word with a name
    the issuer's table lists ("Warren E. Buffett" ~ "BUFFETT WARREN E"; "The
    Vanguard Group" ~ "VANGUARD GROUP INC"; Lennar's Miller family group —
    MP Alpha, the Miller Charitable Fund — ~ "Stuart Miller"). Generous on
    purpose: a wrong match only keeps a statement open, a missed one would end
    a true holding."""
    held = frozenset().union(*map(_name_tokens, table_names)) if table_names else frozenset()
    return any(_name_tokens(alias) & held for alias in aliases)


def _role_holders(owned_id: str) -> list[str]:
    """The names of the people holding a role at the issuer."""
    from app.db.arcadedb import run_query
    return [r["n"] for r in run_query("MATCH (p:Person)-[:HAS_ROLE]->(e:Entity {id: $id}) RETURN DISTINCT p.full_name AS n",
                                      {"id": owned_id}) or [] if r.get("n")]


def _holder_aliases(owner_id: str, owner_label: str | None = "Entity") -> list[str]:
    """A holder's own names — name, aliases, former names — and those of its
    controlling parents (BlackRock, Inc. for BlackRock Finance)."""
    from app.db.arcadedb import run_query
    names: list[str] = []
    for r in run_query(
            f"""MATCH (o:{label_or_entity(owner_label)} {{id: $id}})
               OPTIONAL MATCH (g)-[k:OWNS]->(o) WHERE k.until IS NULL AND k.ownership_type = 'controlling'
               RETURN coalesce(o.name, o.full_name) AS n, o.aliases AS al, o.former_names AS fn,
                      collect(DISTINCT coalesce(g.name, g.full_name)) AS parents""", {"id": owner_id}) or []:
        names.append(r.get("n"))
        for extra in (r.get("al"), r.get("fn"), r.get("parents")):
            for x in extra or []:
                names.append(x.get("name") if isinstance(x, dict) else x)
    return [n for n in dict.fromkeys(names) if n]


def statement_persons(source_url: str | None) -> list[str]:
    """The reporting persons a 13D/G statement names on its cover pages — the
    group a proxy table lists by its controlling person (Berkshire's 13G/A on
    Occidental names Warren E. Buffett; the table lists "Warren E. Buffett and
    affiliated entities"). From the filing's `primary_doc.xml` (since December
    2024), else its full text (`sec_edgar._cover_page_names`, findings item 93).
    [] when the filing cannot be read — the caller keeps the holding then."""
    from app.scraper import sec_edgar
    if not source_url or not source_url.endswith("-index.htm"):
        return []
    folder, index = source_url.rsplit("/", 1)
    try:
        xml = sec_edgar._parse_13dg_xml(sec_edgar._get_text(f"{folder}/primary_doc.xml"))
    except Exception:  # noqa: BLE001 — no XML before December 2024
        xml = None
    if xml:
        return [p["name"] for p in xml.get("persons") or [] if p.get("name")]
    try:
        return sec_edgar._cover_page_names(sec_edgar._get_text(f"{folder}/{index.replace('-index.htm', '.txt')}"))
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC: reporting persons of %s not read: %s", source_url, exc)
        return []


def close_absent_from_table(owned_id: str, filing: dict, rows: list[dict]) -> int:
    """End the 13D/G holdings of `owned_id` its own ownership table no longer
    names (findings item 99): every open SEC statement of 5 % or more (or no
    stated percent) filed at least TABLE_LEAD_DAYS before the table's filing,
    whose holder the table lists under none of its names (`_holder_aliases`),
    closes on the filing's date — `until_reason` NOT_IN_TABLE, `until_source_url`
    the filing, no stale mark. The table lists every holder of more than 5 %:
    one it leaves out is no longer one — Sirius XM's 2026 proxy names Berkshire
    (37.1 %), John C. Malone and Vanguard, not Apollo (16.2 %, 2003), Loral or
    the 2009/2012 Liberty statements. The end is an upper bound: the holder
    may have dropped below 5 % long before.

    Only when the table lists a 5 % holder who holds no role at the issuer:
    a filing whose 5 % section the reader did not find, or a table of the
    company's own directors and officers only (Berkshire's), is no evidence. A holder is looked for under its own names, its controlling
    parents' and — read only for a holding about to close — every reporting
    person its statement names; a statement that cannot be read is kept, and
    so is an insider's (a holder named like a person with a role at the
    issuer: the officers' rows can sit in a table the reader missed).
    Kept too: a statement filed later, one stating under 5 % (the table
    cannot contradict it), a 13F. The claim stays. Returns how many closed."""
    from datetime import date as _date, timedelta
    from app.db.arcadedb import run_sql
    from app.scraper.maintenance import restatement_family
    big = [r["name"] for r in rows if any(p >= 5 for p in r.get("percents") or [])]
    insiders = _role_holders(owned_id)
    if not big or all(named_in_table([n], insiders) for n in big):
        # no 5 % holder, or only the issuer's own directors and officers —
        # Berkshire's proxy lists Buffett and its board, not the funds that
        # hold its Class B stock: no list of 5 % holders, no evidence
        return 0
    table_day = str(filing["date"])[:10]
    cutoff = (_date.fromisoformat(table_day) - timedelta(days=TABLE_LEAD_DAYS)).isoformat()
    names = [r["name"] for r in rows]
    closed = 0
    for e in run_sql("SELECT @rid AS rid, @out.id AS owner, @out.@type AS olabel, filing_type, source_date, "
                     "since, stake_percent, source_url FROM OWNS WHERE until IS NULL AND source_id = 'sec-edgar' AND @in.id = :id",
                     {"id": owned_id}) or []:
        if is_table_filing(e.get("filing_type")):
            # a holding an earlier table stated (findings item 86): a newer
            # table that no longer names it ends it — no statement to read
            if (str(e.get("source_date") or "")[:10] < table_day
                    and not named_in_table(_holder_aliases(e["owner"], e.get("olabel")), names)
                    and not named_in_table(_holder_aliases(e["owner"], e.get("olabel")), insiders)):
                run_sql(f"UPDATE {e['rid']} SET until = :d, until_reason = :why, until_source_url = :u, "
                        + STALE_CLEAR_SQL, {"d": table_day, "why": NOT_IN_TABLE, "u": filing["url"]})
                log.info("SEC: %s's table holding of %s ends %s — not in the newer table", e["owner"], owned_id, table_day)
                closed += 1
            continue
        if not restatement_family(e.get("filing_type")):
            continue
        pct = e.get("stake_percent")
        if isinstance(pct, (int, float)) and pct < 5:
            continue
        day = _statement_day(e) or str(e.get("since") or "")[:10]
        if not day or day >= cutoff:
            continue
        aliases = _holder_aliases(e["owner"], e.get("olabel"))
        # an insider is never judged: the directors' and officers' rows sit in
        # a table of their own that the reader can miss — Dick's 2026 proxy
        # read as its four funds, and Edward W. Stack (chairman, 19.69 %)
        # would have closed
        if named_in_table(aliases, names) or named_in_table(aliases, insiders):
            continue
        # the group the statement reports — read only for a holding that
        # would close, one request each
        persons = statement_persons(e.get("source_url"))
        if not persons or named_in_table(persons, names) or named_in_table(persons, insiders):
            continue
        run_sql(f"UPDATE {e['rid']} SET until = :d, until_reason = :why, until_source_url = :u, "
                + STALE_CLEAR_SQL, {"d": table_day, "why": NOT_IN_TABLE, "u": filing["url"]})
        log.info("SEC: %s's 13D/G holding of %s ends %s — not in the issuer's table", e["owner"], owned_id, table_day)
        closed += 1
    return closed


def read_table_and_close(owned_id: str, cik: str, submissions: dict | None = None,
                         write: bool = False, source_id: str | None = None) -> dict:
    """The issuer's newest ownership table (DEF 14A / 10-K/A within
    `proxy_statement.TABLE_MAX_AGE_DAYS`) read and applied: {"filing", "rows",
    "closed", "written"}; no table filing → {"filing": None, "closed": 0}. Two
    requests (the submissions, unless given, and the filing).

    ``write``: also write the 5 % holders no statement covers
    (`write_table_holders`, findings item 86) — the issuer's own scrape does;
    the whole-graph closing pass (`close_absent_from_tables`) does not, so a
    rebuild's step 7 never adds owners to companies nobody scraped."""
    from app.scraper import sec_edgar
    from app.scraper.proxy_statement import latest_table_filing, read_holder_rows, read_holder_table
    subs = submissions if submissions is not None else sec_edgar._submissions(cik)
    filing = latest_table_filing(subs)
    if not filing:
        return {"filing": None, "rows": 0, "closed": 0, "written": None}
    html = sec_edgar._get_text(filing["url"])
    rows = read_holder_rows(html)
    written = None
    if write and source_id:
        written = write_table_holders(owned_id, filing, read_holder_table(html), source_id, subs)
    return {"filing": filing, "rows": len(rows), "written": written,
            "closed": close_absent_from_table(owned_id, filing, rows)}


# ── The holders the issuer's table adds (findings item 86) ──────────────────
#
# A controlling holder that files no Schedule 13D/G appears in the issuer's own
# ownership table only: Harbor Lights Entertainment's 100 % of Skydance's Class
# A stock (10-K/A, 2026-04-24). The table's 5 % holders are written where no
# statement speaks for the pair — a holder linked to a node the graph already
# has, or to an EDGAR registrant of exactly that name; a person is never minted
# from a proxy's spelling (user, 2026-11-10).

#: a table row below this states no 5 % holding: the directors' "*" rows
TABLE_HOLDER_MIN_PCT = 5.0
#: forms only a registered fund files — its proxy lists who holds each share
#: class of record (IRAs, pension plans, "Individual Investor"), not owners
#: (Europacific Growth Fund, 2025)
FUND_FORMS = frozenset({"N-CSR", "N-CSRS", "485BPOS", "485APOS", "NPORT-P"})


def is_registered_fund(submissions: dict | None) -> bool:
    """Whether the filer is a registered investment company (it files fund
    reports or prospectus updates)."""
    forms = ((submissions or {}).get("filings") or {}).get("recent", {}).get("form") or []
    return any(f in FUND_FORMS for f in forms)


def _table_holder_node(name: str, source_id: str, entity_only: bool) -> tuple[str, str] | None:
    """The node a table's holder name stands for — strictly, since a proxy
    names holders loosely ("MUFG" is not "Morgan Stanley MUFG Securities Co.,
    Ltd.", which a fuzzy resolver picked): one Entity of exactly that name or
    name key, else (a name with no legal form) one Person of exactly that
    name, else an EDGAR registrant of exactly that name, written by its CIK.
    None when none, or more than one, is found."""
    from app.db.arcadedb import run_query
    from app.scraper import sec_edgar
    from app.scraper.graph_writer import _upsert_entity_by_name
    from app.scraper.mapper import has_entity_suffix
    found = run_query("MATCH (e:Entity) WHERE e.name = $n OR e.name_normalized = $k RETURN DISTINCT e.id AS id LIMIT 3",
                      {"n": name, "k": normalize_entity_name(name)}) or []
    if len(found) == 1:
        return found[0]["id"], "Entity"
    if found:
        return None
    if not has_entity_suffix(name):
        if entity_only:
            return None
        people = run_query("MATCH (p:Person) WHERE p.full_name = $n RETURN DISTINCT p.id AS id LIMIT 3", {"n": name}) or []
        return (people[0]["id"], "Person") if len(people) == 1 else None
    registrants = sec_edgar._browse_index(name, "")
    match = sec_edgar._exact_registrant(name, registrants) if registrants else None
    if not match:
        return None
    return _upsert_entity_by_name(name, cik=match["cik"], source_id=source_id), "Entity"


def _shared_blocks(rows: list[dict]) -> set[str]:
    """The rows that report one block between them: the same percent of the
    same class on several rows adding up to more than 100 % — Skydance's
    Harbor Lights (holder) and David Ellison (deemed, as its controller) both
    at 100 % of Class A; Molson Coors' Coors and Pentland sides of the voting
    trust, four rows at 96.2 %. The block is written once, on the one company
    among them (Harbor Lights); with several companies (Molson's) on none — the
    table does not say which holds it. Returns the names NOT to write."""
    from app.scraper.mapper import has_entity_suffix
    groups: dict = {}
    for r in rows:
        if r.get("stake_pct"):
            groups.setdefault((r["stake_pct"], r.get("share_class")), []).append(r)
    skip: set[str] = set()
    for (pct, _), group in groups.items():
        if len(group) < 2 or pct * len(group) <= 100:
            continue
        companies = [r for r in group if any(has_entity_suffix(n) for n in r["names"])]
        keep = companies[0]["name"] if len(companies) == 1 else None
        skip.update(r["name"] for r in group if r["name"] != keep)
    return skip


def write_table_holders(owned_id: str, filing: dict, table: dict, source_id: str,
                        submissions: dict | None = None) -> dict:
    """Write the issuer's 5 % holders its own ownership table states and no
    Schedule 13D/G covers (findings item 86): an SEC OWNS edge per holder —
    `filing_type` the table's form (DEF 14A / 10-K/A), `read_from` table, the
    largest stated class percent as the stake with its class, a voting-power
    column as the voting power, the table's record date as "held since at
    least" (`since_basis` first_reported) and as the event date.

    Not written: a registered fund's table (`is_registered_fund`); a row under
    TABLE_HOLDER_MIN_PCT; a block several rows share (`_shared_blocks`); a
    holder no strict lookup finds (`_table_holder_node`) — a group row ("Entities
    affiliated with X") only as the company X, never as the person; a pair a
    13D/G statement holds, open OR closed (Berkshire's 13G/A on Pool closed
    2026-03-31; the table, as of an earlier record date, would reopen it); a
    holder whose name, or its statement's reporting persons, the issuer's
    13D/G holders bear ("Warren E. Buffett" is Berkshire's). Over a Form 4
    count the table wins — beneficial, not of record (user, 2026-11-10); the
    Form 4 reader then yields to it (`stated_by_13dg`).

    Returns {"written", "covered", "unresolved", "shared", "fund"}."""
    from app.db.arcadedb import run_sql
    from app.scraper.edge_schema import READ_TABLE
    from app.scraper.mapper import derive_ownership_type
    out = {"written": 0, "covered": 0, "unresolved": 0, "shared": 0, "fund": False}
    if is_registered_fund(submissions):
        out["fund"] = True
        return out
    rows = [r for r in table.get("rows") or [] if (r.get("stake_pct") or 0) >= TABLE_HOLDER_MIN_PCT]
    if not rows:
        return out
    shared = _shared_blocks(rows)
    as_of = table.get("as_of")
    # The statements that speak for a holder as of the table: the open ones,
    # and those that ended on or after its date — Berkshire's 13G/A on Pool
    # closed 2026-03-31, after the table's 2026-03-26 filing. One that ended
    # before it says nothing about the table's holders.
    table_day = str(filing["date"])[:10]
    statements = [e for e in run_sql(
        "SELECT @out.id AS owner, @out.@type AS olabel, filing_type, source_url, until FROM OWNS "
        "WHERE source_id = 'sec-edgar' AND @in.id = :id", {"id": owned_id}) or []
        if is_beneficial_ownership_filing(e.get("filing_type"))
        and (not e.get("until") or str(e["until"])[:10] >= min(table_day, as_of or table_day))]
    stated_owners = {e["owner"] for e in statements}
    aliases = [a for e in {e["owner"]: e for e in statements}.values()
               for a in _holder_aliases(e["owner"], e.get("olabel"))]
    persons: list[str] | None = None              # read once, only when needed
    for r in rows:
        if r["name"] in shared:
            out["shared"] += 1
            continue
        if named_in_table(aliases, r["names"]):
            out["covered"] += 1
            continue
        node = next((n for n in (_table_holder_node(x, source_id, r.get("group")) for x in r["names"]) if n), None)
        if node is None:
            out["unresolved"] += 1
            log.info("SEC: %r (%s %% of %s) in %s's table — no node of exactly that name, not written",
                     r["name"], r["stake_pct"], owned_id, filing["form"])
            continue
        owner_id, owner_label = node
        if owner_id == owned_id:
            continue
        if owner_id in stated_owners:
            out["covered"] += 1
            continue
        if persons is None:
            # the open statements' cover pages only: one request each, and a
            # closed statement's holder is matched by its node and names above
            persons = [p for e in statements if not e.get("until") for p in statement_persons(e.get("source_url"))]
        if named_in_table(persons, r["names"]):
            out["covered"] += 1
            continue
        _upsert_owns_sec(
            owner_id, owned_id, source_id, owner_label=owner_label,
            ownership_type=derive_ownership_type(r["stake_pct"]),
            file_date=str(filing["date"])[:10], stake_percent=r["stake_pct"],
            voting_power_pct=r.get("voting_power_pct"), share_class=r.get("share_class"),
            source_url=filing["url"], filing_type=filing["form"], read_from=READ_TABLE,
            filing_dates_the_stake=False, event_date=as_of,
            since_date=as_of, since_basis="first_reported" if as_of else None)
        out["written"] += 1
        log.info("SEC: %r holds %s %% of %s (%s, its %s of %s)", r["name"], r["stake_pct"], owned_id,
                 r.get("share_class") or "the class", filing["form"], filing["date"])
    return out


def _share_owns_edge(session, owner_label: str, owner_id: str, owned_id: str,
                     current, bag: dict, now: str) -> None:
    """Apply an SEC assertion to the pair's active edge that another source drew.

    The answer (stake, source, link, end date…) is taken over only when SEC
    outranks the source holding it — a stated stake first, then credibility —
    and then as a unit; the start date is combined (earliest wins, a lower bound
    keeps its label); freshness moves when SEC is at least as credible, the same
    rule as the generic writer. The claim was recorded before this is reached,
    so SEC's own answer is never lost when the edge keeps another's.
    """
    held = {f: current.get(f) for f in (*ANSWER_FIELDS, *SINCE_FIELDS)}
    sets: dict = {}
    takeover = outranks(bag, held)
    if takeover:
        sets.update({f: bag.get(f) for f in ANSWER_FIELDS})
    since = combine_since(held, bag)
    sets.update({f: v for f, v in since.items() if v != held.get(f)})
    if bag.get("direct_or_indirect") and (
            current.get("direct_or_indirect") is None or current.get("structure_basis")):
        sets.update(direct_or_indirect=bag["direct_or_indirect"],
                    structure_basis=bag.get("structure_basis"))
    if takeover or int(bag.get("credibility_score") or 0) >= int(held.get("credibility_score") or 0):
        sets.update(last_scraped_at=now, **STALE_CLEAR)
    if not sets:
        return
    assignments = ", ".join(f"r.{f} = $v_{f}" for f in sets)
    session.run(
        f"""
        MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
        WHERE r.until IS NULL SET {assignments}
        """,
        oid=owner_id, nid=owned_id, **{f"v_{f}": v for f, v in sets.items()},
    )


def detach_owns_sec(owner_id: str, owned_id: str, source_id: str,
                    filing_type: str = "EX-21", owner_label: str = "Entity") -> str:
    """Withdraw SEC's assertion that ``owner`` holds ``owned`` directly — an
    Exhibit 21 re-read that now places the subsidiary under an intermediate
    parent, where an earlier flat read had hung it off the filer.

    SEC's claim of that filing family for the pair goes (the list's; a 13G or
    Form 4 about the same pair is another statement and stays). The edge goes only if it is SEC's alone;
    an edge another source also claims (GLEIF's ultimate-parent link, say)
    stays and is re-answered from that source's claim, so it never keeps
    citing SEC for a fact SEC withdrew. Returns "deleted", "reassigned" or
    "none" (nothing SEC's to withdraw). ``owner_label``: "Person" for an
    insider whose Form 4 holding is a shared vehicle's (findings item 111).
    """
    from app.claims import KIND_OWNS, claims_for, edge_values_from, owns_claim_key
    from app.db.arcadedb import run_sql
    from app.scraper.owns_merge import ANSWER_FIELDS
    withdrawn = owns_claim_key(owner_id, owned_id, source_id, filing_type)
    run_sql("DELETE FROM Claim WHERE claim_key = :k", {"k": withdrawn})
    with db.get_session() as session:
        edge = session.run(
            f"""MATCH (a:{owner_label} {{id: $o}})-[r:OWNS]->(b:Entity {{id: $n}})
               WHERE r.until IS NULL AND r.source_id = $sid RETURN r LIMIT 1""",
            o=owner_id, n=owned_id, sid=source_id).single()
        if not edge:
            return "none"
        # By key, not by source: SEC can hold another claim for the pair (an
        # acquisition read from the filer's XBRL, a 13G — keyed by filing
        # family), and that one still supports the edge when the list's claim
        # is withdrawn.
        others = [c for c in claims_for(owner_id, owned_id, KIND_OWNS) if c.get("claim_key") != withdrawn]
        if not others:
            session.run(
                f"""MATCH (a:{owner_label} {{id: $o}})-[r:OWNS]->(b:Entity {{id: $n}})
                   WHERE r.until IS NULL AND r.source_id = $sid DELETE r""",
                o=owner_id, n=owned_id, sid=source_id)
            return "deleted"
        values = {f: v for f, v in edge_values_from(others).items() if f in ANSWER_FIELDS}
        # The start from the claims that remain — clearing only the basis
        # kept SEC's withdrawn `since` and turned a "first listed 2013" lower
        # bound into a stated start, hiding the edge before 2013.
        values.update(combine_since(*others))
        if edge["r"].get("structure_basis"):
            values.update(direct_or_indirect=None, structure_basis=None)
        assignments = ", ".join(f"r.{f} = $v_{f}" for f in values)
        session.run(
            f"""MATCH (a:{owner_label} {{id: $o}})-[r:OWNS]->(b:Entity {{id: $n}})
                WHERE r.until IS NULL AND r.source_id = $sid SET {assignments}""",
            o=owner_id, n=owned_id, sid=source_id, **{f"v_{f}": v for f, v in values.items()})
        return "reassigned"


def write_acquisition_sec(owner_id: str, owned_id: str, source_id: str, *,
                          closing: str | None, value_usd: float | None,
                          file_date: str | None, source_url: str,
                          filing_type: str) -> str:
    """Record that ``owner`` bought ``owned``, from the acquirer's own XBRL
    business-combination facts (`sec_acquisitions`): a STATED start (the
    closing date), the consideration paid, read from fields.

    Two SEC families can speak about one pair. The Exhibit 21 list says what
    is held at year-end; this says when it was bought and for how much. They
    do not contradict each other, so neither overrules the other:

    * the pair's active SEC edge carries another filing type (EX-21 / EX-8.1 /
      a 13D…): the list keeps the edge's ANSWER — type, stake, grade, link —
      and gains what only the deal states: the closing date (combined with
      the start it has, earliest wins, a stated day beating a bound on the
      same day), the price and the event date. The acquisition is recorded
      as its own claim (keyed apart, `claims.ACQUISITION_FILING_TYPES`), so
      the Sources panel shows both filings. Returns "enriched". Routing this
      case through `_upsert_owns_sec` would null the list's stake and replace
      its grade and link — the list's answer, wiped by an event that agrees
      with it.
    * otherwise — no edge, our own earlier read, or a pair another source
      drew — the ordinary SEC write: `_upsert_owns_sec` creates the edge, or
      takes over the other source's (`_share_owns_edge`). Returns "written".

    A later Exhibit 21 read of the same pair leaves the closing date in place
    (`combine_since` keeps the earliest start, and the list brings none) and
    the price (COALESCE); the edge's type and grade become the list's again.
    Same end state whichever order the two run in.
    """
    _record_edge_parties(owner_id, owned_id)
    from app.claims import ACQUISITION_FILING_TYPES
    with db.get_session() as session:
        held = session.run(
            """MATCH (a:Entity {id: $o})-[r:OWNS]->(b:Entity {id: $n})
               WHERE r.source_id = $sid AND r.until IS NULL RETURN r LIMIT 1""",
            o=owner_id, n=owned_id, sid=source_id).single()
    if held and held["r"].get("filing_type") not in ACQUISITION_FILING_TYPES:
        current = held["r"]
        record_claim(kind=KIND_OWNS, from_id=owner_id, to_id=owned_id, source_id=source_id,
                     ownership_type="controlling", since=closing, since_basis=None,
                     since_source_url=source_url if closing else None,
                     event_date=closing, source_url=source_url, source_date=file_date,
                     credibility_score=98, filing_type=filing_type, read_from=READ_FIELD)
        started = combine_since({f: current.get(f) for f in SINCE_FIELDS},
                                {"since": closing, "since_basis": None,
                                 "since_source_url": source_url if closing else None})
        with db.get_session() as session:
            session.run(
                """MATCH (a:Entity {id: $o})-[r:OWNS]->(b:Entity {id: $n})
                   WHERE r.source_id = $sid AND r.until IS NULL
                   SET r.since = $since, r.since_basis = $basis, r.since_source_url = $surl,
                       r.value_usd = COALESCE($vusd, r.value_usd),
                       r.event_date = COALESCE(r.event_date, $edate),
                       r.last_scraped_at = $now, """ + STALE_CLEAR_CYPHER,
                o=owner_id, n=owned_id, sid=source_id,
                since=started["since"], basis=started["since_basis"],
                surl=started["since_source_url"], vusd=value_usd, edate=closing,
                now=datetime.now(timezone.utc).isoformat())
        return "enriched"
    _upsert_owns_sec(owner_id=owner_id, owned_id=owned_id, source_id=source_id,
                     ownership_type="controlling", file_date=file_date, stake_percent=None,
                     source_url=source_url, filing_type=filing_type,
                     filing_dates_the_stake=False, since_date=closing, since_basis=None,
                     since_source_url=source_url, event_date=closing, value_usd=value_usd,
                     read_from=READ_FIELD)
    return "written"


def set_since_lower_bound(owner_id: str, owned_id: str, since: str, source_url: str | None,
                          basis: str = "first_listed") -> bool:
    """Date a subsidiary-list edge by its oldest listing: "owned since at least".

    On the pair's ACTIVE edge, whichever source holds its answer — one edge per
    pair, and the start date is combined across sources (``owns_merge``) — and
    only ever EARLIER: a `since` that is already earlier (a stated start, or an
    older listing found before) is kept, and a stated start on the same day
    beats the bound. Sets ``since_basis`` so a reader can tell a date read off
    the lists from a stated start — ``"first_listed"`` ("since 2013 or
    earlier") or ``"newly_listed"`` ("first listed 2025": the list before does
    not name it, see ``sec_ex21.earliest_listing``) — and ``since_source_url``
    to the filing that proves it. The same date with the OTHER listing basis
    is rewritten too (a re-read that now knows more); a stated start never is.
    The claim for the same pair and filing type is dated by the same rule, on
    its own — also when the edge is already right. Returns whether the edge
    changed.

    Only for a pair an annual list actually names — an Exhibit 21/8.1 CLAIM, not
    the edge's own filing type, since the shared edge may carry another source's
    answer (a PSC stake outranks a list that states none).
    """
    from app.db.arcadedb import run_sql
    listed = run_sql(
        "SELECT count(*) AS n FROM Claim WHERE from_id = :o AND to_id = :n AND kind = 'owns' "
        "AND (filing_type = 'EX-21' OR filing_type = 'EX-8.1')", {"o": owner_id, "n": owned_id})
    if not (listed and listed[0].get("n")):
        return False
    with db.get_session() as session:
        rec = session.run(
            """MATCH (a:Entity {id: $o})-[r:OWNS]->(b:Entity {id: $n})
               WHERE r.until IS NULL AND (r.since IS NULL OR r.since > $since
                  OR (r.since = $since AND r.since_basis IS NOT NULL AND r.since_basis <> $basis))
               SET r.since = $since, r.since_basis = $basis, r.since_source_url = $url
               RETURN count(r) AS n""",
            o=owner_id, n=owned_id, since=since, url=source_url, basis=basis).single()
    changed = bool(rec and rec.get("n"))
    # The claim by its own guard, whether or not the edge moved: a claim that
    # lost its date while the edge kept it (see `claims._LISTING_DATE_KEPT`) is
    # repaired by the next run instead of staying undated for good.
    run_sql("UPDATE Claim SET since = :since, since_basis = :basis, "
            "since_source_url = :url WHERE kind = 'owns' AND from_id = :o AND to_id = :n "
            "AND (filing_type = 'EX-21' OR filing_type = 'EX-8.1') "
            "AND (since IS NULL OR since > :since "
            "OR (since = :since AND since_basis IS NOT NULL AND since_basis <> :basis))",
            {"since": since, "url": source_url, "o": owner_id, "n": owned_id, "basis": basis})
    return changed


def form_label(document_type: str | None) -> str:
    """The `filing_type` of a seat read from an insider filing: "Form 3" for
    an initial statement (`3`, `3/A`), "Form 4" otherwise — the label the
    ownership claims of the same scan carry, so the Sources panel reads
    "SEC EDGAR · Form 4" beside a seat as beside a holding."""
    return "Form 3" if str(document_type or "").strip().upper().startswith("3") else "Form 4"


def _upsert_role_sec(person_id: str, entity_id: str, role: str,
                     source_id: str, source_url: str | None = None,
                     source_date: str | None = None, credibility_score: int = 98,
                     since: str | None = None, until: str | None = None,
                     read_from: str | None = None, filing_type: str | None = None):
    """Create a HAS_ROLE edge attributed to SEC EDGAR if not already present.

    ``read_from``: how the seat was read (`edge_schema.READ_GRADES`) — a Form
    3/4 or Form D field. On the claim, a created seat, a relabelled one.
    ``filing_type``: which KIND of record stated the seat ("Form 4", "Form 3",
    "Form D") — on the claim, where the Sources panel shows it beside the
    source; the ownership claims carried it from the start, the seat claims
    did not until 2026-10-25 (208 dev claims without it).

    Provenance: source_url = the specific Form 3/4 filing document,
    source_date = its filing date. On a re-scrape of an existing edge we refresh
    last_scraped_at and backfill the URL/date (COALESCE keeps existing values
    when this scrape didn't yield them). `since` is the person's Form 3 date —
    the day they became an insider in this seat — and backfills an undated
    edge the same way; it never overwrites a date another source stated.
    """
    _record_edge_parties(entity_id)
    record_claim(kind=KIND_ROLE, from_id=person_id, to_id=entity_id, source_id=source_id,
                 role=role, source_url=source_url, source_date=source_date,
                 since=since, until=until, credibility_score=credibility_score,
                 read_from=read_from, filing_type=filing_type)
    # HAS_ROLE is not gated by claims-only — the mode suppresses ownership
    # structure, not people (see graph_writer._upsert_role).
    now = datetime.now(timezone.utc).isoformat()
    with db.get_session() as session:
        # The seat this filing is about: the same position over a touching
        # period (graph_writer._same_seat). A filing dated after a CLOSED
        # spell is a return, not that spell: Tim Cook's older Form 4 says CEO;
        # re-asserted after the 8-K closed the seat on 2026-09-01, it opened a
        # second CEO spell that the next 8-K read closed again, once per
        # scrape. An undated filing is dated by its filing date for this.
        existing = _same_seat(session, person_id, entity_id, role,
                              since or source_date, until)
        if existing and existing.get("until"):
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role AND r.until = $until
                SET r.last_scraped_at = $now, r.since = COALESCE(r.since, $since)
                """,
                pid=person_id, eid=entity_id, role=existing["role"], until=existing["until"],
                now=now, since=since)
            return
        if existing:
            # One seat; the earlier of the two starts (the finer of one date at
            # two precisions) — a Form 3 three days after Wikidata's
            # appointment day used to open a second "Director" seat.
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role AND r.until IS NULL
                SET r.last_scraped_at = $now,
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date),
                    r.since       = $start
                """,
                pid=person_id, eid=entity_id, role=existing["role"], now=now,
                surl=source_url, sdate=source_date,
                start=combined_start(existing.get("since"), since),
            )
            _relabel_if_more_credible(session, person_id, entity_id,
                                      existing["role"], role, existing["cred"],
                                      credibility_score, source_id, read_from)
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
            pid=person_id, eid=entity_id, role=role, since=since, until=until,
            sid=source_id, score=credibility_score, rfrom=read_from,
            surl=source_url, sdate=source_date, now=now,
        )
    if until is None:
        end_seats_at_death(person_id)      # an old Form 4 of someone since dead opens no seat


def _close_role_sec(person_id: str, entity_id: str, until: str, role: str | None = None,
                    source_id: str | None = None, source_url: str | None = None,
                    source_date: str | None = None, credibility_score: int = 98,
                    read_from: str | None = None, filing_type: str | None = None) -> int:
    """End the person's open seat(s) at the company on `until`.

    ``read_from``: how the departure was read — a "Former …" Form 4 is a
    field, an 8-K Item 5.02 is narrative (a name, a verb and a date out of
    an announcement's sentence). Recorded on the closing claim only; the seat
    keeps the grade of whoever drew it. ``filing_type``: the record that
    stated the departure ("Form 4", "8-K"), on that claim likewise.

    The one statutory statement of a departure: a "Former …" Form 4 or an 8-K
    Item 5.02 naming the person. With `role`, only that seat is closed (Tim
    Cook leaves the CEO seat and keeps the board one); without it, every open
    seat. Never creates a person or a seat — a departure we cannot attach to
    a listed person is not written. Returns the number of seats closed.
    """
    now = datetime.now(timezone.utc).isoformat()
    want = canonical_role(role) if role else None
    with db.get_session() as session:
        # Only a seat that began on or before the departure: a 2022 8-K about
        # someone who returned in 2024 closed the new seat with an end before
        # its start — absent in every year — and the next scrape reopened it.
        rows = session.run(
            """
            MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
            WHERE r.until IS NULL AND (r.since IS NULL OR r.since <= $until)
            RETURN r.role AS role
            """,
            pid=person_id, eid=entity_id, until=until)
        seats = [r["role"] for r in rows
                 if want is None or canonical_role(r["role"] or "") == want]
        for seat in seats:
            # The seat keeps the citation of whoever drew it; the departure
            # is cited beside it (`until_source_url`). Until 2026-10-25 the
            # closing filing's URL and date replaced the seat's own.
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role AND r.until IS NULL
                  AND (r.since IS NULL OR r.since <= $until)
                SET r.until = $until, r.last_scraped_at = $now,
                    r.until_source_url = COALESCE($surl, r.until_source_url)
                """,
                pid=person_id, eid=entity_id, role=seat, until=until, now=now,
                surl=source_url)
    if seats and source_id:
        close_role_claim(from_id=person_id, to_id=entity_id, source_id=source_id, role=seats[0],
                         until=until, until_source_url=source_url, until_read_from=read_from,
                         source_date=source_date, filing_type=filing_type,
                         credibility_score=credibility_score)
    return len(seats)


def mark_ex21_stale(holder_ids, current_url: str, as_of: str) -> int:
    """Dim the subsidiary edges the newest Exhibit 21/8.1 no longer lists.

    The 13F rule for the annual lists: a subsidiary missing from the newer
    list was perhaps sold, perhaps only too small to list (an exhibit need not
    name insignificant ones) — silence is not an end date, so it is flagged
    ``stale``, never closed. Before, it stayed current in every later year.

    ``holder_ids``: the filer and the intermediate parents this list placed
    subsidiaries under — the edges an exhibit of this filer drew hang off
    them. An edge counts when one of this filer's exhibits drew it (its
    ``source_url`` is under the filer's EDGAR folder), not the one just read,
    and it is as of an earlier year. A re-listed subsidiary heals itself: the
    upsert sets ``stale = false``.
    """
    import re as _re
    m = _re.match(r"(https?://www\.sec\.gov/Archives/edgar/data/\d+/)", current_url or "")
    if not m or not as_of:
        return 0
    marked = 0
    with db.get_session() as session:
        for hid in sorted(set(holder_ids)):
            rows = session.run(
                """MATCH (a:Entity {id: $h})-[r:OWNS]->(b:Entity)
                   WHERE (r.filing_type = 'EX-21' OR r.filing_type = 'EX-8.1')
                     AND r.until IS NULL AND COALESCE(r.stale, false) = false
                     AND r.source_url STARTS WITH $prefix AND r.source_url <> $url
                     AND r.source_date < $as_of
                   SET r.stale = true, r.stale_reason = 'ex21_not_listed',
                       r.stale_since = $as_of, r.stale_source_url = $url
                   RETURN count(r) AS n""",
                h=hid, prefix=m.group(1), url=current_url, as_of=as_of)
            marked += sum(int(r.get("n") or 0) for r in rows)
    return marked


def mark_13f_stale(company_id: str, period: str) -> int:
    """Dim the 13F holder edges the quarter just ingested did not confirm.

    A 13F seller never states an exit — the position simply vanishes from the
    manager's next filing, and silence is not a statement. So an edge whose
    ``source_date`` (the latest filing period — ``since`` keeps the first-seen
    quarter as a lower bound, ``since_basis = first_reported``; only
    source_date moves on refresh) predates the period just read is flagged
    ``stale`` (dimmed in the panel, and in time travel after that date),
    never closed: writing ``until`` would assert an end date
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
               RETURN a.id AS aid, labels(a)[0] AS alabel""",
            id=company_id, period=period))
        for r in rows:
            # Labelled anchor — a filer may be a fund (Entity) or a person, and
            # the label came back with the row; unlabelled it scans every vertex.
            session.run(
                f"""MATCH (a:{label_or_entity(r.get("alabel"))} {{id: $a}})-[r:OWNS]->(b:Entity {{id: $b}})
                   WHERE r.filing_type = '13F' AND r.until IS NULL
                     AND r.source_date < $period
                   SET r.stale = true, r.stale_reason = '13f_not_confirmed',
                       r.stale_since = $period, r.stale_source_url = null""",
                a=r["aid"], b=company_id, period=period)
    return len(rows)
