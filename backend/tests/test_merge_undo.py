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


# ── findings item 126: whose value it was, and what an undo holds back ───────

@pytest.mark.parametrize("keep,dup,after,whose", [
    ({}, {"birth_date": "1979-10"}, {"birth_date": "1979-10"}, "duplicate"),
    ({"birth_date": "1979-10"}, {}, {"birth_date": "1979-10"}, "kept"),
    ({"birth_date": "1979-10"}, {"birth_date": "1979-10"}, {"birth_date": "1979-10"}, "both"),
    # the fuller of two agreeing dates is the duplicate's
    ({"birth_date": "1979-10"}, {"birth_date": "1979-10-05"}, {"birth_date": "1979-10-05"}, "duplicate"),
    ({"nationalities": ["US"]}, {"nationalities": ["GB"]}, {"nationalities": ["US", "GB"]}, "both"),
    ({"nationalities": ["US"]}, {}, {"nationalities": ["US"]}, "kept"),
    ({}, {}, {}, None),                                                  # never set
    ({"birth_date": "1979-10"}, {"birth_date": "1980-01"}, {"birth_date": "1979-10"}, "kept"),
])
def test_whose_value_the_merged_person_showed(keep, dup, after, whose):
    name = next(iter(after), "birth_date")
    assert merge_undo._came_from(name, keep, dup, after) == whose


@pytest.mark.parametrize("dup,after,current,confirmed,node,held", [
    # removed from the merged person: held back — confirmed or not (a script)
    ({"id": "d", "birth_date": "1979-10"}, {"birth_date": "1979-10"}, {}, set(), {"id": "d"}, ["birth_date"]),
    # still there: written back
    ({"id": "d", "birth_date": "1979-10"}, {"birth_date": "1979-10"}, {"birth_date": "1979-10"}, set(),
     {"id": "d", "birth_date": "1979-10"}, []),
    # changed, and nobody confirmed an edit of it (a scraper overwrote it): written back
    ({"id": "d", "description": "old"}, {"description": "old"}, {"description": "new"}, set(),
     {"id": "d", "description": "old"}, []),
    # changed by a confirmed edit: held back
    ({"id": "d", "birth_date": "1979-10"}, {"birth_date": "1979-10"}, {"birth_date": "1979-11"}, {"birth_date"},
     {"id": "d"}, ["birth_date"]),
    # the merged person never showed the duplicate's value: not this removal's business
    ({"id": "d", "birth_date": "1980-01"}, {"birth_date": "1979-10"}, {}, set(),
     {"id": "d", "birth_date": "1980-01"}, []),
    # a list loses only what was removed
    ({"id": "d", "nationalities": ["GB", "IE"]}, {"nationalities": ["US", "GB", "IE"]}, {"nationalities": ["US", "IE"]},
     set(), {"id": "d", "nationalities": ["IE"]}, ["nationalities"]),
    ({"id": "d", "nationalities": ["GB"]}, {"nationalities": ["US", "GB"]}, {}, set(), {"id": "d"}, ["nationalities"]),
    # a person keeps its name whatever happened to the merged one
    ({"id": "d", "full_name": "Jane Smith"}, {"id": "k", "full_name": "Jane Smith"}, {"id": "k"}, {"full_name"},
     {"id": "d", "full_name": "Jane Smith"}, []),
])
def test_what_an_undo_holds_back_from_the_duplicate(dup, after, current, confirmed, node, held):
    assert merge_undo._held_back(dup, after, current, confirmed) == (node, held)


@pytest.mark.parametrize("edge,whose", [
    (("HAS_ROLE", "co-a", "CEO"), "kept"),
    (("HAS_ROLE", "co-b", "Company Secretary"), "duplicate"),
    (("HAS_ROLE", "co-both", "Director"), "both"),
    (("HAS_ROLE", "co-a", "Director"), None),            # that company, another seat: nobody's
    (("OWNS", "co-b", ""), "duplicate"),                 # a holding carries no role
    (("OWNS", "co-a", ""), None),
])
def test_who_brought_a_link_into_the_merge(edge, whose):
    def seat(kind, other, role=None):
        return {"type": kind, "out": True, "other": other, "props": {"role": role} if role else {}}
    pictures = {"keep": {"edges": [seat("HAS_ROLE", "co-a", "CEO"), seat("HAS_ROLE", "co-both", "Director")]},
                "dup": {"edges": [seat("HAS_ROLE", "co-b", "Company Secretary"), seat("HAS_ROLE", "co-both", "Director"),
                                  seat("OWNS", "co-b")]}}
    with patch.object(merge_undo, "_fwd", lambda node_id: node_id):
        assert merge_undo._edge_from(edge, pictures) == whose

