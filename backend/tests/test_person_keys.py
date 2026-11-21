"""The dedupe-only keys of a person (app/person_keys.py, findings item 124)."""
import pytest

from app import person_keys


@pytest.mark.parametrize("value, key", [
    ("2023-03-01T00:00:00Z", "v:2023-03-01"),
    ("2026-01-05", "v:2026-01-05"),
    ("9999-12-31T00:00:00Z", None),          # the register's open end, no day
    ("", None), (None, None), ("soon", None),
])
def test_the_verification_day_is_the_day_alone(value, key):
    assert person_keys.verification_key(value) == key


def test_a_union_keeps_every_key_once_and_is_none_for_none():
    assert person_keys.union(["v:2026-01-05"], None, ["v:2026-07-19", "v:2026-01-05"], [None]) == \
        ["v:2026-01-05", "v:2026-07-19"]
    assert person_keys.union(None, []) is None and person_keys.union() is None


@pytest.mark.parametrize("a, b, evidence", [
    (["v:2026-01-05"], ["v:2026-01-05"], "the same identity-verification day"),
    (["v:2026-01-05", "v:2025-11-30"], ["v:2025-11-30"], "the same identity-verification day"),
    (["v:2026-01-05"], ["v:2026-07-19"], None),               # a different day: nothing
    (["v:2026-01-05"], None, None), (None, None, None), ([], [], None),
    (["x:whatever"], ["x:whatever"], None),                   # a kind nobody gave a meaning
])
def test_what_two_persons_keys_say(a, b, evidence):
    assert person_keys.shared_evidence(a, b) == evidence


def test_a_node_read_whole_never_carries_the_keys():
    from app.database import PRIVATE_PROPERTIES, _NodeWrapper, public_props
    row = {"@rid": "#1:0", "@cat": "v", "id": "chpsc:x", "full_name": "David Smith", "match_keys": ["v:2026-01-05"]}
    assert "match_keys" in PRIVATE_PROPERTIES
    assert dict(_NodeWrapper(row)) == {"id": "chpsc:x", "full_name": "David Smith"} == public_props(row)


# ── the correspondence postcode, hashed (step 6) ─────────────────────────────

SECRET = "a-test-key-of-reasonable-length"


def test_one_postcode_gives_one_key_however_it_is_written():
    key = person_keys.postcode_key("HU10 7NY", SECRET)
    assert key and key.startswith("p:") and len(key) == 18
    assert person_keys.postcode_key("hu107ny", SECRET) == key == person_keys.postcode_key(" HU10-7NY ", SECRET)
    assert person_keys.postcode_key("HU10 7NZ", SECRET) != key


def test_the_key_does_not_give_the_postcode_back_and_depends_on_the_secret():
    key = person_keys.postcode_key("HU10 7NY", SECRET)
    assert "HU10" not in key.upper() and "7NY" not in key.upper()
    assert person_keys.postcode_key("HU10 7NY", "another-key-of-reasonable-length") != key


@pytest.mark.parametrize("postcode, secret", [
    ("HU10 7NY", ""), ("HU10 7NY", None),         # no secret: the feature is off, no hash is made
    ("", SECRET), (None, SECRET), ("X1", SECRET),  # no postcode worth the name
    ("WC2H 9JQ", SECRET),                          # 71-75 Shelton Street: 182,516 people give it
])
def test_no_key_without_a_secret_a_postcode_or_for_an_agents_postcode(postcode, secret):
    assert person_keys.postcode_key(postcode, secret) is None


def test_the_agents_postcodes_are_the_shipped_list():
    codes = person_keys.mass_person_postcodes()
    assert len(codes) == 574 and {"WC2H9JQ", "N17GU", "EC1V2NX"} <= codes
    assert all(c == c.upper() and " " not in c for c in codes)


def test_two_persons_at_one_postcode_and_what_it_is_called():
    a = [person_keys.postcode_key("HU10 7NY", SECRET)]
    assert person_keys.shared_evidence(a, list(a)) == "the same correspondence postcode"
    assert person_keys.shared_evidence(a, [person_keys.postcode_key("LS21 1AA", SECRET)]) is None
    # a shared day and a shared postcode: one of them is named, both are evidence
    both = [*a, "v:2026-01-05"]
    assert person_keys.shared_evidence(both, list(both)) in person_keys.EVIDENCE.values()
