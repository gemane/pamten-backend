"""
Reading GLEIF's reporting exceptions — the reasons a company gives for naming no
parent.

The shapes here are copied from a real repex delta (2,986 records), including the
two that a parser written from the schema alone gets wrong: `ExceptionReason` is
a **list** even when it holds one reason, and one company usually files twice —
once about its direct parent, once about its ultimate one.
"""
from app.scraper.gleif_repex import (
    CATEGORY_PROPS, DEPRECATED_REASONS, KNOWN_REASONS, _exception_props, _values,
)


def _w(value):
    """The CDF's scalar wrapper."""
    return {"$": value}


def record(lei="5493001KJTIIGC8Y1R12", category="DIRECT_ACCOUNTING_CONSOLIDATION_PARENT",
           reasons=("NATURAL_PERSONS",), reference=None):
    rec = {"LEI": _w(lei), "ExceptionCategory": _w(category),
           "ExceptionReason": [_w(r) for r in reasons]}
    if reference:
        rec["ExceptionReference"] = [_w(reference)]
    return rec


class TestUnwrappingAField:
    def test_a_single_wrapped_value_in_a_list(self):
        assert _values([_w("NO_LEI")]) == ["NO_LEI"]

    def test_several(self):
        assert _values([_w("NO_LEI"), _w("NON_PUBLIC")]) == ["NO_LEI", "NON_PUBLIC"]

    def test_a_bare_object_is_read_too(self):
        # Not the shape GLEIF publishes today. The CDF has moved fields in and out
        # of lists before, and reading only the current shape turns the next such
        # change into silent data loss rather than an error.
        assert _values(_w("NO_LEI")) == ["NO_LEI"]

    def test_a_missing_field_is_empty(self):
        assert _values(None) == []

    def test_blanks_are_dropped(self):
        assert _values([_w(""), _w("  "), _w(" NO_LEI ")]) == ["NO_LEI"]

    def test_a_bare_blank_string_is_dropped_too(self):
        # The unwrapper handles the wrapped case; a bare value reaches the
        # strip on its own path, and a "reason" of one space is not a reason.
        assert _values(["  ", " NO_LEI "]) == ["NO_LEI"]


class TestWhatOneExceptionSays:
    def test_a_direct_parent_exception(self):
        node_id, props = _exception_props(record())
        assert node_id == "lei:5493001KJTIIGC8Y1R12"
        assert props == {"no_direct_parent_reason": "NATURAL_PERSONS"}

    def test_an_ultimate_parent_exception_is_a_different_property(self):
        # Two separate questions: the closest consolidating parent and the top of
        # the tree. A company can answer them differently and usually does.
        _, props = _exception_props(
            record(category="ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT", reasons=("NO_LEI",)))
        assert props == {"no_ultimate_parent_reason": "NO_LEI"}

    def test_several_reasons_are_all_kept(self):
        # Rare (6 of 2,986) and no reason to lose either half of the answer.
        _, props = _exception_props(record(reasons=("NO_LEI", "NON_PUBLIC")))
        assert props["no_direct_parent_reason"] == "NO_LEI, NON_PUBLIC"

    def test_a_reference_is_kept_beside_its_reason(self):
        # Where the filer points at the parent it did not name — usually a
        # register entry for a parent with no LEI. It is the only lead the record
        # offers, so it travels with the reason rather than being dropped.
        _, props = _exception_props(record(reasons=("NO_LEI",),
                                           reference="https://example.test/company/123"))
        assert props["no_direct_parent_reason_reference"] == "https://example.test/company/123"

    def test_an_unknown_reason_is_stored_as_it_stands(self):
        # GLEIF has added reasons before — NO_KNOWN_PERSON is newer than the
        # original schema. Storing only the ones we recognise would quietly drop
        # whatever comes next.
        _, props = _exception_props(record(reasons=("SOMETHING_NEW",)))
        assert props["no_direct_parent_reason"] == "SOMETHING_NEW"

    def test_every_reason_gleif_publishes_is_documented(self):
        # KNOWN_REASONS is documentation, not a filter; this keeps it honest about
        # the ones actually seen in the feed.
        assert {"NATURAL_PERSONS", "NON_CONSOLIDATING", "NO_LEI",
                "NO_KNOWN_PERSON", "NON_PUBLIC"} <= set(KNOWN_REASONS)

    def test_the_reasons_v2_1_retired_are_marked_and_still_read(self):
        # Folded into NON_PUBLIC on 2022-03-01, but a record filed before then and
        # never refreshed still carries one — 17 of 2,986 on a day's delta. Marked
        # so a reader knows they are legacy; still parsed, because they are still
        # arriving.
        assert DEPRECATED_REASONS < set(KNOWN_REASONS)
        assert "NON_PUBLIC" not in DEPRECATED_REASONS, "the umbrella is current, not legacy"
        _, props = _exception_props(record(reasons=("CONSENT_NOT_OBTAINED",)))
        assert props["no_direct_parent_reason"] == "CONSENT_NOT_OBTAINED"


class TestWhatIsRefused:
    def test_a_record_with_no_lei(self):
        rec = record()
        del rec["LEI"]
        assert _exception_props(rec) is None

    def test_a_record_with_no_reason(self):
        # An exception with no reason says only "no parent", which is what the
        # absence of a relationship already says. Writing it would add a property
        # meaning nothing.
        assert _exception_props(record(reasons=())) is None

    def test_a_category_outside_the_two_parent_questions(self):
        # GLEIF could add a category; guessing which property it belongs on would
        # be worse than ignoring it until we know.
        assert _exception_props(record(category="SOMETHING_ELSE_ENTIRELY")) is None

    def test_the_two_categories_are_the_ones_gleif_publishes(self):
        assert set(CATEGORY_PROPS) == {
            "DIRECT_ACCOUNTING_CONSOLIDATION_PARENT",
            "ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT",
        }
