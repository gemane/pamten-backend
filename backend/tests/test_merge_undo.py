"""The undo's pure rules (findings item 123): what the split restores on a
record the merge changed, and that a picture that cannot be stored never fails
the merge it belongs to."""
from unittest.mock import patch

import pytest

from app import merge_undo


@pytest.mark.parametrize("current, before, after, sets, removes", [
    # the merge filled a blank field and nobody touched it since: blank again
    ({"birth_date": "1979-10"}, {}, {"birth_date": "1979-10"}, {}, ["birth_date"]),
    # the merge moved a start date earlier: the kept person's own start is back
    ({"since": "2018-03-01"}, {"since": "2020-05-01"}, {"since": "2018-03-01"}, {"since": "2020-05-01"}, []),
    # … unless something wrote the field since the merge: that stays
    ({"birth_date": "1979-10-04"}, {}, {"birth_date": "1979-10"}, {}, []),
    ({"since": "2017-01-01"}, {"since": "2020-05-01"}, {"since": "2018-03-01"}, {}, []),
    # what the merge did not change is not touched, whatever it reads now
    ({"role": "Chair"}, {"role": "Director"}, {"role": "Director"}, {}, []),
    # a list the merge extended
    ({"aliases": ["J. Smith", "Jane Mary Smith"]}, {"aliases": ["J. Smith"]},
     {"aliases": ["J. Smith", "Jane Mary Smith"]}, {"aliases": ["J. Smith"]}, []),
])
def test_restore_is_three_way(current, before, after, sets, removes):
    assert merge_undo._restore(current, before, after) == (sets, removes)


def test_restore_skips_the_named_fields():
    assert merge_undo._restore({"id": "sec:1"}, {"id": "wd:Q1"}, {"id": "sec:1"}, skip=("id",)) == ({}, [])


def test_a_stored_name_is_never_trusted_as_sql():
    with pytest.raises(ValueError):
        merge_undo._safe("x = 1; DELETE FROM Person")
    assert merge_undo._safe("until_source_url") == "until_source_url"


def test_a_picture_that_cannot_be_stored_does_not_fail_the_merge():
    keep, dup = {"id": "a", "node": {}}, {"id": "b", "node": {}}
    with patch.object(merge_undo, "picture", side_effect=RuntimeError("database away")):
        assert merge_undo.record("s1", keep, dup, "a", "manual") is False


def test_the_claim_identity_does_not_depend_on_the_persons_id():
    claim = {"kind": "role", "from_id": "wd:Q1", "to_id": "co", "source_id": "wikidata", "role": "CEO"}
    with patch.object(merge_undo, "_fwd", side_effect=lambda i: i):
        moved = {**claim, "from_id": "sec:1"}
        assert merge_undo._claim_ident(claim, "wd:Q1") == merge_undo._claim_ident(moved, "sec:1")
        other = {**claim, "role": "Director"}
        assert merge_undo._claim_ident(claim, "wd:Q1") != merge_undo._claim_ident(other, "wd:Q1")
