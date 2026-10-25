"""An entity has an end — `Entity.dissolved_on`, and what ends with it.

The day a legal entity ceased to exist, as a source states it: Wikidata's
P576 (dissolved, abolished or demolished — at the precision the statement
carries, `YYYY-00-00` for a year), GLEIF's `EntityExpirationDate` (rarely
filled; INACTIVE alone sets `active = false` without a date), Companies
House's `DissolutionDate` (the snapshot lists live companies only, so the
column is empty there — the status beside it, `company_status`, says
"Liquidation" or "Active - Proposal to Strike off" while the company lives).
Until 2026-10-25 nothing read any of them: a dissolved company read as live,
kept its open holdings and seats, and time travel hid it before its
incorporation but never after its end.

**Nothing of a dissolved company stays open** — the mirror of "a seat ends
no later than its holder's death" (app.roles): on the dissolution date its
open holdings (what it owns), the holdings in it (its shares cease to exist)
and its seats close with `until_reason = dissolution`, except a founder's
seat (a founder is not a seat that ends) and except an edge the source dates
after the dissolution (its error, left open and reported). The source's
claim is NOT touched — it stated no end, and the claim is what the source
said. `until_source_url` cites the record that stated the dissolution when
the caller knows it (the Wikidata item, the register page); the heal, which
cannot know which source did, cites none.
"""
import logging

from app.roles import WIKIDATA_ITEM, is_founder_role

log = logging.getLogger(__name__)

DISSOLUTION = "dissolution"     # OWNS / HAS_ROLE until_reason: the company ceased to exist

#: (edge type, the side the dissolved company is on): its holdings, the
#: holdings in it, its seats — everything open that it is a party to
_OPEN_EDGES = (("OWNS", "@out"), ("OWNS", "@in"), ("HAS_ROLE", "@in"))


def end_at_dissolution(entity_id: str, source_url: str | None = None) -> dict:
    """Close every open edge of a dissolved company on its `dissolved_on`
    (see the module docstring). Nothing to do for a company without one.
    Best-effort, as the death rule is: a scrape that succeeded must not be
    reported as failed because this rule could not be applied; the heal
    catches up."""
    from app.db.arcadedb import run_sql
    out = {"closed": 0, "founders_kept": 0, "after_dissolution": 0}
    try:
        rows = run_sql("SELECT dissolved_on, wikidata_id FROM Entity WHERE id = :id", {"id": entity_id})
        day = (rows or [{}])[0].get("dissolved_on") if rows else None
        if not day:
            return out
        cite = source_url or (WIKIDATA_ITEM.format(qid=rows[0]["wikidata_id"]) if rows[0].get("wikidata_id") else None)
        for edge, side in _OPEN_EDGES:
            open_edges = run_sql(
                f"SELECT @rid AS rid, role, since FROM {edge} WHERE until IS NULL AND {side}.id = :id",
                {"id": entity_id})
            for e in open_edges or []:
                if edge == "HAS_ROLE" and is_founder_role(e.get("role")):
                    out["founders_kept"] += 1
                    continue
                if e.get("since") and str(e["since"]) > str(day):
                    out["after_dissolution"] += 1
                    continue
                sets = "until = :d, until_reason = :why"
                params = {"d": day, "why": DISSOLUTION}
                if cite:
                    sets += ", until_source_url = :u"
                    params["u"] = cite
                run_sql(f"UPDATE {e['rid']} SET {sets}", params)
                out["closed"] += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("could not apply the dissolution rule to %s: %s", entity_id, exc)
    return out


def heal_ends_at_dissolution(apply: bool = False) -> dict:
    """Every open edge of a dissolved company, closed on the dissolution date
    by `end_at_dissolution` — the rule, applied to a database written before
    it, or loaded in bulk (the bulk importers set `dissolved_on` without
    closing anything). Dry run unless `apply`: counts what would close,
    names the companies."""
    from app.db.arcadedb import run_sql
    companies = run_sql("SELECT id, name, dissolved_on FROM Entity WHERE dissolved_on IS NOT NULL")
    out: dict = {"applied": bool(apply), "companies": 0, "closed": 0, "founders_kept": 0,
                 "after_dissolution": 0, "lines": []}
    for c in companies or []:
        cid, day = c["id"], str(c.get("dissolved_on") or "")
        if apply:
            res = end_at_dissolution(cid)
        else:
            res = {"closed": 0, "founders_kept": 0, "after_dissolution": 0}
            for edge, side in _OPEN_EDGES:
                for e in run_sql(f"SELECT role, since FROM {edge} WHERE until IS NULL AND {side}.id = :id",
                                 {"id": cid}) or []:
                    if edge == "HAS_ROLE" and is_founder_role(e.get("role")):
                        res["founders_kept"] += 1
                    elif e.get("since") and str(e["since"]) > day:
                        res["after_dissolution"] += 1
                    else:
                        res["closed"] += 1
        if res["closed"] or res["after_dissolution"] or res["founders_kept"]:
            out["companies"] += 1
            out["lines"].append(f"{c.get('name')} († {day}): {res['closed']} closed, "
                                f"{res['founders_kept']} founders kept, "
                                f"{res['after_dissolution']} dated after the dissolution")
        for k in ("closed", "founders_kept", "after_dissolution"):
            out[k] += res[k]
    return out
