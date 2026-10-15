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
