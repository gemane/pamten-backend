"""How sure a merge is, read off its logged reason (app/merge_quality.py)."""
import pytest

from app import merge_quality as mq, person_evidence, person_keys

BASE = "same name/alias (order/spelling/title)"


@pytest.mark.parametrize("reason, basis, tier", [
    ("manual", "manual", mq.CONFIRMED),
    (None, "manual", mq.CONFIRMED),                                   # a row from before reasons were kept
    ("same SEC CIK", "register_number", mq.PROOF),
    ("same SEC CIK, share a company, same birth month", "register_number", mq.PROOF),
    ("same birth date + place", "birth_date", mq.STRONG),
    (f"{BASE}, same birth date", "birth_date", mq.STRONG),
    (f"{BASE}, share a company, same birth date", "birth_date", mq.STRONG),
    (f"{BASE}, same birth month, a rare name", "birth_month_rare_name", mq.LIKELY),
    (f"{BASE}, same birth month", "birth_month_rare_name", mq.LIKELY),  # before item 124: the month alone
    (f"{BASE}, share a company, same birth month", "birth_month_and_company", mq.STRONG),
    (f"{BASE}, same birth month, {person_evidence.CO_OWNER}", "birth_month_and_co_owner", mq.STRONG),
    (f"{BASE}, same birth month, {person_evidence.OWNS_OTHER}", "birth_month_and_ownership", mq.STRONG),
    (f"{BASE}, same birth month, {person_evidence.SAME_OFFICE}", "birth_month_and_office", mq.STRONG),
    (f"{BASE}, same birth month, {person_keys.EVIDENCE['v']}", "birth_month_and_verification", mq.STRONG),
    (f"{BASE}, same birth month, {person_keys.EVIDENCE['p']}", "birth_month_and_postcode", mq.STRONG),
    (f"{BASE}, share a company", "name_and_company", mq.LIKELY),
    ("same surname + shared company (name variant)", "name_and_company", mq.LIKELY),
    ("same surname + shared company (name variant), same birth month", "birth_month_and_company", mq.STRONG),
    (BASE, "name", mq.LIKELY),
])
def test_a_reason_gives_its_basis_and_tier(reason, basis, tier):
    assert mq.of(reason) == {"basis": basis, "tier": tier}


def test_every_basis_is_listed_for_the_client():
    reasons = ["manual", "same SEC CIK", "same birth date + place", f"{BASE}, same birth month, a rare name",
               f"{BASE}, share a company", BASE,
               *[f"{BASE}, same birth month, {fact}" for fact in mq._SECOND_FACTS]]
    assert {mq.of(r)["basis"] for r in reasons} == set(mq.BASES)


def test_every_phrase_of_the_evidence_modules_has_a_code():
    # a new kind of evidence must say how sure it is
    phrases = {person_evidence.CO_OWNER, person_evidence.OWNS_OTHER, person_evidence.SAME_OFFICE,
               *person_keys.EVIDENCE.values()}
    assert phrases <= set(mq._FACT_CODE)


@pytest.mark.parametrize("tiers, weakest", [
    ([mq.PROOF, mq.STRONG, mq.LIKELY], mq.LIKELY), ([mq.PROOF, mq.CONFIRMED], mq.CONFIRMED),
    ([mq.STRONG, mq.CONFIRMED], mq.STRONG), ([mq.PROOF], mq.PROOF), ([], None),
])
def test_a_person_is_as_sure_as_its_weakest_merge(tiers, weakest):
    assert mq.weakest(tiers) == weakest


def test_the_weakest_of_a_generator_is_found_too():
    # the profile hands one; read twice, it was empty the second time
    assert mq.weakest(t for t in [mq.PROOF, mq.STRONG]) == mq.STRONG
