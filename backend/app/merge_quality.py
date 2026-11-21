"""How sure a person merge is, in four words a reader can be shown (findings
item 124; the user, 2026-11-21: "the quality that it is one person can be
categorized and shown in the frontend").

Every merge logs why it was made (`MergeLog.reason`, the scan's own line). The
line is built from fixed phrases, so the category is read off it — for the
merges made before this existed as well:

- **proof** — the source's own key says so: one SEC filer number, one Wikidata
  item.
- **confirmed** — a person asked for the merge.
- **strong** — the same name and birth date to the day, or the same birth
  month beside a second fact about the same two (a shared company, what their
  companies say, a key of the register's own).
- **likely** — the same name and birth month under a rare name, or the same
  name at one company without any birth date. Right in more than 99 of 100
  cases as measured, and the first place to look when a person seems to be two.
"""
from __future__ import annotations

from app import person_evidence, person_keys

PROOF, CONFIRMED, STRONG, LIKELY = "proof", "confirmed", "strong", "likely"
#: weakest first — a person is as sure as the weakest merge it was made of
ORDER = (LIKELY, STRONG, CONFIRMED, PROOF)

MANUAL = "manual"
HARD_ID = "same SEC CIK"
BIRTH_AND_PLACE = "same birth date + place"
BIRTH_DATE = "same birth date"
BIRTH_MONTH = "same birth month"
SHARED_COMPANY = "share a company"
VARIANT_COMPANY = "shared company"                   # "same surname + shared company (name variant)"
RARE_NAME = "a rare name"

#: the second facts that stand beside a birth month
_SECOND_FACTS = (SHARED_COMPANY, VARIANT_COMPANY, person_evidence.CO_OWNER, person_evidence.OWNS_OTHER,
                 person_evidence.SAME_OFFICE, *person_keys.EVIDENCE.values())


def of(reason: str | None) -> dict:
    """{"basis", "tier"} for a merge's logged reason. ``basis`` names the
    evidence in a word the client translates."""
    text = reason or MANUAL
    if text == MANUAL:
        return {"basis": "manual", "tier": CONFIRMED}
    if HARD_ID in text:
        return {"basis": "register_number", "tier": PROOF}
    if BIRTH_AND_PLACE in text or (BIRTH_DATE in text and BIRTH_MONTH not in text):
        return {"basis": "birth_date", "tier": STRONG}
    fact = next((f for f in _SECOND_FACTS if f in text), None)
    if BIRTH_MONTH in text:
        if fact:
            return {"basis": "birth_month_and_" + _FACT_CODE[fact], "tier": STRONG}
        return {"basis": "birth_month_rare_name", "tier": LIKELY}
    if fact:
        return {"basis": "name_and_company", "tier": LIKELY}
    return {"basis": "name", "tier": LIKELY}


_FACT_CODE = {SHARED_COMPANY: "company", VARIANT_COMPANY: "company",
              person_evidence.CO_OWNER: "co_owner", person_evidence.OWNS_OTHER: "ownership",
              person_evidence.SAME_OFFICE: "office",
              person_keys.EVIDENCE[person_keys.VERIFIED]: "verification",
              person_keys.EVIDENCE[person_keys.POSTCODE]: "postcode"}

#: every basis `of` can return — the client carries a wording for each
BASES = ("manual", "register_number", "birth_date", "birth_month_rare_name", "name_and_company", "name",
         *sorted({"birth_month_and_" + code for code in _FACT_CODE.values()}))


def weakest(tiers) -> str | None:
    """The weakest of the given tiers, None for none."""
    have = set(tiers)                        # once: the caller may hand a generator
    return next((t for t in ORDER if t in have), None)
