"""A node's id carries the register's key when the writer knows one.

The bulk importers always did — `lei:{LEI}`, `gb-coh:{number}`, `chpsc:…` —
while every node a per-company scrape created got a random uuid, whatever
hard id came with it. A uuid has one cost the forwarding table cannot cover:
a rebuild mints new ones, so every shared link, pin, flag and suppression
naming a Wikidata or SEC node broke on reload (2026-10-26: 814 entities and
1,411 persons on dev carried a QID or a CIK under a uuid).

`mint_node_id` keys a new node by the best hard id it arrives with — the LEI
(the same key the GLEIF import uses, so a later bulk load upserts the node
instead of creating a twin), else the SEC CIK (`sec:{cik}`), else the
Wikidata item (`wd:Q…`) — and falls back to a uuid for a name-only node (an
Exhibit 21 row, a 13D party without a CIK), which has nothing to key on. A
register's id before a knowledge base's (the user's call, 2026-10-30, as for
the hard ids themselves since #480); until then the item came before the CIK. The lookup
that precedes every creation still resolves by the hard ids, so a keyed id
never changes which node a writer finds.

**A node that learns a better hard id than its id is re-keyed then**
(`key_on_learning`): an Exhibit 21 row or a 13D party created by name and
later met with its CIK, a founder name that Wikidata later identifies — and,
since 2026-10-30, a keyed node too: `sec:` that learns an LEI becomes `lei:`,
`wd:` that learns a CIK or an LEI becomes `sec:` or `lei:`. Before, only a
uuid was re-keyed, so the id depended on which source wrote first (findings
item 20): Viavi was `wd:` in one rebuild and `sec:` in the next, and 22 dev
`sec:` nodes held an LEI under no `lei:` id, which the GLEIF delta (it updates
`lei:` ids) never refreshed and a full GLEIF import would have twinned. Ids of
the bulk importers (`gb-coh:`, `chpsc:`) are a register's own key and stay.
"""
import logging
import re
import uuid

from app.scraper.mapper import normalize_cik

log = logging.getLogger(__name__)

_QID = re.compile(r"^Q[0-9]+$")
_LEI = re.compile(r"^[A-Z0-9]{20}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


#: the keyed ids, best first; `key_on_learning` moves a node up, never down
_RANK = ("lei:", "sec:", "wd:")


def keyed_id(lei: str | None = None, wikidata_id: str | None = None,
             sec_cik: str | None = None) -> str | None:
    """The register-keyed id a node with these hard ids gets, or None when it
    has none worth keying on (a malformed value counts as none): the LEI, else
    the CIK, else the Wikidata item."""
    if lei and _LEI.match(lei.strip()):
        return f"lei:{lei.strip()}"
    cik = normalize_cik(sec_cik)
    if cik:
        return f"sec:{cik}"                      # always the 10-digit form: one filer, one id
    if wikidata_id and _QID.match(wikidata_id.strip()):
        return f"wd:{wikidata_id.strip()}"
    return None


def _rank(node_id: str) -> int | None:
    """Where an id stands in `_RANK` (a uuid last); None for any other id —
    a bulk importer's register key, which is never replaced."""
    if is_uuid(node_id):
        return len(_RANK)
    return next((i for i, prefix in enumerate(_RANK) if node_id.startswith(prefix)), None)


def mint_node_id(lei: str | None = None, wikidata_id: str | None = None,
                 sec_cik: str | None = None) -> str:
    """The id for a node a writer is about to create: keyed when it can be,
    a uuid otherwise."""
    return keyed_id(lei, wikidata_id, sec_cik) or str(uuid.uuid4())


def is_uuid(node_id: str | None) -> bool:
    return bool(node_id) and bool(_UUID.match(node_id))


def key_on_learning(session, label: str, node_id: str) -> str:
    """The node's id once a writer has stamped a hard id on it: a node whose
    hard ids now key it better than its id does (`_RANK`) is renamed to the
    keyed id (`rename_node_id`: the node, its claims, the stored references
    and a forwarding row); any other id is returned as it is. A keyed id
    already taken is a twin for the dedup, never forced — the node keeps its
    id."""
    current = _rank(node_id or "")
    if not current:                      # a bulk importer's key, or already `lei:`
        return node_id
    rec = session.run(f"MATCH (n:{label} {{id: $id}}) RETURN n.lei_id AS lei, "
                      "n.wikidata_id AS wid, n.sec_cik AS cik", id=node_id).single()
    new = keyed_id(rec["lei"], rec["wid"], rec["cik"]) if rec else None
    if not new or new == node_id or _rank(new) >= current:
        return node_id
    from app.merged_ids import rename_node_id
    if rename_node_id(session, label, node_id, new):
        log.info("%s %s learned a hard id: now %s", label, node_id, new)
        return new
    log.warning("keyed id %s already taken; %s %s keeps its id for the dedup", new, label, node_id)
    return node_id


def rekey(label: str, node_id: str) -> str:
    """`key_on_learning` for a writer outside a session (the GLEIF join, the
    SEC↔LEI bridge, an entity merge)."""
    from app.database import db
    with db.get_session() as session:
        return key_on_learning(session, label, node_id)
