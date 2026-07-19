"""
Pin overlay — a moderator's corrected value for an OWNS edge (stake % and/or
ownership type), kept separate from the scraped data so it survives re-scrapes
(Phase-B resolution of a verification flag). Like suppressions it is enforced at
**read time**: the read endpoints load the small pin set and apply the corrected
fields over the scraped ones, so the fix stands even if a later import overwrites
the edge.

Keyed by the OWNS edge's natural key (from_id, to_id).
"""


def load_pins(session) -> dict:
    """All OWNS pins as {(from_id, to_id): {stake_percent, ownership_type}}."""
    pins: dict = {}
    for rec in session.run(
        "MATCH (p:Pin) RETURN p.from_id AS f, p.to_id AS t, "
        "p.stake_percent AS stake, p.ownership_type AS otype"
    ):
        pins[(rec.get("f"), rec.get("t"))] = {
            "stake_percent": rec.get("stake"),
            "ownership_type": rec.get("otype"),
        }
    return pins


def apply_pin(pins: dict, from_id, to_id, rel: dict) -> dict:
    """Return ``rel`` with any pinned OWNS fields overriding the scraped values."""
    pin = pins.get((from_id, to_id))
    if not pin:
        return rel
    out = dict(rel)
    if pin.get("stake_percent") is not None:
        out["stake_percent"] = pin["stake_percent"]
    if pin.get("ownership_type") is not None:
        out["ownership_type"] = pin["ownership_type"]
    return out
