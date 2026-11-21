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
