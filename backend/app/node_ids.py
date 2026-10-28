"""A node's id carries the register's key when the writer knows one.

The bulk importers always did — `lei:{LEI}`, `gb-coh:{number}`, `chpsc:…` —
while every node a per-company scrape created got a random uuid, whatever
hard id came with it. A uuid has one cost the forwarding table cannot cover:
a rebuild mints new ones, so every shared link, pin, flag and suppression
naming a Wikidata or SEC node broke on reload (2026-10-26: 814 entities and
1,411 persons on dev carried a QID or a CIK under a uuid).

`mint_node_id` keys a new node by the best hard id it arrives with — the LEI
(the same key the GLEIF import uses, so a later bulk load upserts the node
instead of creating a twin), else the Wikidata item (`wd:Q…`), else the SEC
CIK (`sec:{cik}`) — and falls back to a uuid for a name-only node (an Exhibit
21 row, a 13D party without a CIK), which has nothing to key on. The lookup
that precedes every creation still resolves by the hard ids, so a keyed id
never changes which node a writer finds.

**A node that learns a hard id after its creation is re-keyed then**
(`key_on_learning`): an Exhibit 21 row or a 13D party created by name and
later met with its CIK, a founder name that Wikidata later identifies. The
2026-10-27 rebuild left two (WARBURG PINCUS LLC, Steven Sarracino) once the
one-off heal that used to rename them was gone.
"""
import logging
import re
import uuid

from app.scraper.mapper import normalize_cik

log = logging.getLogger(__name__)

_QID = re.compile(r"^Q[0-9]+$")
_LEI = re.compile(r"^[A-Z0-9]{20}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def keyed_id(lei: str | None = None, wikidata_id: str | None = None,
             sec_cik: str | None = None) -> str | None:
    """The register-keyed id a node with these hard ids gets, or None when it
    has none worth keying on (a malformed value counts as none)."""
    if lei and _LEI.match(lei.strip()):
        return f"lei:{lei.strip()}"
    if wikidata_id and _QID.match(wikidata_id.strip()):
        return f"wd:{wikidata_id.strip()}"
    cik = normalize_cik(sec_cik)
    if cik:
        return f"sec:{cik}"                      # always the 10-digit form: one filer, one id
    return None


def mint_node_id(lei: str | None = None, wikidata_id: str | None = None,
                 sec_cik: str | None = None) -> str:
    """The id for a node a writer is about to create: keyed when it can be,
    a uuid otherwise."""
    return keyed_id(lei, wikidata_id, sec_cik) or str(uuid.uuid4())


def is_uuid(node_id: str | None) -> bool:
    return bool(node_id) and bool(_UUID.match(node_id))


def key_on_learning(session, label: str, node_id: str) -> str:
    """The node's id once the writer has stamped a hard id on it: a uuid-keyed
    node that now carries one is renamed to the keyed id (`rename_node_id`:
    the node, its claims, the stored references and a forwarding row), any
    other id is returned as it is. A keyed id already taken is a twin for the
    dedup, never forced — the node keeps its uuid."""
    if not is_uuid(node_id):
        return node_id
    rec = session.run(f"MATCH (n:{label} {{id: $id}}) RETURN n.lei_id AS lei, "
                      "n.wikidata_id AS wid, n.sec_cik AS cik", id=node_id).single()
    new = keyed_id(rec["lei"], rec["wid"], rec["cik"]) if rec else None
    if not new:
        return node_id
    from app.merged_ids import rename_node_id
    if rename_node_id(session, label, node_id, new):
        log.info("%s %s learned a hard id: now %s", label, node_id, new)
        return new
    log.warning("keyed id %s already taken; %s %s keeps its uuid for the dedup", new, label, node_id)
    return node_id
