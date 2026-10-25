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


#: Form 4's officer-title placeholder ("See Remarks" — the title is in the
#: filing's remarks box), which reached the graph as a role before the reader
#: learned to look there (2026-10-25). Matched case-blind on the prefix: dev
#: carried both "See Remarks" and "See remarks".
PLACEHOLDER_ROLE_PREFIX = "see remarks"


def heal_placeholder_roles(apply: bool = False) -> dict:
    """Delete every HAS_ROLE edge and role claim whose role is the placeholder.

    The seat is real but its role was never read, and a re-scrape of the
    company (the reader now reads the remarks) writes it back with the role
    the filing states. Renaming the rows "Officer" instead would leave a
    second, roleless seat beside that one for good — the writers keep one
    edge per (person, company, canonical role), and "Officer" is not "CFO".
    Dry run unless `apply`; the result names the companies to re-scrape."""
    from app.db.arcadedb import run_sql
    where = f"role IS NOT NULL AND role.toLowerCase() LIKE '{PLACEHOLDER_ROLE_PREFIX}%'"
    edges = run_sql(f"SELECT @out.full_name AS person, @in.name AS company FROM HAS_ROLE WHERE {where}")
    claims = run_sql(f"SELECT count(*) AS n FROM Claim WHERE kind = 'role' AND {where}")
    out = {"edges": len(edges),
           "claims": int(claims[0]["n"]) if claims else 0,
           "companies": sorted({e["company"] for e in edges if e.get("company")}),
           "applied": bool(apply)}
    if apply:
        run_sql(f"DELETE FROM HAS_ROLE WHERE {where}")
        run_sql(f"DELETE FROM Claim WHERE kind = 'role' AND {where}")
    return out


SEC_SOURCE_ID = "sec-edgar"


def heal_seat_claim_filings(apply: bool = False) -> dict:
    """Give the SEC seat claims written before 2026-10-25 the `filing_type`
    the claim itself can still tell: a `narrative` reading with an end date
    is an 8-K Item 5.02 departure, a `primary_doc.xml` URL is a Form D. The
    insider filings' claims (a Form 3 or 4 — the index URL does not say
    which) are left for the next scrape of their company, which rewrites the
    claim with its form; `unfilled` counts them. Dry run unless `apply`.
    """
    from app.db.arcadedb import run_sql
    base = f"kind = 'role' AND source_id = '{SEC_SOURCE_ID}' AND filing_type IS NULL"
    rules = (("form_8k", "8-K", "read_from = 'narrative' AND until IS NOT NULL"),
             ("form_d", "Form D", "source_url LIKE '%/primary_doc.xml'"))
    out: dict = {"applied": bool(apply)}
    for key, label, where in rules:
        rows = run_sql(f"SELECT count(*) AS n FROM Claim WHERE {base} AND {where}")
        out[key] = int((rows or [{}])[0].get("n") or 0)
        if apply and out[key]:
            run_sql(f"UPDATE Claim SET filing_type = '{label}' WHERE {base} AND {where}")
    rows = run_sql(f"SELECT count(*) AS n FROM Claim WHERE {base}")
    left = int((rows or [{}])[0].get("n") or 0)
    out["unfilled"] = left if apply else left - out["form_8k"] - out["form_d"]
    return out
