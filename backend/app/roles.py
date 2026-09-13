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
