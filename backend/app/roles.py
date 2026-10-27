"""Role vocabulary: one position, many spellings.

Every source names board seats in its own dialect — Form D says "Director",
Wikidata's P3320 gives "Board Member", proxy statements write "Member of the
Board of Directors" — and each spelling used to become its own HAS_ROLE edge,
so one seat rendered as two rows and could never corroborate itself. The
writers match (and the corroboration counter buckets) on `canonical_role`,
while the edge keeps displaying the label of its most credible asserter.

Pure logic, importable by `app.claims` without a database layer. The map is
deliberately conservative: only spellings that are the SAME position merge.
"Executive Officer" is broader than "CEO" and "Non-Executive Director" says
more than "Director" — folding either would destroy information, not noise.
"""
import logging
import re

_SYNONYMS = {
    "board member": "director",
    "member of the board": "director",
    "member of the board of directors": "director",
    "board of directors member": "director",
    "chief executive officer": "ceo",
    "chairperson": "chairman",
    "chair": "chairman",
    "chairman of the board": "chairman",
    "chief financial officer": "cfo",
    "chief operating officer": "coo",
    "chief technology officer": "cto",
}


def canonical_role(role: str | None) -> str:
    """The synonym-folded, punctuation-blind key for a role label."""
    key = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (role or "").lower())).strip()
    return _SYNONYMS.get(key, key)


def _date_parts(date: str) -> str:
    """The stated part of a date: '2020-06-00' → '2020-06', '2016-00-00' → '2016'."""
    while date.endswith("-00"):
        date = date[:-3]
    return date


def same_start(a: str | None, b: str | None) -> bool:
    """Whether two start dates can be the same day at different precisions.

    Wikidata holds Ryan Roslansky's LinkedIn CEO statement twice — once as
    2020-06-01, once as "June 2020" (`2020-06-00`) — and SEC states a director's
    day where Wikidata gives only the year. A date is equal to another when the
    coarser one contains it. Two different days stay two dates: a second
    appointment is a second seat. An undated side is not a match here — the
    writers handle "the seat learns its date" on their own."""
    if not a or not b:
        return False
    pa, pb = _date_parts(a), _date_parts(b)
    return pa == pb or pa.startswith(pb + "-") or pb.startswith(pa + "-")


def finer_start(a: str, b: str) -> str:
    """Of two `same_start` dates, the more precise one."""
    return a if len(_date_parts(a)) >= len(_date_parts(b)) else b


def combined_start(a: str | None, b: str | None) -> str | None:
    """The start of one seat two assertions date: the finer of one date at two
    precisions, else the EARLIEST — a Form 3 filed three days after Wikidata's
    appointment day states the same seat, and the seat began on the earlier
    day. An undated side yields to the dated one."""
    if not a or not b:
        return a or b
    return finer_start(a, b) if same_start(a, b) else min(a, b)


def _before_or_within(a: str, b: str) -> bool:
    """Whether date `a` is no later than date `b`, at their stated precision:
    a month is no later than any of its days, a year no later than its
    months ('2020' ≤ '2020-03-01'; '2020-03' ≤ '2020-03-15')."""
    pa, pb = _date_parts(a), _date_parts(b)
    if pa == pb or pa.startswith(pb + "-") or pb.startswith(pa + "-"):
        return True
    return pa < pb


def periods_overlap(a_since: str | None, a_until: str | None,
                    b_since: str | None, b_until: str | None) -> bool:
    """Whether two assertions of one position can be the SAME seat: their
    periods touch. Nobody holds the same post at the same company twice at
    once, so an open seat and a later open assertion are one seat however
    the two sources date its start (Wikidata's appointment day, the SEC's
    Form 3 three days on), and an end stated for an overlapping period ends
    that seat. Two spells that do not touch — CEO 1998–2001, CEO again from
    2011 — stay two seats. An undated side is open on that side: an undated
    assertion matches any spell, which is how a seat learns its date."""
    if a_until and b_since and not _before_or_within(b_since, a_until):
        return False
    if b_until and a_since and not _before_or_within(a_since, b_until):
        return False
    return True


# ── A seat ends no later than its holder's death ─────────────────────────────

DEATH = "death"            # HAS_ROLE.until_reason: the seat ended because the holder died
WIKIDATA_ITEM = "https://www.wikidata.org/wiki/{qid}"


def is_founder_role(role: str | None) -> bool:
    """A founder is not a seat that ends: Steve Jobs founded Apple whether or
    not he lives. "Founder", "Co-Founder" and their spellings stay open."""
    return "founder" in canonical_role(role)


def end_seats_at_death(person_id: str) -> dict:
    """Close the person's open seats on their death date — every seat but a
    founder's, and only a seat that began by then (a start after the death is
    the source's error, left open and counted in `after_death`). The edge says
    why (`until_reason = death`) and cites the person's Wikidata item when one
    is known; the source's claim is NOT touched — it stated no end, and the
    claim is what the source said. Called by the person writer when a death
    date arrives and by the seat writers when they open a seat for someone
    already dead; nothing to do for the living. Two dev directors sat on open
    boards years after their deaths (2026-10-25)."""
    from app.db.arcadedb import run_sql
    out = {"closed": 0, "founders_kept": 0, "after_death": 0}
    # Best-effort, as `record_claim` is: a scrape that succeeded must not be
    # reported as failed because this rule could not be applied; the heal
    # catches up. (The unit tests' mocked sessions never reach here either.)
    try:
        rows = run_sql("SELECT death_date, wikidata_id FROM Person WHERE id = :id", {"id": person_id})
        death = (rows or [{}])[0].get("death_date") if rows else None
        if not death:
            return out
        qid = rows[0].get("wikidata_id")
        seats = run_sql("SELECT @rid AS rid, role, since FROM HAS_ROLE WHERE until IS NULL AND @out.id = :id",
                        {"id": person_id})
        for seat in seats or []:
            if is_founder_role(seat.get("role")):
                out["founders_kept"] += 1
                continue
            if seat.get("since") and str(seat["since"]) > str(death):
                out["after_death"] += 1
                continue
            sets = "until = :d, until_reason = :why"
            params = {"d": death, "why": DEATH}
            if qid:
                sets += ", until_source_url = :u"
                params["u"] = WIKIDATA_ITEM.format(qid=qid)
            run_sql(f"UPDATE {seat['rid']} SET {sets}", params)
            out["closed"] += 1
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("could not apply the death rule to %s: %s", person_id, exc)
    return out
