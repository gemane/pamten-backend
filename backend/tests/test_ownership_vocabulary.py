"""The stored vocabulary for an OWNS edge, and the boundaries that enforce it.

`OwnershipType` is the single source of truth. It used to disagree with what the
importers actually wrote — `unknown` was absent from the enum, so the manual
create endpoint rejected the most common value in the graph, while `partnership`
and `free_float` were accepted but never produced. `free_float` in particular is
not an edge: the widely-held remainder is derived on read as `free_float_pct`,
because nobody *holds* the free float.
"""
import pytest

from app.models.relationship import OwnershipType, coerce_ownership_type


class TestVocabulary:
    def test_contains_exactly_the_stored_values(self):
        assert {t.value for t in OwnershipType} == {
            "full", "majority", "controlling", "minority", "unknown",
        }

    def test_unknown_is_a_first_class_value(self):
        # Most sources name an owner without disclosing a percentage, so this is
        # the common case, not an error state.
        assert OwnershipType.unknown.value == "unknown"

    def test_free_float_is_not_storable(self):
        # It is computed on read (routers/search.py), never written to an edge.
        assert "free_float" not in {t.value for t in OwnershipType}


class TestCoercion:
    def test_passes_through_known_values(self):
        for value in ("full", "majority", "controlling", "minority", "unknown"):
            assert coerce_ownership_type(value) == value

    def test_maps_anything_unrecognised_to_unknown(self):
        # Storing a novel string verbatim would render as a neutral edge in the
        # UI while the data quietly diverged — better to land on `unknown`.
        for value in ("partnership", "free_float", "MAJORITY", "typo", "", None):
            assert coerce_ownership_type(value) == "unknown"


class TestPinBoundary:
    """A pin overrides an edge's value on read (app/pins.py), so it is the one
    place a human-supplied type reaches the graph's output."""

    def test_rejects_a_type_outside_the_vocabulary(self):
        from pydantic import ValidationError
        from app.models.flag import PinRequest

        with pytest.raises(ValidationError):
            PinRequest(ownership_type="partnership")

    def test_accepts_a_valid_type(self):
        from app.models.flag import PinRequest

        assert PinRequest(ownership_type="controlling").ownership_type == "controlling"

    def test_still_requires_at_least_one_field(self):
        from pydantic import ValidationError
        from app.models.flag import PinRequest

        with pytest.raises(ValidationError):
            PinRequest()

    def test_a_stake_only_pin_is_still_valid(self):
        from app.models.flag import PinRequest

        assert PinRequest(stake_percent=42.0).ownership_type is None
